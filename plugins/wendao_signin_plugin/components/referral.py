from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from components.growth_models import GrowthOperation, PromoterRecord, ReferralRecord
from components.growth_store import (
    INVITE_CODE_PREFIX,
    PROMOTER_PREFIX,
    REFERRAL_PREFIX,
    GrowthStore,
    growth_storage_key,
    identity_hash,
)
from components.points import PointChange, PointService


INVITE_CODE_ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'
INVITE_CODE_LENGTH = 8


class ReferralError(ValueError):
    pass


class InviteCodeNotFoundError(ReferralError):
    pass


class AlreadyBoundError(ReferralError):
    pass


@dataclass(frozen=True, slots=True)
class ReferralStats:
    invite_code: str
    registered_count: int
    bound_count: int
    effective_count: int
    recent_effective_count: int


class ReferralService:
    def __init__(
        self,
        store: GrowthStore,
        points: PointService,
        *,
        code_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._points = points
        self._code_factory = code_factory or self._generate_invite_code
        self._indexed_bots: set[str] = set()
        self._referrals: dict[str, dict[str, ReferralRecord]] = {}
        self._identifier_owners: dict[str, dict[str, str]] = {}

    async def ensure_promoter(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        at: str | None = None,
    ) -> PromoterRecord:
        async with self._store.bot_lock(bot_uuid):
            return await self._ensure_promoter_unlocked(bot_uuid, sender_id, at=at)

    async def register_invite(
        self,
        bot_uuid: str,
        invitee_sender_id: str,
        invite_code: str,
        *,
        already_bound: bool = False,
        at: str | None = None,
    ) -> ReferralRecord:
        normalized_code = self._normalize_invite_code(invite_code)
        invitee_hash = identity_hash(bot_uuid, invitee_sender_id)
        async with self._store.bot_lock(bot_uuid):
            existing = await self._store.get(
                growth_storage_key(REFERRAL_PREFIX, bot_uuid, invitee_hash),
                ReferralRecord,
            )
            if existing is not None:
                self._cache_referral(existing)
                return existing
            if already_bound:
                raise AlreadyBoundError('已绑定问道账号，当前邀请码登记不创建新关系。')
            promoter = await self._store.get(
                growth_storage_key(INVITE_CODE_PREFIX, bot_uuid, normalized_code),
                PromoterRecord,
            )
            if promoter is None:
                raise InviteCodeNotFoundError('邀请码不存在或已失效。')
            status = 'rejected' if promoter.identity_hash == invitee_hash else 'pending'
            referral = ReferralRecord(
                bot_uuid=bot_uuid,
                invitee_hash=invitee_hash,
                promoter_hash=promoter.identity_hash,
                invite_code=normalized_code,
                status=status,
                created_at=at or _now_iso(),
            )
            await self._store.save(
                growth_storage_key(REFERRAL_PREFIX, bot_uuid, invitee_hash),
                referral,
            )
            self._cache_referral(referral)
            return referral

    async def on_account_bound(
        self,
        bot_uuid: str,
        invitee_sender_id: str,
        user_identifier: str,
        *,
        at: str | None = None,
    ) -> ReferralRecord | None:
        invitee_hash = identity_hash(bot_uuid, invitee_sender_id)
        user_hash = identity_hash(bot_uuid, user_identifier)
        timestamp = at or _now_iso()
        async with self._store.bot_lock(bot_uuid):
            await self._ensure_promoter_unlocked(bot_uuid, invitee_sender_id, at=timestamp)
            await self._load_referrals_unlocked(bot_uuid)
            referral = self._referrals[bot_uuid].get(invitee_hash)
            if referral is None or referral.status != 'pending':
                return referral
            owner = self._identifier_owners[bot_uuid].get(user_hash)
            if owner is not None and owner != invitee_hash:
                rejected = replace(
                    referral,
                    status='rejected',
                    bound_at=timestamp,
                    user_identifier_hash=user_hash,
                )
                await self._save_referral(rejected)
                return rejected
            bound = replace(
                referral,
                status='bound',
                bound_at=timestamp,
                user_identifier_hash=user_hash,
            )
            await self._save_referral(bound)
            return bound

    async def on_signin_confirmed(
        self,
        bot_uuid: str,
        invitee_sender_id: str,
        *,
        promoter_reward_points: int,
        invitee_reward_points: int,
        at: str | None = None,
    ) -> ReferralRecord | None:
        if (
            type(promoter_reward_points) is not int
            or type(invitee_reward_points) is not int
            or promoter_reward_points < 0
            or invitee_reward_points < 0
        ):
            raise ValueError('推广奖励积分必须是非负整数。')
        invitee_hash = identity_hash(bot_uuid, invitee_sender_id)
        operation_id = f'referral-effective:{invitee_hash}'
        operation = await self._store.get_operation(bot_uuid, operation_id)
        async with self._store.bot_lock(bot_uuid):
            referral = await self._store.get(
                growth_storage_key(REFERRAL_PREFIX, bot_uuid, invitee_hash),
                ReferralRecord,
            )
            if referral is None:
                return referral
            if referral.status not in {'bound', 'effective'}:
                return referral

        if referral.status == 'effective' and (
            operation is None or operation.status == 'COMMITTED'
        ):
            return referral
        effective_at = at or _now_iso()
        if operation is not None:
            if operation.kind == 'referral-reward-zero':
                effective_at = operation.created_at
            elif operation.kind == 'points':
                changes = self._changes_from_operation(operation)
                entries = await self._points.apply_operation(
                    bot_uuid,
                    operation_id,
                    changes,
                    at=operation.created_at,
                    commit=False,
                )
                effective_at = entries[0].created_at if entries else operation.created_at
            else:
                raise ReferralError('推广奖励操作类型不匹配。')
        else:
            changes = self._reward_changes(
                referral,
                promoter_reward_points,
                invitee_reward_points,
            )
            if changes:
                entries = await self._points.apply_operation(
                    bot_uuid,
                    operation_id,
                    changes,
                    at=effective_at,
                    commit=False,
                )
                effective_at = entries[0].created_at if entries else effective_at
            else:
                await self._create_zero_operation(
                    bot_uuid,
                    operation_id,
                    referral,
                    promoter_reward_points,
                    invitee_reward_points,
                    effective_at,
                )

        async with self._store.bot_lock(bot_uuid):
            current = await self._store.get(
                growth_storage_key(REFERRAL_PREFIX, bot_uuid, invitee_hash),
                ReferralRecord,
            )
            if current is None:
                return None
            effective = current
            if current.status == 'bound':
                effective = replace(
                    current,
                    status='effective',
                    effective_at=effective_at,
                )
                await self._save_referral(effective)
            elif current.status != 'effective':
                return current
            operation = await self._store.get_operation(bot_uuid, operation_id)
            if operation is not None and operation.status == 'PENDING':
                await self._store.mark_step_applied(
                    bot_uuid,
                    operation_id,
                    'referral-effective',
                    effective_at,
                )
                await self._store.commit_operation(
                    bot_uuid,
                    operation_id,
                    effective_at,
                )
            return effective

    async def stats(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        now: str | None = None,
    ) -> ReferralStats:
        promoter = await self.ensure_promoter(bot_uuid, sender_id, at=now)
        async with self._store.bot_lock(bot_uuid):
            await self._load_referrals_unlocked(bot_uuid)
            records = tuple(
                item
                for item in self._referrals[bot_uuid].values()
                if item.promoter_hash == promoter.identity_hash
            )
        registered = sum(item.status in {'pending', 'bound', 'effective'} for item in records)
        bound = sum(item.status in {'bound', 'effective'} for item in records)
        effective = sum(item.status == 'effective' for item in records)
        current_time = _parse_time(now or _now_iso())
        cutoff = current_time - timedelta(days=7)
        recent = sum(
            item.status == 'effective'
            and bool(item.effective_at)
            and cutoff <= _parse_time(item.effective_at) <= current_time
            for item in records
        )
        return ReferralStats(
            invite_code=promoter.invite_code,
            registered_count=registered,
            bound_count=bound,
            effective_count=effective,
            recent_effective_count=recent,
        )

    async def _ensure_promoter_unlocked(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        at: str | None = None,
    ) -> PromoterRecord:
        identity = identity_hash(bot_uuid, sender_id)
        identity_key = growth_storage_key(PROMOTER_PREFIX, bot_uuid, identity)
        existing = await self._store.get(identity_key, PromoterRecord)
        if existing is not None:
            invite_key = growth_storage_key(INVITE_CODE_PREFIX, bot_uuid, existing.invite_code)
            if await self._store.get(invite_key, PromoterRecord) is None:
                await self._store.save(invite_key, existing)
            return existing
        while True:
            code = self._normalize_invite_code(self._code_factory())
            invite_key = growth_storage_key(INVITE_CODE_PREFIX, bot_uuid, code)
            if await self._store.get(invite_key, PromoterRecord) is not None:
                continue
            promoter = PromoterRecord(
                bot_uuid=bot_uuid,
                identity_hash=identity,
                invite_code=code,
                created_at=at or _now_iso(),
            )
            await self._store.save(identity_key, promoter)
            await self._store.save(invite_key, promoter)
            return promoter

    async def _load_referrals_unlocked(self, bot_uuid: str) -> None:
        if bot_uuid in self._indexed_bots:
            return
        prefix = growth_storage_key(REFERRAL_PREFIX, bot_uuid, '_index')[:-len('_index')]
        listed = await self._store.list_prefix(prefix, ReferralRecord)
        if listed.skipped_count:
            raise ReferralError('推广关系包含损坏记录。')
        records = {record.invitee_hash: record for record in listed.records}
        owners = {
            record.user_identifier_hash: record.invitee_hash
            for record in records.values()
            if record.user_identifier_hash and record.status in {'bound', 'effective'}
        }
        self._referrals[bot_uuid] = records
        self._identifier_owners[bot_uuid] = owners
        self._indexed_bots.add(bot_uuid)

    async def _save_referral(self, referral: ReferralRecord) -> None:
        await self._store.save(
            growth_storage_key(REFERRAL_PREFIX, referral.bot_uuid, referral.invitee_hash),
            referral,
        )
        self._cache_referral(referral)

    def _cache_referral(self, referral: ReferralRecord) -> None:
        self._referrals.setdefault(referral.bot_uuid, {})[referral.invitee_hash] = referral
        self._identifier_owners.setdefault(referral.bot_uuid, {})
        if referral.status in {'bound', 'effective'} and referral.user_identifier_hash:
            self._identifier_owners[referral.bot_uuid][
                referral.user_identifier_hash
            ] = referral.invitee_hash

    @staticmethod
    def _reward_changes(
        referral: ReferralRecord,
        promoter_points: int,
        invitee_points: int,
    ) -> tuple[PointChange, ...]:
        changes: list[PointChange] = []
        if promoter_points:
            changes.append(
                PointChange(
                    referral.promoter_hash,
                    promoter_points,
                    'referral_reward_promoter',
                    f'promoter:{referral.invitee_hash}',
                    referral.invitee_hash,
                )
            )
        if invitee_points:
            changes.append(
                PointChange(
                    referral.invitee_hash,
                    invitee_points,
                    'referral_reward_invitee',
                    f'invitee:{referral.invitee_hash}',
                    referral.invitee_hash,
                )
            )
        return tuple(changes)

    @staticmethod
    def _changes_from_operation(operation: GrowthOperation) -> tuple[PointChange, ...]:
        raw_changes = operation.payload.get('changes')
        if type(raw_changes) is not list:
            raise ReferralError('推广奖励操作缺少积分变更。')
        changes: list[PointChange] = []
        for raw in raw_changes:
            if type(raw) is not dict:
                raise ReferralError('推广奖励操作格式错误。')
            try:
                changes.append(
                    PointChange(
                        raw['identity_hash'],
                        raw['amount'],
                        raw['entry_type'],
                        raw['step_id'],
                        raw['reason'],
                    )
                )
            except (KeyError, TypeError) as exc:
                raise ReferralError('推广奖励操作格式错误。') from exc
        return tuple(changes)

    async def _create_zero_operation(
        self,
        bot_uuid: str,
        operation_id: str,
        referral: ReferralRecord,
        promoter_points: int,
        invitee_points: int,
        created_at: str,
    ) -> None:
        async with self._store.bot_lock(bot_uuid):
            await self._store.begin_operation(
                bot_uuid,
                operation_id,
                'referral-reward-zero',
                {
                    'referral_id': referral.invitee_hash,
                    'promoter_points': promoter_points,
                    'invitee_points': invitee_points,
                },
                created_at,
            )

    @staticmethod
    def _generate_invite_code() -> str:
        return ''.join(
            secrets.choice(INVITE_CODE_ALPHABET)
            for _ in range(INVITE_CODE_LENGTH)
        )

    @staticmethod
    def _normalize_invite_code(value: str) -> str:
        if type(value) is not str:
            raise ValueError('邀请码格式错误。')
        normalized = value.strip().upper()
        if (
            len(normalized) != INVITE_CODE_LENGTH
            or any(character not in INVITE_CODE_ALPHABET for character in normalized)
        ):
            raise ValueError('邀请码格式错误。')
        return normalized

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)

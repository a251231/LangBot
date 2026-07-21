from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from components.growth_models import EntitlementRecord, GrowthOperation
from components.growth_store import (
    ENTITLEMENT_PREFIX,
    GrowthStore,
    growth_storage_key,
    identity_hash,
)
from components.models import AccountRecord


class EntitlementError(ValueError):
    pass


class EntitlementExpiredError(EntitlementError):
    pass


class EntitlementStorageError(EntitlementError):
    pass


class EntitlementNotFoundError(EntitlementStorageError):
    pass


@dataclass(frozen=True, slots=True)
class EntitlementStatus:
    identity_hash: str
    active: bool
    expires_at: str
    trial_started_at: str


class EntitlementService:
    def __init__(self, store: GrowthStore) -> None:
        self._store = store

    async def initialize_existing_accounts(
        self,
        bot_uuid: str,
        accounts: Iterable[AccountRecord],
        *,
        trial_days: int,
        at: str | None = None,
    ) -> str:
        self._validate_trial_days(trial_days)
        account_list = tuple(accounts)
        if any(account.bot_uuid != bot_uuid for account in account_list):
            raise ValueError('存量账号不属于当前机器人。')

        operation_id = f'growth-rollout:{bot_uuid}'
        async with self._store.bot_lock(bot_uuid):
            operation = await self._load_operation(bot_uuid, operation_id)
            if operation is None:
                rollout_at = at or _now_iso()
                _parse_time(rollout_at)
                payload: dict[str, object] = {
                    'rollout_at': rollout_at,
                    'trial_days': trial_days,
                }
                operation = await self._begin_operation(
                    bot_uuid,
                    operation_id,
                    payload,
                    rollout_at,
                )
            else:
                rollout_at, trial_days = self._rollout_parameters(operation)

            expiry = _add_days(rollout_at, trial_days)
            for account in account_list:
                identity = identity_hash(bot_uuid, account.sender_id)
                step_id = f'entitlement:{identity}'
                current = await self._load_record(bot_uuid, identity)
                if current is None:
                    current = EntitlementRecord(
                        bot_uuid=bot_uuid,
                        identity_hash=identity,
                        expires_at=expiry,
                        created_at=rollout_at,
                        updated_at=rollout_at,
                        trial_started_at=rollout_at,
                    )
                    await self._save_record(current)
                if operation.status == 'PENDING' and step_id not in operation.applied_steps:
                    operation = await self._mark_step(
                        bot_uuid,
                        operation_id,
                        step_id,
                        rollout_at,
                    )

            if operation.status == 'PENDING':
                await self._commit_operation(
                    bot_uuid,
                    operation_id,
                    rollout_at,
                )
            return rollout_at

    async def ensure_for_binding(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        trial_days: int,
        at: str | None = None,
    ) -> EntitlementRecord:
        self._validate_trial_days(trial_days)
        started_at = at or _now_iso()
        _parse_time(started_at)
        identity = identity_hash(bot_uuid, sender_id)
        async with self._store.bot_lock(bot_uuid):
            existing = await self._load_record(bot_uuid, identity)
            if existing is not None:
                return existing
            record = EntitlementRecord(
                bot_uuid=bot_uuid,
                identity_hash=identity,
                expires_at=_add_days(started_at, trial_days),
                created_at=started_at,
                updated_at=started_at,
                trial_started_at=started_at,
            )
            await self._save_record(record)
            return record

    async def get_status(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        now: str | None = None,
    ) -> EntitlementStatus:
        identity = identity_hash(bot_uuid, sender_id)
        async with self._store.bot_lock(bot_uuid):
            return await self._get_status_by_identity_unlocked(
                bot_uuid,
                identity,
                now=now,
            )

    async def _get_status_by_identity_unlocked(
        self,
        bot_uuid: str,
        identity: str,
        *,
        now: str | None = None,
    ) -> EntitlementStatus:
        current_time = _parse_time(now or _now_iso())
        record = await self._load_record(bot_uuid, identity)
        if record is None:
            raise EntitlementNotFoundError('权益记录不存在。')
        expires_at = self._record_expiry(record)
        return EntitlementStatus(
            identity_hash=identity,
            active=current_time < expires_at,
            expires_at=record.expires_at,
            trial_started_at=record.trial_started_at,
        )

    async def require_active(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        now: str | None = None,
    ) -> EntitlementStatus:
        status = await self.get_status(bot_uuid, sender_id, now=now)
        if not status.active:
            raise EntitlementExpiredError('插件使用期限已到期。')
        return status

    async def extend(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        duration_days: int,
        now: str | None = None,
    ) -> EntitlementRecord:
        if type(duration_days) is not int or duration_days <= 0:
            raise ValueError('延期天数必须是正整数。')
        timestamp = now or _now_iso()
        current_time = _parse_time(timestamp)
        identity = identity_hash(bot_uuid, sender_id)
        async with self._store.bot_lock(bot_uuid):
            record = await self._load_record(bot_uuid, identity)
            if record is None:
                raise EntitlementNotFoundError('权益记录不存在。')
            expires_at = self._record_expiry(record)
            base = max(current_time, expires_at)
            updated = replace(
                record,
                expires_at=(base + timedelta(days=duration_days)).isoformat(),
                updated_at=timestamp,
            )
            await self._save_record(updated)
            return updated

    async def _ensure_identity_expiry_unlocked(
        self,
        bot_uuid: str,
        identity: str,
        *,
        expires_at: str,
        updated_at: str,
    ) -> EntitlementRecord:
        target_expiry = _parse_time(expires_at)
        _parse_time(updated_at)
        record = await self._load_record(bot_uuid, identity)
        if record is None:
            raise EntitlementNotFoundError('权益记录不存在。')
        current_expiry = self._record_expiry(record)
        if current_expiry >= target_expiry:
            return record
        updated = replace(
            record,
            expires_at=target_expiry.isoformat(),
            updated_at=updated_at,
        )
        await self._save_record(updated)
        return updated

    async def _load_record(
        self,
        bot_uuid: str,
        identity: str,
    ) -> EntitlementRecord | None:
        try:
            return await self._store.get(
                growth_storage_key(ENTITLEMENT_PREFIX, bot_uuid, identity),
                EntitlementRecord,
            )
        except Exception as exc:
            raise EntitlementStorageError('读取权益记录失败。') from exc

    async def _save_record(self, record: EntitlementRecord) -> None:
        try:
            await self._store.save(
                growth_storage_key(
                    ENTITLEMENT_PREFIX,
                    record.bot_uuid,
                    record.identity_hash,
                ),
                record,
            )
        except Exception as exc:
            raise EntitlementStorageError('保存权益记录失败。') from exc

    async def _load_operation(
        self,
        bot_uuid: str,
        operation_id: str,
    ) -> GrowthOperation | None:
        try:
            return await self._store.get_operation(bot_uuid, operation_id)
        except Exception as exc:
            raise EntitlementStorageError('读取权益初始化操作失败。') from exc

    async def _begin_operation(
        self,
        bot_uuid: str,
        operation_id: str,
        payload: dict[str, object],
        created_at: str,
    ) -> GrowthOperation:
        try:
            return await self._store.begin_operation(
                bot_uuid,
                operation_id,
                'entitlement-rollout',
                payload,
                created_at,
            )
        except Exception as exc:
            raise EntitlementStorageError('创建权益初始化操作失败。') from exc

    async def _mark_step(
        self,
        bot_uuid: str,
        operation_id: str,
        step_id: str,
        updated_at: str,
    ) -> GrowthOperation:
        try:
            return await self._store.mark_step_applied(
                bot_uuid,
                operation_id,
                step_id,
                updated_at,
            )
        except Exception as exc:
            raise EntitlementStorageError('更新权益初始化步骤失败。') from exc

    async def _commit_operation(
        self,
        bot_uuid: str,
        operation_id: str,
        updated_at: str,
    ) -> None:
        try:
            await self._store.commit_operation(bot_uuid, operation_id, updated_at)
        except Exception as exc:
            raise EntitlementStorageError('提交权益初始化操作失败。') from exc

    @staticmethod
    def _rollout_parameters(operation: GrowthOperation) -> tuple[str, int]:
        if (
            operation.kind != 'entitlement-rollout'
            or operation.status not in {'PENDING', 'COMMITTED'}
        ):
            raise EntitlementStorageError('权益初始化操作类型错误。')
        rollout_at = operation.payload.get('rollout_at')
        trial_days = operation.payload.get('trial_days')
        if type(rollout_at) is not str or type(trial_days) is not int:
            raise EntitlementStorageError('权益初始化操作格式错误。')
        try:
            _parse_time(rollout_at)
            EntitlementService._validate_trial_days(trial_days)
        except ValueError as exc:
            raise EntitlementStorageError('权益初始化操作格式错误。') from exc
        return rollout_at, trial_days

    @staticmethod
    def _record_expiry(record: EntitlementRecord) -> datetime:
        try:
            return _parse_time(record.expires_at)
        except ValueError as exc:
            raise EntitlementStorageError('权益记录时间格式错误。') from exc

    @staticmethod
    def _validate_trial_days(trial_days: int) -> None:
        if type(trial_days) is not int or trial_days < 0:
            raise ValueError('试用天数必须是非负整数。')


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    if type(value) is not str or not value:
        raise ValueError('权益时间格式错误。')
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('权益时间必须包含时区。')
    return parsed


def _add_days(value: str, days: int) -> str:
    return (_parse_time(value) + timedelta(days=days)).isoformat()

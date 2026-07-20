from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from components.growth_models import PointAccount, PointEntry
from components.growth_store import (
    POINT_ACCOUNT_PREFIX,
    POINT_ENTRY_PREFIX,
    GrowthStore,
    growth_storage_key,
)


class InsufficientPointsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PointChange:
    identity_hash: str
    amount: int
    reason: str
    step_id: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _entry_id(operation_id: str, step_id: str, identity: str) -> str:
    raw = f'{operation_id}\0{step_id}\0{identity}'.encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


class PointService:
    def __init__(self, store: GrowthStore) -> None:
        self._store = store

    async def balance(self, bot_uuid: str, identity: str) -> int:
        account = await self._get_account(bot_uuid, identity)
        return account.balance if account is not None else 0

    async def credit(
        self,
        bot_uuid: str,
        identity: str,
        amount: int,
        reason: str,
        operation_id: str,
        *,
        at: str | None = None,
    ) -> PointEntry:
        if amount <= 0:
            raise ValueError('增加积分必须大于零。')
        entries = await self.apply_operation(
            bot_uuid,
            operation_id,
            (PointChange(identity, amount, reason, f'credit:{identity}'),),
            at=at,
        )
        return entries[0]

    async def debit(
        self,
        bot_uuid: str,
        identity: str,
        amount: int,
        reason: str,
        operation_id: str,
        *,
        at: str | None = None,
    ) -> PointEntry:
        if amount <= 0:
            raise ValueError('扣减积分必须大于零。')
        entries = await self.apply_operation(
            bot_uuid,
            operation_id,
            (PointChange(identity, -amount, reason, f'debit:{identity}'),),
            at=at,
        )
        return entries[0]

    async def adjust(
        self,
        bot_uuid: str,
        identity: str,
        amount: int,
        reason: str,
        operation_id: str,
        *,
        at: str | None = None,
    ) -> PointEntry:
        if amount == 0:
            raise ValueError('积分调整数量不能为零。')
        entries = await self.apply_operation(
            bot_uuid,
            operation_id,
            (PointChange(identity, amount, reason, f'adjust:{identity}'),),
            at=at,
        )
        return entries[0]

    async def apply_operation(
        self,
        bot_uuid: str,
        operation_id: str,
        changes: Sequence[PointChange],
        *,
        at: str | None = None,
    ) -> tuple[PointEntry, ...]:
        timestamp = at or _now_iso()
        normalized = tuple(changes)
        self._validate_changes(normalized)
        payload: dict[str, object] = {
            'changes': [
                {
                    'identity_hash': change.identity_hash,
                    'amount': change.amount,
                    'reason': change.reason,
                    'step_id': change.step_id,
                }
                for change in normalized
            ]
        }

        async with self._store.bot_lock(bot_uuid):
            existing = await self._store.get_operation(bot_uuid, operation_id)
            if existing is None:
                await self._preflight_new(bot_uuid, normalized)
            operation = await self._store.begin_operation(
                bot_uuid,
                operation_id,
                'points',
                payload,
                timestamp,
            )
            if operation.status == 'COMMITTED':
                return await self._load_entries(
                    bot_uuid,
                    operation_id,
                    normalized,
                    timestamp,
                )

            await self._preflight_pending(
                bot_uuid,
                operation_id,
                normalized,
                set(operation.applied_steps),
            )
            entries: list[PointEntry] = []
            for change in normalized:
                entry = await self._apply_change(
                    bot_uuid,
                    operation_id,
                    change,
                    set(operation.applied_steps),
                    timestamp,
                )
                entries.append(entry)
                operation = await self._store.mark_step_applied(
                    bot_uuid,
                    operation_id,
                    change.step_id,
                    timestamp,
                )
            await self._store.commit_operation(bot_uuid, operation_id, timestamp)
            return tuple(entries)

    @staticmethod
    def _validate_changes(changes: tuple[PointChange, ...]) -> None:
        if not changes:
            raise ValueError('积分操作不能为空。')
        step_ids: set[str] = set()
        for change in changes:
            if (
                not change.identity_hash
                or not change.reason
                or not change.step_id
                or change.amount == 0
            ):
                raise ValueError('积分操作参数错误。')
            if change.step_id in step_ids:
                raise ValueError('积分操作步骤不能重复。')
            step_ids.add(change.step_id)

    async def _preflight_new(
        self,
        bot_uuid: str,
        changes: tuple[PointChange, ...],
    ) -> None:
        balances: dict[str, int] = {}
        for change in changes:
            if change.identity_hash not in balances:
                balances[change.identity_hash] = await self.balance(
                    bot_uuid,
                    change.identity_hash,
                )
            balances[change.identity_hash] += change.amount
            if balances[change.identity_hash] < 0:
                raise InsufficientPointsError('积分余额不足。')

    async def _preflight_pending(
        self,
        bot_uuid: str,
        operation_id: str,
        changes: tuple[PointChange, ...],
        applied_steps: set[str],
    ) -> None:
        balances: dict[str, int] = {}
        for change in changes:
            if change.step_id in applied_steps:
                continue
            entry = await self._get_entry(bot_uuid, operation_id, change)
            if entry is not None:
                balances[change.identity_hash] = entry.balance_after
                continue
            if change.identity_hash not in balances:
                balances[change.identity_hash] = await self.balance(
                    bot_uuid,
                    change.identity_hash,
                )
            balances[change.identity_hash] += change.amount
            if balances[change.identity_hash] < 0:
                raise InsufficientPointsError('积分余额不足。')

    async def _apply_change(
        self,
        bot_uuid: str,
        operation_id: str,
        change: PointChange,
        applied_steps: set[str],
        timestamp: str,
    ) -> PointEntry:
        existing = await self._get_entry(bot_uuid, operation_id, change)
        if change.step_id in applied_steps:
            if existing is None:
                raise ValueError('已应用的积分步骤缺少流水。')
            await self._save_account_from_entry(existing, timestamp)
            return existing
        if existing is not None:
            await self._save_account_from_entry(existing, timestamp)
            return existing

        current = await self._get_account(bot_uuid, change.identity_hash)
        current_balance = current.balance if current is not None else 0
        new_balance = current_balance + change.amount
        if new_balance < 0:
            raise InsufficientPointsError('积分余额不足。')
        entry = PointEntry(
            bot_uuid=bot_uuid,
            entry_id=_entry_id(operation_id, change.step_id, change.identity_hash),
            identity_hash=change.identity_hash,
            amount=change.amount,
            reason=change.reason,
            operation_id=operation_id,
            balance_after=new_balance,
            created_at=timestamp,
            previous_entry_id=current.last_entry_id if current is not None else '',
        )
        entry_key = growth_storage_key(POINT_ENTRY_PREFIX, bot_uuid, entry.entry_id)
        await self._store.save(entry_key, entry)
        await self._save_account_from_entry(entry, timestamp)
        return entry

    async def _load_entries(
        self,
        bot_uuid: str,
        operation_id: str,
        changes: tuple[PointChange, ...],
        updated_at: str,
    ) -> tuple[PointEntry, ...]:
        entries: list[PointEntry] = []
        for change in changes:
            entry = await self._get_entry(bot_uuid, operation_id, change)
            if entry is None:
                raise ValueError('已提交的积分操作缺少流水。')
            await self._save_account_from_entry(entry, updated_at)
            entries.append(entry)
        return tuple(entries)

    async def _get_account(
        self,
        bot_uuid: str,
        identity: str,
    ) -> PointAccount | None:
        key = growth_storage_key(POINT_ACCOUNT_PREFIX, bot_uuid, identity)
        return await self._store.get(key, PointAccount)

    async def _get_entry(
        self,
        bot_uuid: str,
        operation_id: str,
        change: PointChange,
    ) -> PointEntry | None:
        entry_id = _entry_id(operation_id, change.step_id, change.identity_hash)
        key = growth_storage_key(POINT_ENTRY_PREFIX, bot_uuid, entry_id)
        return await self._store.get(key, PointEntry)

    async def _save_account_from_entry(
        self,
        entry: PointEntry,
        updated_at: str,
    ) -> None:
        account = PointAccount(
            bot_uuid=entry.bot_uuid,
            identity_hash=entry.identity_hash,
            balance=entry.balance_after,
            last_entry_id=entry.entry_id,
            updated_at=updated_at,
        )
        key = growth_storage_key(
            POINT_ACCOUNT_PREFIX,
            entry.bot_uuid,
            entry.identity_hash,
        )
        await self._store.save(key, account)

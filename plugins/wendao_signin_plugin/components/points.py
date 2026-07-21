from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from components.growth_models import POINT_ENTRY_TYPES, PointAccount, PointEntry
from components.growth_store import (
    POINT_ACCOUNT_PREFIX,
    POINT_ENTRY_PREFIX,
    GrowthStore,
    growth_storage_key,
)


class InsufficientPointsError(ValueError):
    pass


class PointLedgerCorruptionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PointChange:
    identity_hash: str
    amount: int
    entry_type: str
    step_id: str
    reason: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _entry_id(operation_id: str, step_id: str, identity: str) -> str:
    raw = f'{operation_id}\0{step_id}\0{identity}'.encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


class PointService:
    def __init__(self, store: GrowthStore) -> None:
        self._store = store
        self._indexed_bots: set[str] = set()
        self._indexed_revisions: dict[str, int] = {}
        self._ledger_tails: dict[tuple[str, str], PointEntry] = {}

    async def balance(self, bot_uuid: str, identity: str) -> int:
        async with self._store.bot_lock(bot_uuid):
            return await self._balance_unlocked(bot_uuid, identity)

    async def _balance_unlocked(self, bot_uuid: str, identity: str) -> int:
        account = await self._get_or_rebuild_account(bot_uuid, identity)
        return account.balance if account is not None else 0

    async def credit(
        self,
        bot_uuid: str,
        identity: str,
        amount: int,
        entry_type: str,
        operation_id: str,
        *,
        at: str | None = None,
    ) -> PointEntry:
        if type(amount) is not int or amount <= 0:
            raise ValueError('增加积分必须大于零。')
        entries = await self.apply_operation(
            bot_uuid,
            operation_id,
            (
                PointChange(
                    identity,
                    amount,
                    entry_type,
                    f'credit:{identity}',
                    '',
                ),
            ),
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
        if type(amount) is not int or amount <= 0:
            raise ValueError('扣减积分必须大于零。')
        entries = await self.apply_operation(
            bot_uuid,
            operation_id,
            (
                PointChange(
                    identity,
                    -amount,
                    'redeem_debit',
                    f'debit:{identity}',
                    reason,
                ),
            ),
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
        if type(amount) is not int or amount == 0:
            raise ValueError('积分调整数量不能为零。')
        entries = await self.apply_operation(
            bot_uuid,
            operation_id,
            (
                PointChange(
                    identity,
                    amount,
                    'admin_adjustment',
                    f'adjust:{identity}',
                    reason,
                ),
            ),
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
        if type(operation_id) is not str or not operation_id:
            raise ValueError('积分操作 ID 不能为空。')
        timestamp = at or _now_iso()
        normalized = tuple(changes)
        self._validate_changes(normalized)
        payload: dict[str, object] = {
            'changes': [
                {
                    'identity_hash': change.identity_hash,
                    'amount': change.amount,
                    'entry_type': change.entry_type,
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
                type(change.entry_type) is not str
                or change.entry_type not in POINT_ENTRY_TYPES
            ):
                raise ValueError('积分流水类型错误。')
            if (
                type(change.identity_hash) is not str
                or not change.identity_hash
                or type(change.amount) is not int
                or change.amount == 0
                or type(change.step_id) is not str
                or not change.step_id
                or type(change.reason) is not str
            ):
                raise ValueError('积分操作参数错误。')
            if change.entry_type.startswith('referral_reward_') and change.amount < 0:
                raise ValueError('积分奖励数量必须大于零。')
            if change.entry_type == 'redeem_debit' and change.amount > 0:
                raise ValueError('积分兑换扣减数量必须小于零。')
            if change.entry_type == 'admin_adjustment' and not change.reason:
                raise ValueError('管理员积分调整原因不能为空。')
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
                account = await self._get_or_rebuild_account(
                    bot_uuid,
                    change.identity_hash,
                )
                balances[change.identity_hash] = account.balance if account else 0
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
                account = await self._get_or_rebuild_account(
                    bot_uuid,
                    change.identity_hash,
                )
                balances[change.identity_hash] = account.balance if account else 0
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
            await self._reconcile_account_from_entry(existing, timestamp)
            return existing
        if existing is not None:
            await self._reconcile_account_from_entry(existing, timestamp)
            return existing

        current = await self._get_or_rebuild_account(
            bot_uuid,
            change.identity_hash,
        )
        current_balance = current.balance if current is not None else 0
        new_balance = current_balance + change.amount
        if new_balance < 0:
            raise InsufficientPointsError('积分余额不足。')
        entry = PointEntry(
            bot_uuid=bot_uuid,
            entry_id=_entry_id(operation_id, change.step_id, change.identity_hash),
            identity_hash=change.identity_hash,
            amount=change.amount,
            entry_type=change.entry_type,
            reason=change.reason,
            operation_id=operation_id,
            balance_after=new_balance,
            created_at=timestamp,
            previous_entry_id=current.last_entry_id if current is not None else '',
        )
        entry_key = growth_storage_key(POINT_ENTRY_PREFIX, bot_uuid, entry.entry_id)
        await self._store.save(entry_key, entry)
        await self._write_account_from_entry(entry, timestamp)
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
            await self._reconcile_account_from_entry(entry, updated_at)
            entries.append(entry)
        return tuple(entries)

    async def _get_account(
        self,
        bot_uuid: str,
        identity: str,
    ) -> PointAccount | None:
        key = growth_storage_key(POINT_ACCOUNT_PREFIX, bot_uuid, identity)
        return await self._store.get(key, PointAccount)

    async def _get_or_rebuild_account(
        self,
        bot_uuid: str,
        identity: str,
    ) -> PointAccount | None:
        account = await self._get_account(bot_uuid, identity)
        await self._ensure_ledger_index(bot_uuid)
        tail = self._ledger_tails.get((bot_uuid, identity))
        if tail is None:
            if account is not None:
                raise PointLedgerCorruptionError('积分账户缺少对应流水。')
            return None
        if (
            account is not None
            and account.last_entry_id == tail.entry_id
            and account.balance == tail.balance_after
        ):
            return account
        return await self._write_account_from_entry(tail, tail.created_at)

    async def _ensure_ledger_index(self, bot_uuid: str) -> None:
        revision = self._store.point_entry_revision(bot_uuid)
        if (
            bot_uuid in self._indexed_bots
            and self._indexed_revisions.get(bot_uuid) == revision
        ):
            return
        for key in tuple(self._ledger_tails):
            if key[0] == bot_uuid:
                del self._ledger_tails[key]
        prefix = f'{POINT_ENTRY_PREFIX}{bot_uuid}:'
        if not await self._store.has_prefix(prefix):
            self._indexed_bots.add(bot_uuid)
            self._indexed_revisions[bot_uuid] = revision
            return
        listed = await self._store.list_prefix(prefix, PointEntry)
        if listed.skipped_count:
            raise PointLedgerCorruptionError('积分流水包含损坏记录。')
        entries_by_identity: dict[str, list[PointEntry]] = {}
        for entry in listed.records:
            entries_by_identity.setdefault(entry.identity_hash, []).append(entry)
        for identity, entries in entries_by_identity.items():
            self._ledger_tails[(bot_uuid, identity)] = self._find_ledger_tail(entries)
        self._indexed_bots.add(bot_uuid)
        self._indexed_revisions[bot_uuid] = revision

    @staticmethod
    def _find_ledger_tail(entries: Sequence[PointEntry]) -> PointEntry:
        by_id = {entry.entry_id: entry for entry in entries}
        referenced_ids = {
            entry.previous_entry_id
            for entry in entries
            if entry.previous_entry_id
        }
        tails = tuple(entry for entry in entries if entry.entry_id not in referenced_ids)
        if len(tails) != 1:
            raise PointLedgerCorruptionError('积分流水链存在多个尾节点或循环。')

        tail = tails[0]
        visited: set[str] = set()
        cursor = tail
        while True:
            if cursor.entry_id in visited:
                raise PointLedgerCorruptionError('积分流水链存在循环。')
            visited.add(cursor.entry_id)
            if not cursor.previous_entry_id:
                break
            previous = by_id.get(cursor.previous_entry_id)
            if previous is None:
                raise PointLedgerCorruptionError('积分流水链缺少前序记录。')
            cursor = previous
        if len(visited) != len(entries):
            raise PointLedgerCorruptionError('积分流水链存在分支或孤立记录。')

        return tail

    async def _get_entry(
        self,
        bot_uuid: str,
        operation_id: str,
        change: PointChange,
    ) -> PointEntry | None:
        entry_id = _entry_id(operation_id, change.step_id, change.identity_hash)
        key = growth_storage_key(POINT_ENTRY_PREFIX, bot_uuid, entry_id)
        return await self._store.get(key, PointEntry)

    async def _reconcile_account_from_entry(
        self,
        entry: PointEntry,
        updated_at: str,
    ) -> None:
        current = await self._get_or_rebuild_account(
            entry.bot_uuid,
            entry.identity_hash,
        )
        if current is None:
            raise PointLedgerCorruptionError('积分流水缺少账户尾节点。')
        if current.last_entry_id == entry.entry_id:
            if current.balance == entry.balance_after:
                return
        elif current.last_entry_id != entry.previous_entry_id:
            return
        await self._write_account_from_entry(entry, updated_at)

    async def _write_account_from_entry(
        self,
        entry: PointEntry,
        updated_at: str,
    ) -> PointAccount:
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
        self._ledger_tails[(entry.bot_uuid, entry.identity_hash)] = entry
        self._indexed_bots.add(entry.bot_uuid)
        self._indexed_revisions[entry.bot_uuid] = self._store.point_entry_revision(
            entry.bot_uuid
        )
        return account

from __future__ import annotations

import asyncio

import pytest

from components.growth_models import GrowthOperation, PointAccount, PointEntry
from components.growth_store import (
    GROWTH_OPERATION_PREFIX,
    POINT_ACCOUNT_PREFIX,
    POINT_ENTRY_PREFIX,
    GrowthStore,
)
from components.points import InsufficientPointsError, PointChange, PointService


class FakeStorage:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.fail_prefix_once = ''

    async def set_plugin_storage(self, key: str, value: bytes) -> None:
        if self.fail_prefix_once and key.startswith(self.fail_prefix_once):
            self.fail_prefix_once = ''
            raise RuntimeError('simulated storage interruption')
        self.values[key] = value

    async def get_plugin_storage(self, key: str) -> bytes:
        return self.values[key]

    async def get_plugin_storage_keys(self) -> list[str]:
        return list(self.values)

    async def delete_plugin_storage(self, key: str) -> None:
        del self.values[key]


def test_credit_replay_creates_one_entry() -> None:
    async def scenario() -> None:
        store = GrowthStore(FakeStorage())
        points = PointService(store)

        first = await points.credit(
            'bot-a',
            'identity-a',
            100,
            'referral',
            'operation-1',
            at='2026-07-20T00:00:00+08:00',
        )
        replay = await points.credit(
            'bot-a',
            'identity-a',
            100,
            'referral',
            'operation-1',
            at='2026-07-20T00:00:01+08:00',
        )
        entries = await store.list_prefix(
            f'{POINT_ENTRY_PREFIX}bot-a:',
            PointEntry,
        )

        assert first == replay
        assert await points.balance('bot-a', 'identity-a') == 100
        assert len(entries.records) == 1

    asyncio.run(scenario())


def test_committed_replay_rebuilds_missing_account_snapshot() -> None:
    async def scenario() -> None:
        store = GrowthStore(FakeStorage())
        points = PointService(store)
        entry = await points.credit(
            'bot-a',
            'identity-a',
            100,
            'referral',
            'operation-1',
            at='2026-07-20T00:00:00+08:00',
        )
        account_key = f'{POINT_ACCOUNT_PREFIX}bot-a:identity-a'
        await store.delete(account_key)

        replay = await points.credit(
            'bot-a',
            'identity-a',
            100,
            'referral',
            'operation-1',
            at='2026-07-20T00:00:01+08:00',
        )

        assert replay == entry
        assert await points.balance('bot-a', 'identity-a') == 100

    asyncio.run(scenario())


def test_replaying_older_operation_does_not_downgrade_latest_balance() -> None:
    async def scenario() -> None:
        store = GrowthStore(FakeStorage())
        points = PointService(store)
        await points.credit(
            'bot-a',
            'identity-a',
            100,
            'first-credit',
            'operation-1',
            at='2026-07-20T00:00:00+08:00',
        )
        await points.credit(
            'bot-a',
            'identity-a',
            50,
            'second-credit',
            'operation-2',
            at='2026-07-20T00:01:00+08:00',
        )

        await points.credit(
            'bot-a',
            'identity-a',
            100,
            'first-credit',
            'operation-1',
            at='2026-07-20T00:02:00+08:00',
        )

        assert await points.balance('bot-a', 'identity-a') == 150

    asyncio.run(scenario())


def test_missing_account_snapshot_rebuilds_from_latest_ledger_entry() -> None:
    async def scenario() -> None:
        store = GrowthStore(FakeStorage())
        points = PointService(store)
        await points.credit(
            'bot-a',
            'identity-a',
            100,
            'first-credit',
            'operation-1',
            at='2026-07-20T00:00:00+08:00',
        )
        await points.credit(
            'bot-a',
            'identity-a',
            50,
            'second-credit',
            'operation-2',
            at='2026-07-20T00:01:00+08:00',
        )
        await store.delete(f'{POINT_ACCOUNT_PREFIX}bot-a:identity-a')

        await points.credit(
            'bot-a',
            'identity-a',
            100,
            'first-credit',
            'operation-1',
            at='2026-07-20T00:02:00+08:00',
        )

        account = await store.get(
            f'{POINT_ACCOUNT_PREFIX}bot-a:identity-a',
            PointAccount,
        )
        assert account is not None
        assert account.balance == 150

    asyncio.run(scenario())


def test_point_operation_applies_two_users_once() -> None:
    async def scenario() -> None:
        store = GrowthStore(FakeStorage())
        points = PointService(store)
        changes = (
            PointChange('promoter-a', 100, 'promoter-reward', 'promoter-credit'),
            PointChange('invitee-a', 20, 'invitee-reward', 'invitee-credit'),
        )

        first = await points.apply_operation(
            'bot-a',
            'referral-effective:referral-1',
            changes,
            at='2026-07-20T00:00:00+08:00',
        )
        replay = await points.apply_operation(
            'bot-a',
            'referral-effective:referral-1',
            changes,
            at='2026-07-20T00:00:01+08:00',
        )

        assert replay == first
        assert await points.balance('bot-a', 'promoter-a') == 100
        assert await points.balance('bot-a', 'invitee-a') == 20
        entries = await store.list_prefix(f'{POINT_ENTRY_PREFIX}bot-a:', PointEntry)
        assert len(entries.records) == 2

    asyncio.run(scenario())


def test_insufficient_debit_keeps_point_state_unchanged() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        points = PointService(store)

        with pytest.raises(InsufficientPointsError):
            await points.debit(
                'bot-a',
                'identity-a',
                1,
                'redeem',
                'redeem-1',
                at='2026-07-20T00:00:00+08:00',
            )

        assert await points.balance('bot-a', 'identity-a') == 0
        assert not any(
            key.startswith((POINT_ACCOUNT_PREFIX, POINT_ENTRY_PREFIX, GROWTH_OPERATION_PREFIX))
            for key in storage.values
        )

    asyncio.run(scenario())


def test_pending_operation_recovers_after_entry_write_interruption() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        points = PointService(store)
        changes = (
            PointChange('identity-a', 80, 'admin-adjust', 'adjust:identity-a'),
        )
        storage.fail_prefix_once = f'{POINT_ACCOUNT_PREFIX}bot-a:'

        with pytest.raises(RuntimeError, match='interruption'):
            await points.apply_operation(
                'bot-a',
                'adjust:request-1',
                changes,
                at='2026-07-20T00:00:00+08:00',
            )

        pending = await store.list_pending_operations('bot-a')
        entries_before = await store.list_prefix(
            f'{POINT_ENTRY_PREFIX}bot-a:',
            PointEntry,
        )
        assert len(pending.records) == 1
        assert pending.records[0].applied_steps == ()
        assert len(entries_before.records) == 1

        recovered = await points.apply_operation(
            'bot-a',
            'adjust:request-1',
            changes,
            at='2026-07-20T00:00:01+08:00',
        )
        account = await store.get(
            f'{POINT_ACCOUNT_PREFIX}bot-a:identity-a',
            PointAccount,
        )
        operation = await store.get(
            f'{GROWTH_OPERATION_PREFIX}bot-a:adjust:request-1',
            GrowthOperation,
        )

        assert recovered == entries_before.records
        assert account is not None and account.balance == 80
        assert operation is not None and operation.status == 'COMMITTED'
        assert operation.applied_steps == ('adjust:identity-a',)
        entries_after = await store.list_prefix(
            f'{POINT_ENTRY_PREFIX}bot-a:',
            PointEntry,
        )
        assert len(entries_after.records) == 1

    asyncio.run(scenario())

from __future__ import annotations

import asyncio

import pytest

from components.entitlement import (
    EntitlementExpiredError,
    EntitlementNotFoundError,
    EntitlementService,
    EntitlementStorageError,
)
from components.growth_store import ENTITLEMENT_PREFIX, GrowthStore
from components.models import AccountRecord, Credentials


class FakeStorage:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.fail_prefix_once = ''

    async def set_plugin_storage(self, key: str, value: bytes) -> None:
        if self.fail_prefix_once and key.startswith(self.fail_prefix_once):
            self.fail_prefix_once = ''
            raise RuntimeError('simulated entitlement write interruption')
        self.values[key] = value

    async def get_plugin_storage(self, key: str) -> bytes:
        return self.values[key]

    async def get_plugin_storage_keys(self) -> list[str]:
        return list(self.values)

    async def delete_plugin_storage(self, key: str) -> None:
        del self.values[key]


def build_service() -> tuple[FakeStorage, EntitlementService]:
    storage = FakeStorage()
    return storage, EntitlementService(GrowthStore(storage))


def account(bot_uuid: str, sender_id: str) -> AccountRecord:
    return AccountRecord.create(
        bot_uuid=bot_uuid,
        sender_id=sender_id,
        credentials=Credentials(
            token='token',
            device='device',
            version='version',
            version_code='1',
            guest_id='guest',
            client_type='client',
        ),
        target_id='target',
        schedule_time='08:00',
        auto_signin=True,
        auto_resign=True,
        auto_milestone=True,
    )


def test_rollout_at_is_persisted_and_existing_accounts_keep_same_trial_expiry() -> None:
    async def scenario() -> None:
        _, entitlement = build_service()
        accounts = [account('bot-a', 'user-a'), account('bot-a', 'user-b')]

        rollout_at = await entitlement.initialize_existing_accounts(
            'bot-a',
            accounts,
            trial_days=30,
            at='2026-07-20T00:00:00+08:00',
        )
        first = await entitlement.get_status(
            'bot-a',
            'user-a',
            now='2026-07-20T00:00:00+08:00',
        )
        await entitlement.initialize_existing_accounts(
            'bot-a',
            accounts,
            trial_days=90,
            at='2026-08-20T00:00:00+08:00',
        )
        second = await entitlement.get_status(
            'bot-a',
            'user-a',
            now='2026-07-21T00:00:00+08:00',
        )

        assert rollout_at == '2026-07-20T00:00:00+08:00'
        assert first.expires_at == '2026-08-19T00:00:00+08:00'
        assert second.expires_at == first.expires_at

    asyncio.run(scenario())


def test_new_binding_uses_binding_time_and_duplicate_binding_does_not_reset() -> None:
    async def scenario() -> None:
        _, entitlement = build_service()

        first = await entitlement.ensure_for_binding(
            'bot-a',
            'user-a',
            trial_days=30,
            at='2026-07-20T12:00:00+08:00',
        )
        second = await entitlement.ensure_for_binding(
            'bot-a',
            'user-a',
            trial_days=90,
            at='2026-08-20T12:00:00+08:00',
        )

        assert first == second
        assert first.expires_at == '2026-08-19T12:00:00+08:00'

    asyncio.run(scenario())


def test_expired_entitlement_requires_extension_before_business_use() -> None:
    async def scenario() -> None:
        _, entitlement = build_service()
        await entitlement.ensure_for_binding(
            'bot-a',
            'user-a',
            trial_days=30,
            at='2026-07-20T00:00:00+08:00',
        )

        status = await entitlement.get_status(
            'bot-a',
            'user-a',
            now='2026-08-20T00:00:00+08:00',
        )
        with pytest.raises(EntitlementExpiredError):
            await entitlement.require_active(
                'bot-a',
                'user-a',
                now='2026-08-20T00:00:00+08:00',
            )

        assert status.active is False

    asyncio.run(scenario())


def test_missing_entitlement_uses_specific_not_found_storage_error() -> None:
    async def scenario() -> None:
        _, entitlement = build_service()

        with pytest.raises(EntitlementNotFoundError) as status_error:
            await entitlement.get_status('bot-a', 'missing-user')
        with pytest.raises(EntitlementNotFoundError) as extend_error:
            await entitlement.extend(
                'bot-a',
                'missing-user',
                duration_days=30,
            )

        assert isinstance(status_error.value, EntitlementStorageError)
        assert isinstance(extend_error.value, EntitlementStorageError)

    asyncio.run(scenario())


def test_extension_starts_from_now_after_expiry_and_from_existing_expiry_before_expiry() -> None:
    async def scenario() -> None:
        _, entitlement = build_service()
        await entitlement.ensure_for_binding(
            'bot-a',
            'expired-user',
            trial_days=30,
            at='2026-07-01T00:00:00+08:00',
        )
        await entitlement.ensure_for_binding(
            'bot-a',
            'active-user',
            trial_days=30,
            at='2026-07-20T00:00:00+08:00',
        )

        expired = await entitlement.extend(
            'bot-a',
            'expired-user',
            duration_days=10,
            now='2026-08-20T00:00:00+08:00',
        )
        active = await entitlement.extend(
            'bot-a',
            'active-user',
            duration_days=10,
            now='2026-07-21T00:00:00+08:00',
        )

        assert expired.expires_at == '2026-08-30T00:00:00+08:00'
        assert active.expires_at == '2026-08-29T00:00:00+08:00'

    asyncio.run(scenario())


def test_rollout_interruption_leaves_pending_operation_and_retries_without_reset() -> None:
    async def scenario() -> None:
        storage, entitlement = build_service()
        accounts = [account('bot-a', 'user-a'), account('bot-a', 'user-b')]
        storage.fail_prefix_once = f'{ENTITLEMENT_PREFIX}bot-a:'

        with pytest.raises(EntitlementStorageError):
            await entitlement.initialize_existing_accounts(
                'bot-a',
                accounts,
                trial_days=30,
                at='2026-07-20T00:00:00+08:00',
            )

        pending = await entitlement._store.list_pending_operations('bot-a')
        assert len(pending.records) == 1
        await entitlement.initialize_existing_accounts(
            'bot-a',
            accounts,
            trial_days=90,
            at='2026-08-20T00:00:00+08:00',
        )
        status = await entitlement.get_status(
            'bot-a',
            'user-a',
            now='2026-07-21T00:00:00+08:00',
        )

        assert status.expires_at == '2026-08-19T00:00:00+08:00'
        assert (await entitlement._store.list_pending_operations('bot-a')).records == ()

    asyncio.run(scenario())


def test_rollout_step_interruption_recovers_existing_entitlement(monkeypatch) -> None:
    async def scenario() -> None:
        _, entitlement = build_service()
        existing_accounts = [account('bot-a', 'user-a')]
        original_mark_step = entitlement._store.mark_step_applied
        failed = False

        async def fail_mark_once(
            bot_uuid: str,
            operation_id: str,
            step_id: str,
            updated_at: str,
        ):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError('simulated rollout step interruption')
            return await original_mark_step(
                bot_uuid,
                operation_id,
                step_id,
                updated_at,
            )

        monkeypatch.setattr(entitlement._store, 'mark_step_applied', fail_mark_once)

        with pytest.raises(EntitlementStorageError):
            await entitlement.initialize_existing_accounts(
                'bot-a',
                existing_accounts,
                trial_days=30,
                at='2026-07-20T00:00:00+08:00',
            )
        before = await entitlement.get_status(
            'bot-a',
            'user-a',
            now='2026-07-21T00:00:00+08:00',
        )
        await entitlement.initialize_existing_accounts(
            'bot-a',
            existing_accounts,
            trial_days=90,
            at='2026-08-20T00:00:00+08:00',
        )
        after = await entitlement.get_status(
            'bot-a',
            'user-a',
            now='2026-07-21T00:00:00+08:00',
        )

        assert before.expires_at == '2026-08-19T00:00:00+08:00'
        assert after == before
        assert (await entitlement._store.list_pending_operations('bot-a')).records == ()

    asyncio.run(scenario())


def test_missing_entitlement_is_reported_as_storage_error() -> None:
    async def scenario() -> None:
        _, entitlement = build_service()

        with pytest.raises(EntitlementStorageError):
            await entitlement.get_status('bot-a', 'missing-user')

    asyncio.run(scenario())

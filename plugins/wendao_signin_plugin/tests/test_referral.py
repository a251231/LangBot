from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from components.growth_models import GrowthOperation, PointEntry
from components.growth_store import (
    POINT_ENTRY_PREFIX,
    REFERRAL_PREFIX,
    GrowthStore,
    growth_storage_key,
    identity_hash,
)
from components.points import PointService
from components.referral import (
    AlreadyBoundError,
    InviteCodeNotFoundError,
    ReferralError,
    ReferralService,
)


class FakeStorage:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.fail_prefix_once = ''

    async def set_plugin_storage(self, key: str, value: bytes) -> None:
        if self.fail_prefix_once and key.startswith(self.fail_prefix_once):
            self.fail_prefix_once = ''
            raise RuntimeError('simulated referral write interruption')
        self.values[key] = value

    async def get_plugin_storage(self, key: str) -> bytes:
        return self.values[key]

    async def get_plugin_storage_keys(self) -> list[str]:
        return list(self.values)

    async def delete_plugin_storage(self, key: str) -> None:
        del self.values[key]


def build_service(
    code_factory=None,
) -> tuple[FakeStorage, ReferralService, PointService]:
    storage = FakeStorage()
    store = GrowthStore(storage)
    points = PointService(store)
    return storage, ReferralService(store, points, code_factory=code_factory), points


def test_ensure_promoter_has_stable_eight_character_invite_code() -> None:
    async def scenario() -> None:
        storage, referral, _ = build_service()

        first = await referral.ensure_promoter('bot-a', 'promoter-1')
        second = await referral.ensure_promoter('bot-a', 'promoter-1')

        assert first == second
        assert len(first.invite_code) == 8
        assert set(first.invite_code) <= set('23456789ABCDEFGHJKLMNPQRSTUVWXYZ')
        assert 'promoter-1' not in '\n'.join(storage.values.keys())

    asyncio.run(scenario())


def test_invite_code_collision_retries() -> None:
    async def scenario() -> None:
        codes = iter(('23456789', '23456789', 'ABCDEFGH'))
        _, referral, _ = build_service(code_factory=lambda: next(codes))

        first = await referral.ensure_promoter('bot-a', 'promoter-1')
        second = await referral.ensure_promoter('bot-a', 'promoter-2')

        assert first.invite_code == '23456789'
        assert second.invite_code == 'ABCDEFGH'

    asyncio.run(scenario())


def test_first_valid_invite_is_locked_and_bound_users_cannot_register() -> None:
    async def scenario() -> None:
        _, referral, _ = build_service()
        promoter_a = await referral.ensure_promoter('bot-a', 'promoter-a')
        promoter_b = await referral.ensure_promoter('bot-a', 'promoter-b')

        first = await referral.register_invite(
            'bot-a',
            'invitee-a',
            promoter_a.invite_code,
        )
        replay = await referral.register_invite(
            'bot-a',
            'invitee-a',
            promoter_b.invite_code,
        )

        assert replay == first
        with pytest.raises(AlreadyBoundError):
            await referral.register_invite(
                'bot-a',
                'already-bound',
                promoter_a.invite_code,
                already_bound=True,
            )
        with pytest.raises(InviteCodeNotFoundError):
            await referral.register_invite('bot-a', 'invitee-b', '22222222')

    asyncio.run(scenario())


def test_self_invite_is_recorded_as_rejected_without_rewards() -> None:
    async def scenario() -> None:
        _, referral, points = build_service()
        promoter = await referral.ensure_promoter('bot-a', 'promoter-a')

        rejected = await referral.register_invite(
            'bot-a',
            'promoter-a',
            promoter.invite_code,
        )

        assert rejected.status == 'rejected'
        await referral.on_account_bound('bot-a', 'promoter-a', 'user-a')
        assert await points.balance('bot-a', rejected.invitee_hash) == 0

    asyncio.run(scenario())


def test_referral_transitions_pending_bound_effective_and_rewards_both_users() -> None:
    async def scenario() -> None:
        _, referral, points = build_service()
        promoter = await referral.ensure_promoter('bot-a', 'promoter-a')
        pending = await referral.register_invite(
            'bot-a',
            'invitee-a',
            promoter.invite_code,
        )
        bound = await referral.on_account_bound('bot-a', 'invitee-a', 'user-a')
        effective = await referral.on_signin_confirmed(
            'bot-a',
            'invitee-a',
            promoter_reward_points=100,
            invitee_reward_points=20,
            at='2026-07-20T00:00:00+08:00',
        )

        assert pending.status == 'pending'
        assert bound is not None and bound.status == 'bound'
        assert effective is not None and effective.status == 'effective'
        assert await points.balance('bot-a', promoter.identity_hash) == 100
        assert await points.balance('bot-a', pending.invitee_hash) == 20

    asyncio.run(scenario())


def test_same_user_identifier_can_be_effective_only_once() -> None:
    async def scenario() -> None:
        _, referral, points = build_service()
        promoter_a = await referral.ensure_promoter('bot-a', 'promoter-a')
        promoter_b = await referral.ensure_promoter('bot-a', 'promoter-b')
        first = await referral.register_invite(
            'bot-a',
            'invitee-a',
            promoter_a.invite_code,
        )
        second = await referral.register_invite(
            'bot-a',
            'invitee-b',
            promoter_b.invite_code,
        )

        bound_first = await referral.on_account_bound('bot-a', 'invitee-a', 'same-user')
        rejected_second = await referral.on_account_bound(
            'bot-a',
            'invitee-b',
            'same-user',
        )
        await referral.on_signin_confirmed(
            'bot-a',
            'invitee-a',
            promoter_reward_points=100,
            invitee_reward_points=20,
            at='2026-07-20T00:00:00+08:00',
        )

        assert bound_first is not None and bound_first.status == 'bound'
        assert rejected_second.status == 'rejected'
        assert await points.balance('bot-a', first.invitee_hash) == 20
        assert await points.balance('bot-a', second.invitee_hash) == 0

    asyncio.run(scenario())


def test_effective_reward_replay_is_idempotent() -> None:
    async def scenario() -> None:
        _, referral, points = build_service()
        promoter = await referral.ensure_promoter('bot-a', 'promoter-a')
        relation = await referral.register_invite(
            'bot-a',
            'invitee-a',
            promoter.invite_code,
        )
        await referral.on_account_bound('bot-a', 'invitee-a', 'user-a')

        first = await referral.on_signin_confirmed(
            'bot-a',
            'invitee-a',
            promoter_reward_points=100,
            invitee_reward_points=20,
            at='2026-07-20T00:00:00+08:00',
        )
        replay = await referral.on_signin_confirmed(
            'bot-a',
            'invitee-a',
            promoter_reward_points=999,
            invitee_reward_points=999,
            at='2026-07-21T00:00:00+08:00',
        )
        entries = await referral._store.list_prefix(
            f'{POINT_ENTRY_PREFIX}bot-a:',
            PointEntry,
        )

        assert first == replay
        assert await points.balance('bot-a', promoter.identity_hash) == 100
        assert await points.balance('bot-a', relation.invitee_hash) == 20
        assert len(entries.records) == 2

    asyncio.run(scenario())


def test_effective_relation_write_interruption_keeps_operation_recoverable() -> None:
    async def scenario() -> None:
        storage, referral, points = build_service()
        promoter = await referral.ensure_promoter('bot-a', 'promoter-a')
        relation = await referral.register_invite(
            'bot-a',
            'invitee-a',
            promoter.invite_code,
        )
        await referral.on_account_bound('bot-a', 'invitee-a', 'user-a')
        storage.fail_prefix_once = f'{REFERRAL_PREFIX}bot-a:'

        with pytest.raises(RuntimeError, match='interruption'):
            await referral.on_signin_confirmed(
                'bot-a',
                'invitee-a',
                promoter_reward_points=100,
                invitee_reward_points=20,
                at='2026-07-20T00:00:00+08:00',
            )

        pending = await referral._store.list_pending_operations('bot-a')
        assert len(pending.records) == 1
        assert pending.records[0].status == 'PENDING'
        recovered = await referral.on_signin_confirmed(
            'bot-a',
            'invitee-a',
            promoter_reward_points=999,
            invitee_reward_points=999,
            at='2026-07-21T00:00:00+08:00',
        )

        assert recovered is not None and recovered.status == 'effective'
        assert await points.balance('bot-a', promoter.identity_hash) == 100
        assert await points.balance('bot-a', relation.invitee_hash) == 20
        assert (await referral._store.list_pending_operations('bot-a')).records == ()

    asyncio.run(scenario())


def test_effective_operation_commit_interruption_is_recoverable(monkeypatch) -> None:
    async def scenario() -> None:
        _, referral, points = build_service()
        promoter = await referral.ensure_promoter('bot-a', 'promoter-a')
        relation = await referral.register_invite(
            'bot-a',
            'invitee-a',
            promoter.invite_code,
        )
        await referral.on_account_bound('bot-a', 'invitee-a', 'user-a')
        original_commit = referral._store.commit_operation
        failed = False

        async def fail_commit_once(
            bot_uuid: str,
            operation_id: str,
            updated_at: str,
        ):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError('simulated operation commit interruption')
            return await original_commit(bot_uuid, operation_id, updated_at)

        monkeypatch.setattr(referral._store, 'commit_operation', fail_commit_once)

        with pytest.raises(RuntimeError, match='interruption'):
            await referral.on_signin_confirmed(
                'bot-a',
                'invitee-a',
                promoter_reward_points=100,
                invitee_reward_points=20,
                at='2026-07-20T00:00:00+08:00',
            )

        pending = await referral._store.list_pending_operations('bot-a')
        assert len(pending.records) == 1
        assert pending.records[0].applied_steps[-1] == 'referral-effective'
        recovered = await referral.on_signin_confirmed(
            'bot-a',
            'invitee-a',
            promoter_reward_points=999,
            invitee_reward_points=999,
            at='2026-07-21T00:00:00+08:00',
        )

        assert recovered is not None and recovered.status == 'effective'
        assert await points.balance('bot-a', promoter.identity_hash) == 100
        assert await points.balance('bot-a', relation.invitee_hash) == 20
        assert (await referral._store.list_pending_operations('bot-a')).records == ()

    asyncio.run(scenario())


def test_corrupt_referral_records_are_not_silently_ignored() -> None:
    async def scenario() -> None:
        storage, referral, _ = build_service()
        storage.values[
            growth_storage_key(REFERRAL_PREFIX, 'bot-a', 'corrupt')
        ] = b'{not-json'

        with pytest.raises(ReferralError, match='损坏'):
            await referral.stats('bot-a', 'promoter-a')

    asyncio.run(scenario())


def test_promotion_stats_use_status_buckets_and_seven_day_window() -> None:
    async def scenario() -> None:
        _, referral, _ = build_service()
        promoter = await referral.ensure_promoter('bot-a', 'promoter-a')
        await referral.register_invite('bot-a', 'pending-user', promoter.invite_code)
        await referral.register_invite('bot-a', 'bound-user', promoter.invite_code)
        await referral.on_account_bound('bot-a', 'bound-user', 'bound-id')
        await referral.register_invite('bot-a', 'recent-user', promoter.invite_code)
        await referral.on_account_bound('bot-a', 'recent-user', 'recent-id')
        await referral.on_signin_confirmed(
            'bot-a',
            'recent-user',
            promoter_reward_points=0,
            invitee_reward_points=0,
            at='2026-07-19T00:00:00+08:00',
        )
        await referral.register_invite('bot-a', 'future-user', promoter.invite_code)
        await referral.on_account_bound('bot-a', 'future-user', 'future-id')
        await referral.on_signin_confirmed(
            'bot-a',
            'future-user',
            promoter_reward_points=0,
            invitee_reward_points=0,
            at='2026-07-21T00:00:00+08:00',
        )
        rejected = await referral.register_invite(
            'bot-a',
            'promoter-a',
            promoter.invite_code,
        )
        assert rejected.status == 'rejected'

        stats = await referral.stats(
            'bot-a',
            'promoter-a',
            now='2026-07-20T00:00:00+08:00',
        )

        assert stats.registered_count == 4
        assert stats.bound_count == 3
        assert stats.effective_count == 2
        assert stats.recent_effective_count == 1

    asyncio.run(scenario())


def test_recovery_rejects_malformed_invitee_hash_before_writing_points() -> None:
    async def scenario() -> None:
        _, referral, _ = build_service()
        operation = GrowthOperation(
            bot_uuid='bot-a',
            operation_id='referral-effective:not-a-hash',
            kind='points',
            status='PENDING',
            payload={
                'changes': [
                    {
                        'identity_hash': 'target',
                        'amount': 100,
                        'entry_type': 'referral_reward_promoter',
                        'reason': 'not-a-hash',
                        'step_id': 'promoter:not-a-hash',
                    }
                ]
            },
            applied_steps=(),
            created_at='2026-07-20T00:00:00+08:00',
            updated_at='2026-07-20T00:00:00+08:00',
        )
        await referral._store.save(
            growth_storage_key(
                'growth-op:v1:',
                operation.bot_uuid,
                operation.operation_id,
            ),
            operation,
        )

        with pytest.raises(ReferralError, match='格式'):
            await referral.recover_operation(operation)

        entries = await referral._store.list_prefix(
            f'{POINT_ENTRY_PREFIX}bot-a:',
            PointEntry,
        )
        assert entries.records == ()

    asyncio.run(scenario())


def test_recovery_cross_checks_reward_payload_before_writing_points() -> None:
    async def scenario() -> None:
        _, referral, _ = build_service()
        promoter = await referral.ensure_promoter('bot-a', 'promoter-a')
        relation = await referral.register_invite(
            'bot-a',
            'invitee-a',
            promoter.invite_code,
        )
        await referral.on_account_bound('bot-a', 'invitee-a', 'user-a')
        wrong_identity = identity_hash('bot-a', 'other-promoter')
        operation = GrowthOperation(
            bot_uuid='bot-a',
            operation_id=f'referral-effective:{relation.invitee_hash}',
            kind='points',
            status='PENDING',
            payload={
                'changes': [
                    {
                        'identity_hash': wrong_identity,
                        'amount': 100,
                        'entry_type': 'referral_reward_promoter',
                        'reason': relation.invitee_hash,
                        'step_id': f'promoter:{relation.invitee_hash}',
                    }
                ]
            },
            applied_steps=(),
            created_at='2026-07-20T00:00:00+08:00',
            updated_at='2026-07-20T00:00:00+08:00',
        )
        await referral._store.save(
            growth_storage_key(
                'growth-op:v1:',
                operation.bot_uuid,
                operation.operation_id,
            ),
            operation,
        )

        with pytest.raises(ReferralError, match='不一致'):
            await referral.recover_operation(operation)

        entries = await referral._store.list_prefix(
            f'{POINT_ENTRY_PREFIX}bot-a:',
            PointEntry,
        )
        assert entries.records == ()

    asyncio.run(scenario())


def test_recovery_rejects_bound_relation_after_operation_time() -> None:
    async def scenario() -> None:
        _, referral, _ = build_service()
        promoter = await referral.ensure_promoter('bot-a', 'promoter-a')
        relation = await referral.register_invite(
            'bot-a',
            'invitee-a',
            promoter.invite_code,
        )
        await referral.on_account_bound(
            'bot-a',
            'invitee-a',
            'user-a',
            at='2026-07-21T00:00:00+08:00',
        )
        operation = GrowthOperation(
            bot_uuid='bot-a',
            operation_id=f'referral-effective:{relation.invitee_hash}',
            kind='referral-reward-zero',
            status='PENDING',
            payload={
                'referral_id': relation.invitee_hash,
                'promoter_points': 0,
                'invitee_points': 0,
            },
            applied_steps=(),
            created_at='2026-07-20T00:00:00+08:00',
            updated_at='2026-07-20T00:00:00+08:00',
        )
        await referral._store.save(
            growth_storage_key(
                'growth-op:v1:',
                operation.bot_uuid,
                operation.operation_id,
            ),
            operation,
        )

        with pytest.raises(ReferralError, match='时间'):
            await referral.recover_operation(operation)

    asyncio.run(scenario())


def test_recovery_rejects_effective_relation_with_different_effective_time() -> None:
    async def scenario() -> None:
        _, referral, _ = build_service()
        promoter = await referral.ensure_promoter('bot-a', 'promoter-a')
        relation = await referral.register_invite(
            'bot-a',
            'invitee-a',
            promoter.invite_code,
        )
        bound = await referral.on_account_bound(
            'bot-a',
            'invitee-a',
            'user-a',
            at='2026-07-19T00:00:00+08:00',
        )
        assert bound is not None
        effective = replace(
            bound,
            status='effective',
            effective_at='2026-07-21T00:00:00+08:00',
        )
        await referral._store.save(
            growth_storage_key(
                REFERRAL_PREFIX,
                effective.bot_uuid,
                effective.invitee_hash,
            ),
            effective,
        )
        operation = GrowthOperation(
            bot_uuid='bot-a',
            operation_id=f'referral-effective:{relation.invitee_hash}',
            kind='referral-reward-zero',
            status='PENDING',
            payload={
                'referral_id': relation.invitee_hash,
                'promoter_points': 0,
                'invitee_points': 0,
            },
            applied_steps=('referral-effective',),
            created_at='2026-07-20T00:00:00+08:00',
            updated_at='2026-07-20T00:00:00+08:00',
        )
        await referral._store.save(
            growth_storage_key(
                'growth-op:v1:',
                operation.bot_uuid,
                operation.operation_id,
            ),
            operation,
        )

        with pytest.raises(ReferralError, match='时间'):
            await referral.recover_operation(operation)

    asyncio.run(scenario())

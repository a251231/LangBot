from __future__ import annotations

import asyncio
from collections.abc import Iterable

import pytest

from components.growth_models import (
    CardRecord,
    EntitlementRecord,
    GrowthOperation,
    PointAccount,
    PointEntry,
    ProductRecord,
    PromoterRecord,
    RedemptionRecord,
    ReferralRecord,
)
from components.growth_store import (
    PROMOTER_PREFIX,
    GrowthStore,
    deserialize_record,
    growth_storage_key,
    identity_hash,
    serialize_record,
)


class FakeStorage:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set_plugin_storage(self, key: str, value: bytes) -> None:
        self.values[key] = value

    async def get_plugin_storage(self, key: str) -> bytes:
        return self.values[key]

    async def get_plugin_storage_keys(self) -> list[str]:
        return list(self.values)

    async def delete_plugin_storage(self, key: str) -> None:
        del self.values[key]


def _model_samples() -> Iterable[object]:
    yield PromoterRecord(
        bot_uuid='bot-a',
        identity_hash='identity-a',
        invite_code='ABCD2345',
        created_at='2026-07-20T00:00:00+08:00',
    )
    yield ReferralRecord(
        bot_uuid='bot-a',
        invitee_hash='invitee-a',
        promoter_hash='promoter-a',
        invite_code='ABCD2345',
        status='pending',
        created_at='2026-07-20T00:00:00+08:00',
    )
    yield PointAccount(
        bot_uuid='bot-a',
        identity_hash='identity-a',
        balance=120,
        last_entry_id='entry-1',
        updated_at='2026-07-20T00:00:00+08:00',
    )
    yield PointEntry(
        bot_uuid='bot-a',
        entry_id='entry-1',
        identity_hash='identity-a',
        amount=120,
        reason='referral',
        operation_id='operation-1',
        balance_after=120,
        created_at='2026-07-20T00:00:00+08:00',
    )
    yield ProductRecord(
        bot_uuid='bot-a',
        product_id='P000001',
        name='30 天使用期限',
        points_cost=500,
        duration_days=30,
        enabled=True,
        created_at='2026-07-20T00:00:00+08:00',
        updated_at='2026-07-20T00:00:00+08:00',
    )
    yield CardRecord(
        bot_uuid='bot-a',
        card_hash='card-a',
        product_id='P000001',
        product_name='30 天使用期限',
        duration_days=30,
        status='AVAILABLE',
        encrypted_code='ciphertext',
        created_at='2026-07-20T00:00:00+08:00',
    )
    yield RedemptionRecord(
        bot_uuid='bot-a',
        redemption_id='redemption-1',
        identity_hash='identity-a',
        product_id='P000001',
        card_hash='card-a',
        points_cost=500,
        duration_days=30,
        created_at='2026-07-20T00:00:00+08:00',
    )
    yield EntitlementRecord(
        bot_uuid='bot-a',
        identity_hash='identity-a',
        expires_at='2026-08-19T00:00:00+08:00',
        created_at='2026-07-20T00:00:00+08:00',
        updated_at='2026-07-20T00:00:00+08:00',
    )
    yield GrowthOperation(
        bot_uuid='bot-a',
        operation_id='operation-1',
        kind='referral-effective',
        status='PENDING',
        payload={'promoter_points': 100, 'invitee_points': 20},
        applied_steps=('promoter-credit',),
        created_at='2026-07-20T00:00:00+08:00',
        updated_at='2026-07-20T00:00:00+08:00',
    )


@pytest.mark.parametrize('record', list(_model_samples()))
def test_growth_models_round_trip_json(record: object) -> None:
    raw = serialize_record(record)

    restored = deserialize_record(raw, type(record))

    assert restored == record
    assert getattr(restored, 'schema_version') == 1


def test_card_record_deserializes_product_name_snapshot() -> None:
    raw = (
        b'{"bot_uuid":"bot-a","card_hash":"card-a",'
        b'"created_at":"2026-07-20T00:00:00+08:00",'
        b'"duration_days":30,"encrypted_code":"ciphertext",'
        b'"product_id":"P000001","product_name":"30 days access",'
        b'"schema_version":1,"status":"AVAILABLE"}'
    )

    restored = deserialize_record(raw, CardRecord)

    assert restored.product_name == '30 days access'


def test_identity_hash_hides_raw_identity_and_isolates_bots() -> None:
    sender_id = 'ou_sensitive_sender_123'

    first = identity_hash('bot-a', sender_id)
    second = identity_hash('bot-b', sender_id)

    assert sender_id not in first
    assert len(first) == 64
    assert first != second
    assert sender_id not in growth_storage_key(PROMOTER_PREFIX, 'bot-a', first)


def test_growth_store_crud_and_corrupt_record_diagnostics() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        record = PromoterRecord(
            bot_uuid='bot-a',
            identity_hash='identity-a',
            invite_code='ABCD2345',
            created_at='2026-07-20T00:00:00+08:00',
        )
        key = growth_storage_key(PROMOTER_PREFIX, 'bot-a', record.identity_hash)
        other_key = growth_storage_key(PROMOTER_PREFIX, 'bot-b', record.identity_hash)
        corrupt_key = growth_storage_key(PROMOTER_PREFIX, 'bot-a', 'corrupt')

        await store.save(key, record)
        storage.values[other_key] = serialize_record(record)
        storage.values[corrupt_key] = b'{invalid-json'

        assert await store.get(key, PromoterRecord) == record
        listed = await store.list_prefix(f'{PROMOTER_PREFIX}bot-a:', PromoterRecord)
        assert listed.records == (record,)
        assert listed.skipped_count == 1
        assert await store.delete(key) is True
        assert await store.delete(key) is False
        assert await store.get(key, PromoterRecord) is None

    asyncio.run(scenario())


def test_growth_store_skips_records_with_invalid_field_types() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        key = growth_storage_key(PROMOTER_PREFIX, 'bot-a', 'wrong-type')
        storage.values[key] = (
            b'{"bot_uuid":"bot-a","created_at":"2026-07-20",'
            b'"identity_hash":"identity-a","invite_code":123,'
            b'"schema_version":1}'
        )

        result = await store.list_prefix(f'{PROMOTER_PREFIX}bot-a:', PromoterRecord)

        assert result.records == ()
        assert result.skipped_count == 1

    asyncio.run(scenario())


def test_growth_store_skips_records_with_boolean_schema_version() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        key = growth_storage_key(PROMOTER_PREFIX, 'bot-a', 'boolean-version')
        storage.values[key] = (
            b'{"bot_uuid":"bot-a","created_at":"2026-07-20",'
            b'"identity_hash":"identity-a","invite_code":"ABCD2345",'
            b'"schema_version":true}'
        )

        result = await store.list_prefix(f'{PROMOTER_PREFIX}bot-a:', PromoterRecord)

        assert result.records == ()
        assert result.skipped_count == 1

    asyncio.run(scenario())


def test_growth_store_rejects_record_saved_under_another_bot_key() -> None:
    async def scenario() -> None:
        store = GrowthStore(FakeStorage())
        record = PromoterRecord(
            bot_uuid='bot-a',
            identity_hash='identity-a',
            invite_code='ABCD2345',
            created_at='2026-07-20T00:00:00+08:00',
        )
        wrong_key = growth_storage_key(
            PROMOTER_PREFIX,
            'bot-b',
            record.identity_hash,
        )

        with pytest.raises(ValueError, match='bot_uuid'):
            await store.save(wrong_key, record)

    asyncio.run(scenario())


def test_bot_lock_is_stable_and_isolated() -> None:
    store = GrowthStore(FakeStorage())

    assert store.bot_lock('bot-a') is store.bot_lock('bot-a')
    assert store.bot_lock('bot-a') is not store.bot_lock('bot-b')


def test_sharded_index_uses_fixed_500_item_boundary() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        base_key = 'redemption-index:v1:bot-a:identity-a'

        for index in range(501):
            await store.append_sharded_index(base_key, f'redemption-{index:04d}')

        result = await store.read_sharded_index(base_key)
        shard_keys = sorted(
            key for key in storage.values if key.startswith(base_key + ':')
        )

        assert len(shard_keys) == 2
        assert result.items[0] == 'redemption-0000'
        assert result.items[-1] == 'redemption-0500'
        assert len(result.items) == 501
        assert result.shard_count == 2
        assert result.skipped_count == 0

    asyncio.run(scenario())


def test_sharded_index_skips_corrupt_shards_with_diagnostics() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        base_key = 'card-pool:v1:bot-a:P000001'
        storage.values[f'{base_key}:000000'] = b'{invalid-json'
        storage.values[f'{base_key}:000001'] = (
            b'{"schema_version":1,"items":["card-a","card-b"]}'
        )

        result = await store.read_sharded_index(base_key)

        assert result.items == ('card-a', 'card-b')
        assert result.shard_count == 2
        assert result.skipped_count == 1

    asyncio.run(scenario())


def test_sharded_index_skips_shards_with_boolean_schema_version() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        base_key = 'card-pool:v1:bot-a:P000001'
        storage.values[f'{base_key}:000000'] = (
            b'{"schema_version":true,"items":["card-a"]}'
        )

        result = await store.read_sharded_index(base_key)

        assert result.items == ()
        assert result.shard_count == 1
        assert result.skipped_count == 1

    asyncio.run(scenario())


def test_growth_operation_lifecycle_is_idempotent() -> None:
    async def scenario() -> None:
        store = GrowthStore(FakeStorage())
        payload = {'changes': [{'identity_hash': 'identity-a', 'amount': 100}]}

        operation = await store.begin_operation(
            'bot-a',
            'referral-effective:referral-1',
            'points',
            payload,
            '2026-07-20T00:00:00+08:00',
        )
        replay = await store.begin_operation(
            'bot-a',
            'referral-effective:referral-1',
            'points',
            payload,
            '2026-07-20T00:00:01+08:00',
        )
        stepped = await store.mark_step_applied(
            'bot-a',
            operation.operation_id,
            'credit:identity-a',
            '2026-07-20T00:00:02+08:00',
        )
        stepped_again = await store.mark_step_applied(
            'bot-a',
            operation.operation_id,
            'credit:identity-a',
            '2026-07-20T00:00:03+08:00',
        )
        pending = await store.list_pending_operations('bot-a')
        committed = await store.commit_operation(
            'bot-a',
            operation.operation_id,
            '2026-07-20T00:00:04+08:00',
        )

        assert replay == operation
        assert stepped.applied_steps == ('credit:identity-a',)
        assert stepped_again == stepped
        assert pending.records == (stepped,)
        assert committed.status == 'COMMITTED'
        assert (await store.list_pending_operations('bot-a')).records == ()

    asyncio.run(scenario())

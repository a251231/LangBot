from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from dataclasses import replace

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
    CARD_PREFIX,
    ENTITLEMENT_PREFIX,
    GROWTH_OPERATION_PREFIX,
    INVITE_CODE_PREFIX,
    POINT_ACCOUNT_PREFIX,
    POINT_ENTRY_PREFIX,
    PRODUCT_PREFIX,
    PROMOTER_PREFIX,
    REDEMPTION_PREFIX,
    REFERRAL_PREFIX,
    GrowthStore,
    deserialize_record,
    growth_storage_key,
    identity_hash,
    serialize_record,
)
from components.points import PointService


class FakeStorage:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.get_keys_calls = 0
        self.get_errors: dict[str, Exception] = {}
        self.set_errors: dict[str, Exception] = {}

    async def set_plugin_storage(self, key: str, value: bytes) -> None:
        error = self.set_errors.get(key)
        if error is not None:
            raise error
        self.values[key] = value

    async def get_plugin_storage(self, key: str) -> bytes:
        error = self.get_errors.get(key)
        if error is not None:
            raise error
        return self.values[key]

    async def get_plugin_storage_keys(self) -> list[str]:
        self.get_keys_calls += 1
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
        entry_type='referral_reward_promoter',
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
        b'"issued_to_hash":"","issued_at":"",'
        b'"activated_by_hash":"","activated_at":"",'
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


@pytest.mark.parametrize('bot_uuid', ['', 'bot:a', 'bot\tname', 'bot\x7fname'])
def test_growth_storage_key_rejects_ambiguous_bot_uuid(bot_uuid: str) -> None:
    with pytest.raises(ValueError, match='bot_uuid'):
        growth_storage_key(PROMOTER_PREFIX, bot_uuid, 'identity-a')

    assert (
        growth_storage_key(
            GROWTH_OPERATION_PREFIX,
            'bot-a',
            'referral-effective:referral-1',
        )
        == 'growth-op:v1:bot-a:referral-effective:referral-1'
    )


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


def test_growth_store_serializes_first_key_cache_initialization_across_bots() -> None:
    async def scenario() -> None:
        class SnapshotBarrierStorage(FakeStorage):
            def __init__(self) -> None:
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def get_plugin_storage_keys(self) -> list[str]:
                self.get_keys_calls += 1
                snapshot = list(self.values)
                self.started.set()
                await self.release.wait()
                return snapshot

        storage = SnapshotBarrierStorage()
        store = GrowthStore(storage)

        async def save(bot_uuid: str, entry_id: str) -> None:
            entry = PointEntry(
                bot_uuid=bot_uuid,
                entry_id=entry_id,
                identity_hash=f'identity-{bot_uuid}',
                amount=1,
                entry_type='admin_adjustment',
                reason='test',
                operation_id=f'operation-{bot_uuid}',
                balance_after=1,
                created_at='2026-07-20T00:00:00+08:00',
            )
            await store.save(
                growth_storage_key(POINT_ENTRY_PREFIX, bot_uuid, entry_id),
                entry,
            )

        first = asyncio.create_task(save('bot-a', 'entry-a'))
        await storage.started.wait()
        second = asyncio.create_task(save('bot-b', 'entry-b'))
        await asyncio.sleep(0)
        storage.release.set()
        await asyncio.gather(first, second)

        assert await store.get(
            growth_storage_key(POINT_ENTRY_PREFIX, 'bot-a', 'entry-a'),
            PointEntry,
        ) is not None
        assert await store.get(
            growth_storage_key(POINT_ENTRY_PREFIX, 'bot-b', 'entry-b'),
            PointEntry,
        ) is not None
        assert storage.get_keys_calls == 1

    asyncio.run(scenario())


def test_growth_store_propagates_list_record_read_error() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        key = growth_storage_key(PROMOTER_PREFIX, 'bot-a', 'identity-a')
        storage.values[key] = b'{}'
        backend_error = ValueError('backend list read failed')
        storage.get_errors[key] = backend_error

        with pytest.raises(ValueError, match='backend list read failed') as raised:
            await store.list_prefix(f'{PROMOTER_PREFIX}bot-a:', PromoterRecord)

        assert raised.value is backend_error

    asyncio.run(scenario())


def test_growth_store_propagates_append_shard_read_error() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        base_key = 'card-pool:v1:bot-a:P000001'
        shard_key = f'{base_key}:000000'
        storage.values[shard_key] = b'{}'
        backend_error = TypeError('backend append read failed')
        storage.get_errors[shard_key] = backend_error

        with pytest.raises(TypeError, match='backend append read failed') as raised:
            await store.append_sharded_index(base_key, 'card-a')

        assert raised.value is backend_error

    asyncio.run(scenario())


def test_growth_store_propagates_read_shard_storage_error() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        base_key = 'redemption-index:v1:bot-a:identity-a'
        shard_key = f'{base_key}:000000'
        storage.values[shard_key] = b'{}'
        backend_error = ValueError('backend shard read failed')
        storage.get_errors[shard_key] = backend_error

        with pytest.raises(ValueError, match='backend shard read failed') as raised:
            await store.read_sharded_index(base_key)

        assert raised.value is backend_error

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


def test_growth_store_rejects_stored_record_whose_payload_does_not_match_key() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        key = growth_storage_key(PROMOTER_PREFIX, 'bot-a', 'identity-a')
        mismatched = PromoterRecord(
            bot_uuid='bot-b',
            identity_hash='identity-a',
            invite_code='ABCD2345',
            created_at='2026-07-20T00:00:00+08:00',
        )
        storage.values[key] = serialize_record(mismatched)

        with pytest.raises(ValueError, match='存储键不一致'):
            await store.get(key, PromoterRecord)

        listed = await store.list_prefix(f'{PROMOTER_PREFIX}bot-a:', PromoterRecord)
        assert listed.records == ()
        assert listed.skipped_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ('record', 'prefix', 'primary_key'),
    [
        (record, prefix, primary_key)
        for record, prefix, primary_key in zip(
            _model_samples(),
            (
                PROMOTER_PREFIX,
                REFERRAL_PREFIX,
                POINT_ACCOUNT_PREFIX,
                POINT_ENTRY_PREFIX,
                PRODUCT_PREFIX,
                CARD_PREFIX,
                REDEMPTION_PREFIX,
                ENTITLEMENT_PREFIX,
                GROWTH_OPERATION_PREFIX,
            ),
            (
                'identity-a',
                'invitee-a',
                'identity-a',
                'entry-1',
                'P000001',
                'card-a',
                'redemption-1',
                'identity-a',
                'operation-1',
            ),
            strict=True,
        )
    ],
)
def test_growth_store_save_requires_canonical_record_key(
    record: object,
    prefix: str,
    primary_key: str,
) -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        bot_uuid = getattr(record, 'bot_uuid')
        canonical_key = growth_storage_key(prefix, bot_uuid, primary_key)
        wrong_primary_key = growth_storage_key(prefix, bot_uuid, 'wrong-primary')
        wrong_prefix = PRODUCT_PREFIX if prefix == PROMOTER_PREFIX else PROMOTER_PREFIX
        wrong_type_key = growth_storage_key(wrong_prefix, bot_uuid, primary_key)

        await store.save(canonical_key, record)
        with pytest.raises(ValueError, match='存储键不一致'):
            await store.save(wrong_primary_key, record)
        with pytest.raises(ValueError, match='存储键不一致'):
            await store.save(wrong_type_key, record)

        assert list(storage.values) == [canonical_key]

    asyncio.run(scenario())


def test_promoter_record_supports_identity_and_invite_code_keys() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        record = PromoterRecord(
            bot_uuid='bot-a',
            identity_hash='identity-a',
            invite_code='ABCD2345',
            created_at='2026-07-20T00:00:00+08:00',
        )
        identity_key = growth_storage_key(
            PROMOTER_PREFIX,
            record.bot_uuid,
            record.identity_hash,
        )
        invite_code_key = growth_storage_key(
            INVITE_CODE_PREFIX,
            record.bot_uuid,
            record.invite_code,
        )

        await store.save(identity_key, record)
        await store.save(invite_code_key, record)

        assert await store.get(identity_key, PromoterRecord) == record
        assert await store.get(invite_code_key, PromoterRecord) == record
        listed = await store.list_prefix(
            f'{INVITE_CODE_PREFIX}{record.bot_uuid}:',
            PromoterRecord,
        )
        assert listed.records == (record,)
        with pytest.raises(ValueError, match='存储键不一致'):
            await store.save(
                growth_storage_key(
                    INVITE_CODE_PREFIX,
                    record.bot_uuid,
                    'WRONG234',
                ),
                record,
            )

    asyncio.run(scenario())


def test_growth_store_validates_field_types_before_write() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        record = ProductRecord(
            bot_uuid='bot-a',
            product_id='P000001',
            name='30 天使用期限',
            points_cost=True,
            duration_days=30,
            enabled=True,
            created_at='2026-07-20T00:00:00+08:00',
            updated_at='2026-07-20T00:00:00+08:00',
        )
        key = growth_storage_key(PRODUCT_PREFIX, 'bot-a', 'P000001')

        with pytest.raises(ValueError, match='字段类型错误'):
            await store.save(key, record)

        assert storage.values == {}

    asyncio.run(scenario())


def test_point_entries_are_append_only() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        entry = PointEntry(
            bot_uuid='bot-a',
            entry_id='entry-1',
            identity_hash='identity-a',
            amount=100,
            entry_type='referral_reward_promoter',
            reason='referral',
            operation_id='operation-1',
            balance_after=100,
            created_at='2026-07-20T00:00:00+08:00',
        )
        key = growth_storage_key(POINT_ENTRY_PREFIX, 'bot-a', entry.entry_id)

        await store.save(key, entry)
        await store.save(key, entry)
        with pytest.raises(ValueError, match='不可变'):
            await store.save(
                key,
                replace(entry, amount=999, balance_after=999),
            )

        assert await store.get(key, PointEntry) == entry

    asyncio.run(scenario())


def test_pending_operations_reject_ambiguous_bot_uuid() -> None:
    async def scenario() -> None:
        store = GrowthStore(FakeStorage())
        await store.begin_operation(
            'bot-a',
            'scope:operation-1',
            'points',
            {'changes': []},
            '2026-07-20T00:00:00+08:00',
        )

        with pytest.raises(ValueError, match='bot_uuid'):
            await store.list_pending_operations('bot-a:scope')

    asyncio.run(scenario())


def test_sharded_index_rejects_non_string_item_without_writing() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        base_key = 'card-pool:v1:bot-a:P000001'

        with pytest.raises(ValueError, match='索引项'):
            await store.append_sharded_index(base_key, 123)  # type: ignore[arg-type]

        assert storage.values == {}

    asyncio.run(scenario())


def test_point_entry_delete_is_rejected_and_committed_replay_survives() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        points = PointService(store)
        entry = await points.credit(
            'bot-a',
            'identity-a',
            100,
            'referral_reward_promoter',
            'operation-1',
            at='2026-07-20T00:00:00+08:00',
        )
        entry_key = growth_storage_key(POINT_ENTRY_PREFIX, 'bot-a', entry.entry_id)
        before = dict(storage.values)

        with pytest.raises(ValueError, match='不可删除'):
            await store.delete(entry_key)

        assert storage.values == before
        replay = await points.credit(
            'bot-a',
            'identity-a',
            100,
            'referral_reward_promoter',
            'operation-1',
            at='2026-07-20T00:01:00+08:00',
        )
        assert replay == entry

    asyncio.run(scenario())


def test_deserialize_record_rejects_unknown_fields() -> None:
    raw = (
        b'{"bot_uuid":"bot-a","created_at":"2026-07-20",'
        b'"identity_hash":"identity-a","invite_code":"ABCD2345",'
        b'"schema_version":1,"unexpected":true}'
    )

    with pytest.raises(ValueError, match='格式错误'):
        deserialize_record(raw, PromoterRecord)


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


def test_sharded_index_deduplicates_items_across_all_shards() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        base_key = 'redemption-index:v1:bot-a:identity-a'

        for index in range(501):
            await store.append_sharded_index(base_key, f'redemption-{index:04d}')

        original_shard = await store.append_sharded_index(
            base_key,
            'redemption-0000',
        )
        result = await store.read_sharded_index(base_key)

        assert original_shard == 0
        assert len(result.items) == 501
        assert result.items.count('redemption-0000') == 1

    asyncio.run(scenario())


def test_sharded_index_scans_storage_keys_once_per_base_key() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        base_key = 'redemption-index:v1:bot-a:identity-a'

        for index in range(100):
            await store.append_sharded_index(base_key, f'redemption-{index:04d}')

        assert storage.get_keys_calls <= 1

    asyncio.run(scenario())


def test_sharded_index_scans_storage_keys_once_across_base_keys() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)

        for index in range(1000):
            base_key = f'redemption-index:v1:bot-a:identity-{index:04d}'
            await store.append_sharded_index(base_key, f'redemption-{index:04d}')

        assert storage.get_keys_calls <= 1

    asyncio.run(scenario())


def test_sharded_index_cache_updates_only_after_successful_write() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        base_key = 'card-pool:v1:bot-a:P000001'
        shard_key = f'{base_key}:000000'
        storage.set_errors[shard_key] = RuntimeError('backend write failed')

        with pytest.raises(RuntimeError, match='backend write failed'):
            await store.append_sharded_index(base_key, 'card-a')

        del storage.set_errors[shard_key]
        assert await store.append_sharded_index(base_key, 'card-a') == 0
        assert (await store.read_sharded_index(base_key)).items == ('card-a',)

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


def test_sharded_index_rejects_empty_and_duplicate_items_with_diagnostics() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        base_key = 'card-pool:v1:bot-a:P000001'
        storage.values[f'{base_key}:000000'] = (
            b'{"schema_version":1,"items":["card-a","card-a",""]}'
        )
        storage.values[f'{base_key}:000001'] = (
            b'{"schema_version":1,"items":["card-b","card-c"]}'
        )
        storage.values[f'{base_key}:000002'] = (
            b'{"schema_version":1,"items":["card-c","card-d"]}'
        )

        result = await store.read_sharded_index(base_key)

        assert result.items == ('card-b', 'card-c', 'card-d')
        assert result.shard_count == 3
        assert result.skipped_count == 2

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


def test_sharded_index_rejects_unknown_fields() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        base_key = 'card-pool:v1:bot-a:P000001'
        storage.values[f'{base_key}:000000'] = (
            b'{"schema_version":1,"items":["card-a"],"unexpected":true}'
        )

        result = await store.read_sharded_index(base_key)

        assert result.items == ()
        assert result.shard_count == 1
        assert result.skipped_count == 1

    asyncio.run(scenario())


def test_sharded_index_does_not_overflow_six_digit_shard_space() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        store = GrowthStore(storage)
        base_key = 'card-pool:v1:bot-a:P000001'
        last_shard_key = f'{base_key}:999999'
        storage.values[last_shard_key] = json.dumps(
            {
                'schema_version': 1,
                'items': [f'card-{index:04d}' for index in range(500)],
            },
            separators=(',', ':'),
        ).encode()
        before = dict(storage.values)

        with pytest.raises(OverflowError, match='分片'):
            await store.append_sharded_index(base_key, 'card-overflow')

        assert storage.values == before
        assert f'{base_key}:1000000' not in storage.values

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

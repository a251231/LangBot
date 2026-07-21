from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib

import pytest
from cryptography.fernet import Fernet

from components.commerce import (
    CardUnavailableError,
    CommerceStorageError,
    CommerceService,
    InventoryEmptyError,
    ProductNotFoundError,
    ProductUnavailableError,
)
from components.entitlement import EntitlementService, EntitlementStorageError
from components.growth_models import CardRecord, GrowthOperation, ProductRecord
from components.growth_store import (
    CARD_PREFIX,
    ENTITLEMENT_PREFIX,
    GROWTH_OPERATION_PREFIX,
    GROWTH_SECRET_PREFIX,
    PRODUCT_PREFIX,
    REDEMPTION_PREFIX,
    GrowthStore,
    growth_storage_key,
)
from components.points import InsufficientPointsError, PointService


class FakeStorage:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.fail_prefix_once = ''
        self.fail_prefix_after_matches: tuple[str, int] | None = None

    async def set_plugin_storage(self, key: str, value: bytes) -> None:
        if self.fail_prefix_once and key.startswith(self.fail_prefix_once):
            self.fail_prefix_once = ''
            raise RuntimeError('simulated commerce write interruption')
        if self.fail_prefix_after_matches is not None:
            prefix, remaining = self.fail_prefix_after_matches
            if key.startswith(prefix):
                if remaining == 0:
                    self.fail_prefix_after_matches = None
                    raise RuntimeError('simulated commerce write interruption')
                self.fail_prefix_after_matches = (prefix, remaining - 1)
        self.values[key] = value

    async def get_plugin_storage(self, key: str) -> bytes:
        return self.values[key]

    async def get_plugin_storage_keys(self) -> list[str]:
        return list(self.values)

    async def delete_plugin_storage(self, key: str) -> None:
        del self.values[key]


def card_code(character: str) -> str:
    return f'WD-{character * 43}'


def build_services(
    *,
    codes: tuple[str, ...] = (),
) -> tuple[
    FakeStorage,
    GrowthStore,
    PointService,
    EntitlementService,
    CommerceService,
]:
    storage = FakeStorage()
    store = GrowthStore(storage)
    points = PointService(store)
    entitlement = EntitlementService(store)
    code_iterator = iter(codes)
    commerce = CommerceService(
        store,
        points,
        entitlement,
        card_code_factory=(lambda: next(code_iterator)) if codes else None,
        secret_key_factory=Fernet.generate_key,
    )
    return storage, store, points, entitlement, commerce


def restart_services(
    storage: FakeStorage,
) -> tuple[GrowthStore, PointService, EntitlementService, CommerceService]:
    store = GrowthStore(storage)
    points = PointService(store)
    entitlement = EntitlementService(store)
    return store, points, entitlement, CommerceService(store, points, entitlement)


async def create_enabled_product(
    commerce: CommerceService,
    *,
    name: str = '30 天使用期限',
    points_cost: int = 500,
    duration_days: int = 30,
) -> ProductRecord:
    product = await commerce.create_product(
        'bot-a',
        name,
        points_cost,
        duration_days,
        at='2026-07-20T00:00:00+08:00',
    )
    return await commerce.set_enabled(
        'bot-a',
        product.product_id,
        True,
        request_id='enable-product',
        at='2026-07-20T00:01:00+08:00',
    )


def test_product_creation_listing_toggle_and_input_bounds() -> None:
    async def scenario() -> None:
        _, _, _, _, commerce = build_services()

        first = await commerce.create_product(
            'bot-a',
            ' 30 天使用期限 ',
            500,
            30,
            at='2026-07-20T00:00:00+08:00',
        )
        second = await commerce.create_product(
            'bot-a',
            '90 天使用期限',
            1200,
            90,
            at='2026-07-20T00:01:00+08:00',
        )
        enabled = await commerce.set_enabled(
            'bot-a',
            first.product_id,
            True,
            request_id='enable-first',
            at='2026-07-20T00:02:00+08:00',
        )
        listed = await commerce.list_products('bot-a')

        assert first.product_id == 'P000001'
        assert first.name == '30 天使用期限'
        assert first.enabled is False
        assert second.product_id == 'P000002'
        assert enabled.enabled is True
        assert [(item.product.product_id, item.available_stock) for item in listed] == [
            ('P000001', 0),
            ('P000002', 0),
        ]
        with pytest.raises(ValueError):
            await commerce.create_product('bot-a', '', 500, 30)
        with pytest.raises(ValueError):
            await commerce.create_product('bot-a', 'x' * 51, 500, 30)
        with pytest.raises(ValueError):
            await commerce.create_product('bot-a', 'test', 0, 30)
        with pytest.raises(ValueError):
            await commerce.create_product('bot-a', 'test', 1_000_001, 30)
        with pytest.raises(ValueError):
            await commerce.create_product('bot-a', 'test', 500, 0)
        with pytest.raises(ValueError):
            await commerce.create_product('bot-a', 'test', 500, 3651)
        with pytest.raises(ProductNotFoundError):
            await commerce.set_enabled(
                'bot-a',
                'P999999',
                True,
                request_id='missing-product',
            )

    asyncio.run(scenario())


def test_product_creation_request_recovers_without_duplicate_after_interruption() -> None:
    async def scenario() -> None:
        storage, store, _, _, commerce = build_services()
        storage.fail_prefix_once = f'{PRODUCT_PREFIX}bot-a:'

        with pytest.raises(RuntimeError, match='interruption'):
            await commerce.create_product(
                'bot-a',
                '30 天使用期限',
                500,
                30,
                request_id='product-create-1',
                at='2026-07-20T00:00:00+08:00',
            )

        pending = await store.list_pending_operations('bot-a')
        assert len(pending.records) == 1
        assert pending.records[0].kind == 'product-create'

        _, _, _, restarted = restart_services(storage)
        await restarted.recover_operation(pending.records[0])
        replay = await restarted.create_product(
            'bot-a',
            '30 天使用期限',
            500,
            30,
            request_id='product-create-1',
            at='2026-07-21T00:00:00+08:00',
        )
        listed = await restarted.list_products('bot-a')

        assert replay.product_id == 'P000001'
        assert [item.product.product_id for item in listed] == ['P000001']
        assert (await store.list_pending_operations('bot-a')).records == ()

    asyncio.run(scenario())


def test_product_id_allocation_reserves_pending_product_creation() -> None:
    async def scenario() -> None:
        storage, store, _, _, commerce = build_services()
        storage.fail_prefix_once = f'{PRODUCT_PREFIX}bot-a:'

        with pytest.raises(RuntimeError, match='interruption'):
            await commerce.create_product(
                'bot-a',
                'interrupted product',
                500,
                30,
                request_id='product-create-a',
                at='2026-07-20T00:00:00+08:00',
            )

        second = await commerce.create_product(
            'bot-a',
            'later product',
            800,
            60,
            request_id='product-create-b',
            at='2026-07-20T00:01:00+08:00',
        )
        [pending] = (await store.list_pending_operations('bot-a')).records

        assert pending.payload['product_id'] == 'P000001'
        assert second.product_id == 'P000002'

        _, _, _, restarted = restart_services(storage)
        await restarted.recover_operation(pending)

        assert [
            item.product.product_id
            for item in await restarted.list_products('bot-a')
        ] == ['P000001', 'P000002']

    asyncio.run(scenario())


def test_product_id_allocation_rejects_pending_reservation_collision() -> None:
    async def scenario() -> None:
        _, store, _, _, commerce = build_services()
        existing = await commerce.create_product(
            'bot-a',
            'existing product',
            500,
            30,
            at='2026-07-20T00:00:00+08:00',
        )
        operation_id = 'product-create:bot-a:' + hashlib.sha256(
            b'corrupt-reservation'
        ).hexdigest()
        await store.begin_operation(
            'bot-a',
            operation_id,
            'product-create',
            {
                'product_id': existing.product_id,
                'name': 'different product',
                'points_cost': 800,
                'duration_days': 60,
                'created_at': '2026-07-20T00:01:00+08:00',
            },
            '2026-07-20T00:01:00+08:00',
        )

        with pytest.raises(CommerceStorageError, match='预留'):
            await commerce.create_product(
                'bot-a',
                'later product',
                1000,
                90,
                request_id='later-product',
                at='2026-07-20T00:02:00+08:00',
            )

        products = await commerce.list_products('bot-a')
        assert [item.product.name for item in products] == ['existing product']

    asyncio.run(scenario())


def test_product_status_request_recovers_older_pending_before_new_change() -> None:
    async def scenario() -> None:
        storage, store, _, _, commerce = build_services()
        product = await commerce.create_product(
            'bot-a',
            '30 day product',
            500,
            30,
            at='2026-07-20T00:00:00+08:00',
        )
        storage.fail_prefix_after_matches = (
            f'{GROWTH_OPERATION_PREFIX}bot-a:',
            1,
        )

        with pytest.raises(RuntimeError, match='interruption'):
            await commerce.set_enabled(
                'bot-a',
                product.product_id,
                True,
                request_id='state-old',
                at='2026-07-20T00:01:00+08:00',
            )

        changed = await commerce.set_enabled(
            'bot-a',
            product.product_id,
            False,
            request_id='state-new',
            at='2026-07-20T00:02:00+08:00',
        )
        replay = await commerce.set_enabled(
            'bot-a',
            product.product_id,
            True,
            request_id='state-old',
            at='2026-07-20T00:03:00+08:00',
        )

        assert changed.enabled is False
        assert replay.enabled is True
        assert (await store.list_pending_operations('bot-a')).records == ()
        assert (await commerce.list_products('bot-a'))[0].product.enabled is False

    asyncio.run(scenario())


def test_recover_old_pending_product_state_does_not_overwrite_newer_fact() -> None:
    async def scenario() -> None:
        storage, store, _, _, commerce = build_services()
        product = await commerce.create_product(
            'bot-a',
            '30 day product',
            500,
            30,
            at='2026-07-20T00:00:00+08:00',
        )
        old_id = 'admin-product-enabled:' + hashlib.sha256(
            b'state-old'
        ).hexdigest()
        newer_id = 'admin-product-enabled:' + hashlib.sha256(
            b'state-new'
        ).hexdigest()
        old = await store.begin_operation(
            'bot-a',
            old_id,
            'admin-product-enabled',
            {'product_id': product.product_id, 'enabled': True},
            '2026-07-20T00:01:00+08:00',
        )
        await store.save(
            growth_storage_key(PRODUCT_PREFIX, 'bot-a', product.product_id),
            replace(
                product,
                enabled=True,
                updated_at='2026-07-20T00:01:00+08:00',
            ),
        )
        await store.begin_operation(
            'bot-a',
            newer_id,
            'admin-product-enabled',
            {'product_id': product.product_id, 'enabled': False},
            '2026-07-20T00:02:00+08:00',
        )
        await store.save(
            growth_storage_key(PRODUCT_PREFIX, 'bot-a', product.product_id),
            replace(
                product,
                enabled=False,
                updated_at='2026-07-20T00:02:00+08:00',
            ),
        )
        await store.mark_step_applied(
            'bot-a',
            newer_id,
            'target-saved',
            '2026-07-20T00:02:00+08:00',
        )
        await store.commit_operation(
            'bot-a',
            newer_id,
            '2026-07-20T00:02:00+08:00',
        )

        _, _, _, restarted = restart_services(storage)
        await restarted.recover_operation(old)

        assert (await restarted.list_products('bot-a'))[0].product.enabled is False
        recovered = await store.get_operation('bot-a', old_id)
        assert recovered is not None
        assert recovered.status == 'COMMITTED'
        assert recovered.applied_steps == ('target-saved',)

    asyncio.run(scenario())


def test_product_status_equal_time_conflict_fails_and_new_times_are_monotonic() -> None:
    async def scenario() -> None:
        storage, store, _, _, commerce = build_services()
        timestamp = '2026-07-20T00:00:00+08:00'
        product = await commerce.create_product(
            'bot-a',
            'equal-time product',
            500,
            30,
            at=timestamp,
        )
        conflict_id = 'admin-product-enabled:' + hashlib.sha256(
            b'equal-time-conflict'
        ).hexdigest()
        conflict = await store.begin_operation(
            'bot-a',
            conflict_id,
            'admin-product-enabled',
            {'product_id': product.product_id, 'enabled': True},
            timestamp,
        )
        product_key = growth_storage_key(PRODUCT_PREFIX, 'bot-a', product.product_id)
        before_product = storage.values[product_key]

        with pytest.raises(CommerceStorageError, match='时间冲突'):
            await commerce.recover_operation(conflict)

        assert storage.values[product_key] == before_product
        assert await store.get_operation('bot-a', conflict_id) == conflict

        _, monotonic_store, _, _, monotonic = build_services()
        monotonic_product = await monotonic.create_product(
            'bot-a',
            'monotonic product',
            500,
            30,
            at=timestamp,
        )
        first = await monotonic.set_enabled(
            'bot-a',
            monotonic_product.product_id,
            True,
            request_id='monotonic-first',
            at=timestamp,
        )
        first_id = 'admin-product-enabled:' + hashlib.sha256(
            b'monotonic-first'
        ).hexdigest()
        first_operation = await monotonic_store.get_operation('bot-a', first_id)
        assert first_operation is not None
        assert datetime.fromisoformat(first_operation.created_at) > (
            datetime.fromisoformat(timestamp)
        )
        assert first.updated_at == first_operation.created_at

        second = await monotonic.set_enabled(
            'bot-a',
            monotonic_product.product_id,
            False,
            request_id='monotonic-second',
            at='2026-07-19T23:59:59+08:00',
        )
        second_id = 'admin-product-enabled:' + hashlib.sha256(
            b'monotonic-second'
        ).hexdigest()
        second_operation = await monotonic_store.get_operation('bot-a', second_id)
        assert second_operation is not None
        assert datetime.fromisoformat(second_operation.created_at) > (
            datetime.fromisoformat(first_operation.created_at)
        )
        assert second.enabled is False

        replay = await monotonic.set_enabled(
            'bot-a',
            monotonic_product.product_id,
            True,
            request_id='monotonic-first',
            at='2026-07-21T00:00:00+08:00',
        )
        replayed_operation = await monotonic_store.get_operation('bot-a', first_id)
        assert replay.enabled is True
        assert replayed_operation == first_operation
        assert (await monotonic.list_products('bot-a'))[0].product.enabled is False

    asyncio.run(scenario())


def test_product_create_recovery_rejects_operation_payload_time_mismatch() -> None:
    async def scenario() -> None:
        _, store, _, _, commerce = build_services()
        operation_id = 'product-create:bot-a:' + hashlib.sha256(
            b'product-time-mismatch'
        ).hexdigest()
        operation = await store.begin_operation(
            'bot-a',
            operation_id,
            'product-create',
            {
                'product_id': 'P000001',
                'name': 'mismatched time product',
                'points_cost': 500,
                'duration_days': 30,
                'created_at': '2026-07-20T00:00:00+08:00',
            },
            '2026-07-20T00:00:01+08:00',
        )
        product_key = growth_storage_key(PRODUCT_PREFIX, 'bot-a', 'P000001')

        with pytest.raises(ValueError):
            await commerce.recover_operation(operation)

        assert await store.get(product_key, ProductRecord) is None
        assert await store.get_operation('bot-a', operation_id) == operation

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ('operation_id', 'applied_steps'),
    (
        ('admin-product-enabled:not-a-canonical-hash', ()),
        ('admin-product-enabled:' + 'f' * 64, ('unexpected-step',)),
    ),
)
def test_product_status_recovery_rejects_invalid_audit_fields_without_fact_change(
    operation_id: str,
    applied_steps: tuple[str, ...],
) -> None:
    async def scenario() -> None:
        storage, store, _, _, commerce = build_services()
        product = await commerce.create_product(
            'bot-a',
            '30 day product',
            500,
            30,
            at='2026-07-20T00:00:00+08:00',
        )
        product_key = growth_storage_key(PRODUCT_PREFIX, 'bot-a', product.product_id)
        before_product = storage.values[product_key]
        operation = GrowthOperation(
            bot_uuid='bot-a',
            operation_id=operation_id,
            kind='admin-product-enabled',
            status='PENDING',
            payload={'product_id': product.product_id, 'enabled': True},
            applied_steps=applied_steps,
            created_at='2026-07-20T00:01:00+08:00',
            updated_at='2026-07-20T00:01:00+08:00',
        )
        await store.save(
            growth_storage_key(GROWTH_OPERATION_PREFIX, 'bot-a', operation_id),
            operation,
        )

        with pytest.raises(CommerceStorageError, match='商品状态操作'):
            await commerce.recover_operation(operation)

        current = (await commerce.list_products('bot-a'))[0].product
        assert current.enabled is False
        assert (await store.list_pending_operations('bot-a')).records == (operation,)
        assert storage.values[product_key] == before_product

    asyncio.run(scenario())


def test_inventory_uses_encrypted_codes_hashed_keys_and_persistent_secret() -> None:
    async def scenario() -> None:
        code_a = card_code('A')
        code_b = card_code('B')
        storage, _, _, _, commerce = build_services(
            codes=(code_a, code_b),
        )
        product = await create_enabled_product(commerce)

        result = await commerce.add_inventory(
            'bot-a',
            product.product_id,
            2,
            request_id='inventory-1',
            at='2026-07-20T00:02:00+08:00',
        )
        _, restarted_points, _, restarted = restart_services(storage)
        listed = await restarted.list_products('bot-a')
        identity = restarted.identity_for('bot-a', 'user-a')
        await restarted_points.adjust(
            'bot-a',
            identity,
            1000,
            'test credit',
            'admin-credit-1',
        )
        redeemed = await restarted.redeem(
            'bot-a',
            'user-a',
            product.product_id,
            request_id='redeem-1',
        )

        assert result.added_count == 2
        assert listed[0].available_stock == 2
        assert redeemed.card_code in {code_a, code_b}
        serialized = b'\n'.join(storage.values.values())
        keys = '\n'.join(storage.values)
        assert code_a.encode() not in serialized
        assert code_b.encode() not in serialized
        assert code_a not in keys
        assert code_b not in keys

    asyncio.run(scenario())


def test_inventory_request_replay_is_idempotent_and_quantity_is_bounded() -> None:
    async def scenario() -> None:
        _, _, _, _, commerce = build_services(codes=(card_code('A'),))
        product = await create_enabled_product(commerce)

        first = await commerce.add_inventory(
            'bot-a',
            product.product_id,
            1,
            request_id='inventory-1',
        )
        replay = await commerce.add_inventory(
            'bot-a',
            product.product_id,
            1,
            request_id='inventory-1',
        )

        assert replay == first
        assert (await commerce.list_products('bot-a'))[0].available_stock == 1
        other = await commerce.create_product('bot-a', 'other', 100, 10)
        with pytest.raises(ValueError, match='请求 ID'):
            await commerce.add_inventory(
                'bot-a',
                other.product_id,
                1,
                request_id='inventory-1',
            )
        with pytest.raises(ValueError, match='请求 ID'):
            await commerce.add_inventory(
                'bot-a',
                product.product_id,
                2,
                request_id='inventory-1',
            )
        with pytest.raises(ValueError):
            await commerce.add_inventory(
                'bot-a',
                product.product_id,
                1,
                request_id='',
            )
        with pytest.raises(ValueError):
            await commerce.add_inventory(
                'bot-a',
                product.product_id,
                0,
                request_id='bad-count',
            )
        with pytest.raises(ValueError):
            await commerce.add_inventory(
                'bot-a',
                product.product_id,
                1001,
                request_id='bad-count',
            )

    asyncio.run(scenario())


def test_redeem_debits_once_issues_card_and_can_be_queried_again() -> None:
    async def scenario() -> None:
        code = card_code('A')
        storage, _, points, _, commerce = build_services(codes=(code,))
        product = await create_enabled_product(commerce)
        await commerce.add_inventory(
            'bot-a',
            product.product_id,
            1,
            request_id='inventory-1',
        )
        await points.adjust(
            'bot-a',
            commerce.identity_for('bot-a', 'user-a'),
            1000,
            'test credit',
            'admin-credit-1',
        )

        redeemed = await commerce.redeem(
            'bot-a',
            'user-a',
            product.product_id,
            request_id='redeem-1',
            at='2026-07-20T01:00:00+08:00',
        )
        replay = await commerce.redeem(
            'bot-a',
            'user-a',
            product.product_id,
            request_id='redeem-1',
            at='2026-07-21T01:00:00+08:00',
        )
        history = await commerce.list_redemptions('bot-a', 'user-a')

        assert redeemed.card_code == code
        assert replay.card_code == code
        assert redeemed.redemption == replay.redemption
        assert history[0].card_code == code
        assert history[0].status == 'ISSUED'
        assert await points.balance(
            'bot-a',
            commerce.identity_for('bot-a', 'user-a'),
        ) == 500
        assert (await commerce.list_products('bot-a'))[0].available_stock == 0
        operation_keys = [
            key for key in storage.values
            if key.startswith(GROWTH_OPERATION_PREFIX)
            and ':redeem:' in key
        ]
        assert len(operation_keys) == 1
        assert f':redeem:bot-a:{commerce.identity_for("bot-a", "user-a")}:' in (
            operation_keys[0]
        )
        assert 'user-a' not in operation_keys[0]
        assert 'redeem-1' not in operation_keys[0]

    asyncio.run(scenario())


def test_failed_redeem_keeps_points_and_inventory_unchanged() -> None:
    async def scenario() -> None:
        _, _, points, _, commerce = build_services(codes=(card_code('A'),))
        product = await create_enabled_product(commerce)
        identity = commerce.identity_for('bot-a', 'user-a')
        await points.adjust('bot-a', identity, 400, 'test credit', 'admin-credit-1')
        await commerce.add_inventory(
            'bot-a',
            product.product_id,
            1,
            request_id='inventory-1',
        )

        with pytest.raises(InsufficientPointsError):
            await commerce.redeem(
                'bot-a',
                'user-a',
                product.product_id,
                request_id='redeem-insufficient',
            )
        assert await points.balance('bot-a', identity) == 400
        assert (await commerce.list_products('bot-a'))[0].available_stock == 1

        await commerce.set_enabled(
            'bot-a',
            product.product_id,
            False,
            request_id='disable-product',
        )
        with pytest.raises(ProductUnavailableError):
            await commerce.redeem(
                'bot-a',
                'user-a',
                product.product_id,
                request_id='redeem-disabled',
            )
        assert await points.balance('bot-a', identity) == 400

        await commerce.set_enabled(
            'bot-a',
            product.product_id,
            True,
            request_id='reenable-product',
        )
        other = await commerce.create_product('bot-a', 'empty', 1, 1)
        await commerce.set_enabled(
            'bot-a',
            other.product_id,
            True,
            request_id='enable-other',
        )
        with pytest.raises(InventoryEmptyError):
            await commerce.redeem(
                'bot-a',
                'user-a',
                other.product_id,
                request_id='redeem-empty',
            )

    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ('ciphertext', 'missing-secret', 'hash-mismatch'))
def test_redeem_crypto_preflight_failure_keeps_points_and_card_available(
    damage: str,
) -> None:
    async def scenario() -> None:
        code_a = card_code('A')
        storage, store, points, _, commerce = build_services(codes=(code_a,))
        product = await create_enabled_product(commerce)
        identity = commerce.identity_for('bot-a', 'user-a')
        await points.adjust('bot-a', identity, 1000, 'test credit', 'credit-a')
        await commerce.add_inventory(
            'bot-a',
            product.product_id,
            1,
            request_id='inventory-1',
        )
        cards = await store.list_prefix(f'{CARD_PREFIX}bot-a:', CardRecord)
        [card] = cards.records
        if damage == 'ciphertext':
            await store.save(
                growth_storage_key(CARD_PREFIX, 'bot-a', card.card_hash),
                replace(card, encrypted_code='corrupt-token'),
            )
        elif damage == 'missing-secret':
            await store.delete(
                growth_storage_key(
                    GROWTH_SECRET_PREFIX,
                    'bot-a',
                    'card-encryption',
                )
            )
        else:
            key = await store.get_secret('bot-a', 'card-encryption')
            assert key is not None
            wrong_ciphertext = Fernet(key).encrypt(card_code('B').encode()).decode()
            await store.save(
                growth_storage_key(CARD_PREFIX, 'bot-a', card.card_hash),
                replace(card, encrypted_code=wrong_ciphertext),
            )

        with pytest.raises(CommerceStorageError):
            await commerce.redeem(
                'bot-a',
                'user-a',
                product.product_id,
                request_id=f'redeem-{damage}',
            )

        current = await store.get(
            growth_storage_key(CARD_PREFIX, 'bot-a', card.card_hash),
            CardRecord,
        )
        assert current is not None and current.status == 'AVAILABLE'
        assert await points.balance('bot-a', identity) == 1000
        assert (await store.list_pending_operations('bot-a')).records == ()
        if damage == 'missing-secret':
            assert not any(
                key.startswith(f'{GROWTH_SECRET_PREFIX}bot-a:')
                for key in storage.values
            )

    asyncio.run(scenario())


def test_redeem_interruption_recovers_without_duplicate_debit_or_card() -> None:
    async def scenario() -> None:
        code = card_code('A')
        storage, _, points, _, commerce = build_services(codes=(code,))
        product = await create_enabled_product(commerce)
        identity = commerce.identity_for('bot-a', 'user-a')
        await points.adjust('bot-a', identity, 1000, 'test credit', 'admin-credit-1')
        await commerce.add_inventory(
            'bot-a',
            product.product_id,
            1,
            request_id='inventory-1',
        )
        storage.fail_prefix_once = f'{REDEMPTION_PREFIX}bot-a:'

        with pytest.raises(RuntimeError, match='interruption'):
            await commerce.redeem(
                'bot-a',
                'user-a',
                product.product_id,
                request_id='redeem-1',
                at='2026-07-20T01:00:00+08:00',
            )

        _, restarted_points, _, restarted = restart_services(storage)
        pending = await restarted._store.list_pending_operations('bot-a')
        assert len(pending.records) == 1
        await restarted.recover_operation(pending.records[0])
        recovered = await restarted.list_redemptions('bot-a', 'user-a')

        assert recovered[0].card_code == code
        assert await restarted_points.balance('bot-a', identity) == 500
        assert len(recovered) == 1
        assert (await restarted._store.list_pending_operations('bot-a')).records == ()

    asyncio.run(scenario())


def test_pending_redeem_reserves_card_until_interrupted_issue_recovers() -> None:
    async def scenario() -> None:
        code_a = card_code('A')
        code_b = card_code('B')
        storage, _, points, _, commerce = build_services(codes=(code_a, code_b))
        product = await create_enabled_product(commerce)
        identity_a = commerce.identity_for('bot-a', 'user-a')
        identity_b = commerce.identity_for('bot-a', 'user-b')
        await points.adjust('bot-a', identity_a, 1000, 'test credit', 'credit-a')
        await points.adjust('bot-a', identity_b, 1000, 'test credit', 'credit-b')
        await commerce.add_inventory(
            'bot-a',
            product.product_id,
            2,
            request_id='inventory-1',
        )
        storage.fail_prefix_once = f'{CARD_PREFIX}bot-a:'

        with pytest.raises(RuntimeError, match='interruption'):
            await commerce.redeem(
                'bot-a',
                'user-a',
                product.product_id,
                request_id='redeem-a',
            )
        redeemed_b = await commerce.redeem(
            'bot-a',
            'user-b',
            product.product_id,
            request_id='redeem-b',
        )
        recovered_a = await commerce.redeem(
            'bot-a',
            'user-a',
            product.product_id,
            request_id='redeem-a',
        )

        assert {redeemed_b.card_code, recovered_a.card_code} == {code_a, code_b}
        assert redeemed_b.redemption.card_hash != recovered_a.redemption.card_hash
        assert await points.balance('bot-a', identity_a) == 500
        assert await points.balance('bot-a', identity_b) == 500
        assert (await commerce._store.list_pending_operations('bot-a')).records == ()

    asyncio.run(scenario())


def test_restart_loads_pending_card_reservations_before_new_redeem() -> None:
    async def scenario() -> None:
        storage, _, points, _, commerce = build_services(codes=(card_code('A'),))
        product = await create_enabled_product(commerce)
        identity_a = commerce.identity_for('bot-a', 'user-a')
        identity_b = commerce.identity_for('bot-a', 'user-b')
        await points.adjust('bot-a', identity_a, 1000, 'test credit', 'credit-a')
        await points.adjust('bot-a', identity_b, 1000, 'test credit', 'credit-b')
        await commerce.add_inventory(
            'bot-a',
            product.product_id,
            1,
            request_id='inventory-1',
        )
        storage.fail_prefix_once = f'{CARD_PREFIX}bot-a:'

        with pytest.raises(RuntimeError, match='interruption'):
            await commerce.redeem(
                'bot-a',
                'user-a',
                product.product_id,
                request_id='redeem-a',
            )

        _, restarted_points, _, restarted = restart_services(storage)
        with pytest.raises(InventoryEmptyError):
            await restarted.redeem(
                'bot-a',
                'user-b',
                product.product_id,
                request_id='redeem-b',
            )
        assert await restarted_points.balance('bot-a', identity_b) == 1000

        pending = await restarted._store.list_pending_operations('bot-a')
        [redeem_operation] = [
            operation for operation in pending.records
            if operation.kind == 'redeem'
        ]
        await restarted.recover_operation(redeem_operation)

        assert await restarted_points.balance('bot-a', identity_a) == 500
        assert await restarted_points.balance('bot-a', identity_b) == 1000
        assert (await restarted._store.list_pending_operations('bot-a')).records == ()

    asyncio.run(scenario())


def test_hot_shop_paths_do_not_scan_operation_history_or_load_historical_cards(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        _, _, points, _, commerce = build_services(
            codes=(card_code('A'), card_code('B')),
        )
        product = await create_enabled_product(commerce)
        identity = commerce.identity_for('bot-a', 'user-a')
        await points.adjust('bot-a', identity, 2000, 'test credit', 'credit-a')
        await commerce.add_inventory(
            'bot-a',
            product.product_id,
            2,
            request_id='inventory-1',
        )
        await commerce.redeem(
            'bot-a',
            'user-a',
            product.product_id,
            request_id='redeem-1',
        )

        async def fail_operation_scan(_bot_uuid: str):
            raise AssertionError('hot redeem path scanned all operation records')

        original_get = commerce._store.get

        async def reject_historical_card_load(key, record_type):
            if record_type is CardRecord:
                raise AssertionError('shop listing loaded individual card history')
            return await original_get(key, record_type)

        monkeypatch.setattr(
            commerce._store,
            'list_pending_operations',
            fail_operation_scan,
        )
        monkeypatch.setattr(commerce._store, 'get', reject_historical_card_load)
        listed = await commerce.list_products('bot-a')
        assert listed[0].available_stock == 1

        monkeypatch.setattr(commerce._store, 'get', original_get)
        second = await commerce.redeem(
            'bot-a',
            'user-a',
            product.product_id,
            request_id='redeem-2',
        )
        assert second.card_code == card_code('B')

    asyncio.run(scenario())


def test_recover_rejects_type_valid_but_inconsistent_redeem_payload() -> None:
    async def scenario() -> None:
        _, store, points, _, commerce = build_services(codes=(card_code('A'),))
        product = await create_enabled_product(commerce)
        await commerce.add_inventory(
            'bot-a',
            product.product_id,
            1,
            request_id='inventory-1',
        )
        identity = commerce.identity_for('bot-a', 'user-a')
        await points.adjust('bot-a', identity, 1000, 'test credit', 'credit-a')
        [card] = (
            await store.list_prefix(f'{CARD_PREFIX}bot-a:', CardRecord)
        ).records
        request_hash = hashlib.sha256(b'malformed-redeem').hexdigest()
        operation_id = f'redeem:bot-a:{identity}:{request_hash}'
        operation = await store.begin_operation(
            'bot-a',
            operation_id,
            'redeem',
            {
                'identity_hash': identity,
                'product_id': product.product_id,
                'card_hash': card.card_hash,
                'redemption_id': hashlib.sha256(
                    f'bot-a\0{operation_id}'.encode()
                ).hexdigest(),
                'points_cost': product.points_cost,
                'duration_days': card.duration_days,
                'created_at': '2026-07-20T01:00:00+08:00',
                'changes': [
                    {
                        'identity_hash': identity,
                        'amount': -(product.points_cost - 1),
                        'entry_type': 'redeem_debit',
                        'step_id': 'points-debited',
                        'reason': product.product_id,
                    }
                ],
            },
            '2026-07-20T01:00:00+08:00',
        )

        with pytest.raises(CommerceStorageError):
            await commerce.recover_operation(operation)

        assert await points.balance('bot-a', identity) == 1000
        current = await store.get(
            growth_storage_key(CARD_PREFIX, 'bot-a', card.card_hash),
            CardRecord,
        )
        assert current is not None and current.status == 'AVAILABLE'
        assert (await store.get_operation('bot-a', operation_id)).status == 'PENDING'

    asyncio.run(scenario())


def test_recover_rejects_inventory_card_from_different_product() -> None:
    async def scenario() -> None:
        _, store, _, _, commerce = build_services(codes=(card_code('A'),))
        first = await create_enabled_product(commerce)
        second = await commerce.create_product('bot-a', 'other', 100, 10)
        await commerce.add_inventory(
            'bot-a',
            first.product_id,
            1,
            request_id='inventory-1',
        )
        original_id = commerce._inventory_operation_id('bot-a', 'inventory-1')
        original = await store.get_operation('bot-a', original_id)
        assert original is not None
        cards = [dict(item) for item in original.payload['cards']]
        bad_id = commerce._inventory_operation_id('bot-a', 'bad-inventory')
        bad = await store.begin_operation(
            'bot-a',
            bad_id,
            'inventory-add',
            {
                'product_id': second.product_id,
                'quantity': 1,
                'cards': cards,
            },
            original.created_at,
        )
        keys_before = set(store._storage_keys or ())

        with pytest.raises(CommerceStorageError):
            await commerce.recover_operation(bad)

        assert set(store._storage_keys or ()) == keys_before
        assert (await store.get_operation('bot-a', bad_id)).status == 'PENDING'

    asyncio.run(scenario())


def test_recover_rejects_activation_operation_id_that_mismatches_card_hash() -> None:
    async def scenario() -> None:
        _, store, points, entitlement, commerce = build_services(codes=(card_code('A'),))
        product = await create_enabled_product(commerce)
        identity = commerce.identity_for('bot-a', 'user-a')
        await points.adjust('bot-a', identity, 1000, 'test credit', 'credit-a')
        await entitlement.ensure_for_binding(
            'bot-a',
            'user-a',
            trial_days=30,
            at='2026-07-20T00:00:00+08:00',
        )
        await commerce.add_inventory(
            'bot-a',
            product.product_id,
            1,
            request_id='inventory-1',
        )
        redeemed = await commerce.redeem(
            'bot-a',
            'user-a',
            product.product_id,
            request_id='redeem-1',
        )
        expires_before = (
            await entitlement.get_status('bot-a', 'user-a')
        ).expires_at
        bad_id = f'activate:{"0" * 64}'
        bad = await store.begin_operation(
            'bot-a',
            bad_id,
            'activate',
            {
                'card_hash': redeemed.redemption.card_hash,
                'identity_hash': identity,
                'base_expires_at': expires_before,
                'duration_days': 30,
                'target_expires_at': '2026-09-18T00:00:00+08:00',
                'activated_at': '2026-07-21T00:00:00+08:00',
            },
            '2026-07-21T00:00:00+08:00',
        )

        with pytest.raises(CommerceStorageError):
            await commerce.recover_operation(bad)

        assert (await entitlement.get_status('bot-a', 'user-a')).expires_at == (
            expires_before
        )
        card = await store.get(
            growth_storage_key(
                CARD_PREFIX,
                'bot-a',
                redeemed.redemption.card_hash,
            ),
            CardRecord,
        )
        assert card is not None and card.status == 'ISSUED'
        assert (await store.get_operation('bot-a', bad_id)).status == 'PENDING'

    asyncio.run(scenario())


def test_recover_rejects_activation_target_expanded_beyond_card_duration() -> None:
    async def scenario() -> None:
        _, store, points, entitlement, commerce = build_services(codes=(card_code('A'),))
        product = await create_enabled_product(commerce)
        identity = commerce.identity_for('bot-a', 'user-a')
        await points.adjust('bot-a', identity, 1000, 'test credit', 'credit-a')
        await entitlement.ensure_for_binding(
            'bot-a',
            'user-a',
            trial_days=30,
            at='2026-07-20T00:00:00+08:00',
        )
        await commerce.add_inventory(
            'bot-a',
            product.product_id,
            1,
            request_id='inventory-1',
        )
        redeemed = await commerce.redeem(
            'bot-a',
            'user-a',
            product.product_id,
            request_id='redeem-1',
        )
        expires_before = (
            await entitlement.get_status('bot-a', 'user-a')
        ).expires_at
        operation_id = f'activate:{redeemed.redemption.card_hash}'
        operation = await store.begin_operation(
            'bot-a',
            operation_id,
            'activate',
            {
                'card_hash': redeemed.redemption.card_hash,
                'identity_hash': identity,
                'base_expires_at': expires_before,
                'duration_days': 30,
                'target_expires_at': '2099-12-31T00:00:00+08:00',
                'activated_at': '2026-07-21T00:00:00+08:00',
            },
            '2026-07-21T00:00:00+08:00',
        )

        with pytest.raises(CommerceStorageError):
            await commerce.recover_operation(operation)

        assert (await entitlement.get_status('bot-a', 'user-a')).expires_at == (
            expires_before
        )
        card = await store.get(
            growth_storage_key(
                CARD_PREFIX,
                'bot-a',
                redeemed.redemption.card_hash,
            ),
            CardRecord,
        )
        assert card is not None and card.status == 'ISSUED'

    asyncio.run(scenario())


def test_activation_snapshot_mismatch_is_rejected_before_entitlement_write() -> None:
    async def scenario() -> None:
        _, store, points, entitlement, commerce = build_services(codes=(card_code('A'),))
        product = await create_enabled_product(commerce)
        identity = commerce.identity_for('bot-a', 'user-a')
        await points.adjust('bot-a', identity, 1000, 'test credit', 'credit-a')
        await entitlement.ensure_for_binding(
            'bot-a',
            'user-a',
            trial_days=30,
            at='2026-07-20T00:00:00+08:00',
        )
        await commerce.add_inventory(
            'bot-a',
            product.product_id,
            1,
            request_id='inventory-1',
        )
        redeemed = await commerce.redeem(
            'bot-a',
            'user-a',
            product.product_id,
            request_id='redeem-1',
        )
        expires_before = (
            await entitlement.get_status('bot-a', 'user-a')
        ).expires_at
        target = (
            datetime.fromisoformat(expires_before) + timedelta(days=3650)
        ).isoformat()
        operation_id = f'activate:{redeemed.redemption.card_hash}'
        operation = await store.begin_operation(
            'bot-a',
            operation_id,
            'activate',
            {
                'card_hash': redeemed.redemption.card_hash,
                'identity_hash': identity,
                'base_expires_at': expires_before,
                'duration_days': 3650,
                'target_expires_at': target,
                'activated_at': '2026-07-21T00:00:00+08:00',
            },
            '2026-07-21T00:00:00+08:00',
        )

        with pytest.raises(CommerceStorageError):
            await commerce.recover_operation(operation)

        assert (await entitlement.get_status('bot-a', 'user-a')).expires_at == (
            expires_before
        )
        card = await store.get(
            growth_storage_key(
                CARD_PREFIX,
                'bot-a',
                redeemed.redemption.card_hash,
            ),
            CardRecord,
        )
        assert card is not None and card.status == 'ISSUED'
        assert (await store.get_operation('bot-a', operation_id)).status == 'PENDING'

    asyncio.run(scenario())


def test_inventory_interruption_is_recovered_from_operation_payload_after_restart() -> None:
    async def scenario() -> None:
        storage, _, _, _, commerce = build_services(codes=(card_code('A'),))
        product = await create_enabled_product(commerce)
        storage.fail_prefix_once = f'{CARD_PREFIX}bot-a:'

        with pytest.raises(RuntimeError, match='interruption'):
            await commerce.add_inventory(
                'bot-a',
                product.product_id,
                1,
                request_id='inventory-1',
            )

        _, _, _, restarted = restart_services(storage)
        pending = await restarted._store.list_pending_operations('bot-a')
        assert len(pending.records) == 1
        await restarted.recover_operation(pending.records[0])

        assert (await restarted.list_products('bot-a'))[0].available_stock == 1
        assert (await restarted._store.list_pending_operations('bot-a')).records == ()

    asyncio.run(scenario())


def test_inventory_recovery_never_regresses_card_already_issued(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        _, store, points, _, commerce = build_services(codes=(card_code('A'),))
        product = await create_enabled_product(commerce)
        original_mark_step = store.mark_step_applied
        failed = False

        async def fail_inventory_mark_once(
            bot_uuid: str,
            operation_id: str,
            step_id: str,
            updated_at: str,
        ):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError('simulated inventory step interruption')
            return await original_mark_step(
                bot_uuid,
                operation_id,
                step_id,
                updated_at,
            )

        monkeypatch.setattr(store, 'mark_step_applied', fail_inventory_mark_once)

        with pytest.raises(RuntimeError, match='interruption'):
            await commerce.add_inventory(
                'bot-a',
                product.product_id,
                1,
                request_id='inventory-1',
            )
        identity = commerce.identity_for('bot-a', 'user-a')
        await points.adjust('bot-a', identity, 1000, 'test credit', 'credit-a')
        redeemed = await commerce.redeem(
            'bot-a',
            'user-a',
            product.product_id,
            request_id='redeem-1',
        )
        pending = await store.list_pending_operations('bot-a')
        [inventory_operation] = [
            operation for operation in pending.records
            if operation.kind == 'inventory-add'
        ]

        await commerce.recover_operation(inventory_operation)

        card = await store.get(
            growth_storage_key(
                CARD_PREFIX,
                'bot-a',
                redeemed.redemption.card_hash,
            ),
            CardRecord,
        )
        assert card is not None and card.status == 'ISSUED'
        assert (await commerce.list_products('bot-a'))[0].available_stock == 0
        assert (await store.list_pending_operations('bot-a')).records == ()

    asyncio.run(scenario())


def test_cross_user_activation_extends_once_and_hides_activated_code_from_history() -> None:
    async def scenario() -> None:
        code = card_code('A')
        _, _, points, entitlement, commerce = build_services(codes=(code,))
        product = await create_enabled_product(commerce)
        redeemer = commerce.identity_for('bot-a', 'user-a')
        await points.adjust('bot-a', redeemer, 1000, 'test credit', 'admin-credit-1')
        await entitlement.ensure_for_binding(
            'bot-a',
            'user-b',
            trial_days=30,
            at='2026-07-20T00:00:00+08:00',
        )
        await commerce.add_inventory(
            'bot-a',
            product.product_id,
            1,
            request_id='inventory-1',
        )
        redeemed = await commerce.redeem(
            'bot-a',
            'user-a',
            product.product_id,
            request_id='redeem-1',
        )

        first = await commerce.activate(
            'bot-a',
            'user-b',
            redeemed.card_code,
            at='2026-07-21T00:00:00+08:00',
        )
        replay = await commerce.activate(
            'bot-a',
            'user-b',
            redeemed.card_code,
            at='2026-07-22T00:00:00+08:00',
        )
        history = await commerce.list_redemptions('bot-a', 'user-a')

        assert first.already_activated is False
        assert first.expires_at == '2026-09-18T00:00:00+08:00'
        assert replay.already_activated is True
        assert replay.expires_at == first.expires_at
        assert history[0].status == 'ACTIVATED'
        assert history[0].card_code == ''

    asyncio.run(scenario())


def test_activation_interruption_uses_fixed_target_expiry_on_retry() -> None:
    async def scenario() -> None:
        code = card_code('A')
        storage, _, points, entitlement, commerce = build_services(codes=(code,))
        product = await create_enabled_product(commerce)
        identity = commerce.identity_for('bot-a', 'user-a')
        await points.adjust('bot-a', identity, 1000, 'test credit', 'admin-credit-1')
        await entitlement.ensure_for_binding(
            'bot-a',
            'user-a',
            trial_days=30,
            at='2026-07-20T00:00:00+08:00',
        )
        await commerce.add_inventory(
            'bot-a',
            product.product_id,
            1,
            request_id='inventory-1',
        )
        redeemed = await commerce.redeem(
            'bot-a',
            'user-a',
            product.product_id,
            request_id='redeem-1',
        )
        storage.fail_prefix_once = f'{CARD_PREFIX}bot-a:'

        with pytest.raises(RuntimeError, match='interruption'):
            await commerce.activate(
                'bot-a',
                'user-a',
                redeemed.card_code,
                at='2026-07-21T00:00:00+08:00',
            )
        _, _, restarted_entitlement, restarted = restart_services(storage)
        pending = await restarted._store.list_pending_operations('bot-a')
        assert len(pending.records) == 1
        await restarted.recover_operation(pending.records[0])
        recovered = await restarted_entitlement.get_status(
            'bot-a',
            'user-a',
            now='2026-07-22T00:00:00+08:00',
        )

        assert recovered.expires_at == '2026-09-18T00:00:00+08:00'
        assert (await restarted._store.list_pending_operations('bot-a')).records == ()

    asyncio.run(scenario())


def test_new_activation_recovers_older_pending_card_before_calculating_expiry() -> None:
    async def scenario() -> None:
        code_a = card_code('A')
        code_b = card_code('B')
        storage, _, points, entitlement, commerce = build_services(
            codes=(code_a, code_b),
        )
        product = await create_enabled_product(commerce)
        identity = commerce.identity_for('bot-a', 'user-a')
        await points.adjust('bot-a', identity, 2000, 'test credit', 'credit-a')
        await entitlement.ensure_for_binding(
            'bot-a',
            'user-a',
            trial_days=30,
            at='2026-07-20T00:00:00+08:00',
        )
        await commerce.add_inventory(
            'bot-a',
            product.product_id,
            2,
            request_id='inventory-1',
        )
        first_card = await commerce.redeem(
            'bot-a',
            'user-a',
            product.product_id,
            request_id='redeem-1',
        )
        second_card = await commerce.redeem(
            'bot-a',
            'user-a',
            product.product_id,
            request_id='redeem-2',
        )
        storage.fail_prefix_once = f'{ENTITLEMENT_PREFIX}bot-a:'

        with pytest.raises(EntitlementStorageError):
            await commerce.activate(
                'bot-a',
                'user-a',
                first_card.card_code,
                at='2026-07-21T00:00:00+08:00',
            )
        activated = await commerce.activate(
            'bot-a',
            'user-a',
            second_card.card_code,
            at='2026-07-22T00:00:00+08:00',
        )

        assert activated.expires_at == '2026-10-18T00:00:00+08:00'
        assert (await commerce._store.list_pending_operations('bot-a')).records == ()

    asyncio.run(scenario())


def test_generated_card_keeps_product_snapshot_after_product_changes() -> None:
    async def scenario() -> None:
        code = card_code('A')
        _, store, points, entitlement, commerce = build_services(codes=(code,))
        product = await create_enabled_product(
            commerce,
            name='original',
            points_cost=500,
            duration_days=30,
        )
        await commerce.add_inventory(
            'bot-a',
            product.product_id,
            1,
            request_id='inventory-1',
        )
        changed = replace(product, name='changed', duration_days=365, updated_at='later')
        await store.save(
            growth_storage_key(PRODUCT_PREFIX, 'bot-a', product.product_id),
            changed,
        )
        identity = commerce.identity_for('bot-a', 'user-a')
        await points.adjust('bot-a', identity, 1000, 'test credit', 'admin-credit-1')
        await entitlement.ensure_for_binding(
            'bot-a',
            'user-a',
            trial_days=30,
            at='2026-07-20T00:00:00+08:00',
        )

        redeemed = await commerce.redeem(
            'bot-a',
            'user-a',
            product.product_id,
            request_id='redeem-1',
        )
        activated = await commerce.activate(
            'bot-a',
            'user-a',
            redeemed.card_code,
            at='2026-07-21T00:00:00+08:00',
        )

        card = await store.get(
            growth_storage_key(CARD_PREFIX, 'bot-a', redeemed.redemption.card_hash),
            CardRecord,
        )
        assert card is not None
        assert card.product_name == 'original'
        assert card.duration_days == 30
        assert redeemed.redemption.duration_days == 30
        assert activated.expires_at == '2026-09-18T00:00:00+08:00'

    asyncio.run(scenario())


def test_invalid_or_unissued_card_cannot_be_activated() -> None:
    async def scenario() -> None:
        code = card_code('A')
        _, _, _, entitlement, commerce = build_services(codes=(code,))
        product = await create_enabled_product(commerce)
        await entitlement.ensure_for_binding('bot-a', 'user-a', trial_days=30)
        await commerce.add_inventory(
            'bot-a',
            product.product_id,
            1,
            request_id='inventory-1',
        )

        with pytest.raises(CardUnavailableError):
            await commerce.activate('bot-a', 'user-a', code)
        with pytest.raises(ValueError):
            await commerce.activate('bot-a', 'user-a', 'bad-code')

    asyncio.run(scenario())

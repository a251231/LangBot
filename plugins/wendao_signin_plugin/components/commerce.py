from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast

from cryptography.fernet import Fernet, InvalidToken

from components.entitlement import EntitlementService
from components.growth_models import (
    CardRecord,
    GrowthOperation,
    ProductRecord,
    RedemptionRecord,
)
from components.growth_store import (
    CARD_POOL_PREFIX,
    CARD_PREFIX,
    PRODUCT_PREFIX,
    REDEMPTION_INDEX_PREFIX,
    REDEMPTION_PREFIX,
    GrowthStore,
    growth_storage_key,
    identity_hash,
    strictly_later_timestamp,
)
from components.points import PointChange, PointService


_CARD_PATTERN = re.compile(r'WD-[A-Za-z0-9_-]{43}\Z')
_HASH_PATTERN = re.compile(r'[0-9a-f]{64}\Z')
_CARD_SECRET_NAME = 'card-encryption'


class CommerceError(ValueError):
    pass


class ProductNotFoundError(CommerceError):
    pass


class ProductUnavailableError(CommerceError):
    pass


class InventoryEmptyError(CommerceError):
    pass


class CardUnavailableError(CommerceError):
    pass


class CommerceStorageError(CommerceError):
    pass


@dataclass(frozen=True, slots=True)
class ProductStock:
    product: ProductRecord
    available_stock: int


@dataclass(frozen=True, slots=True)
class InventoryResult:
    product_id: str
    added_count: int


@dataclass(frozen=True, slots=True)
class RedemptionResult:
    redemption: RedemptionRecord
    card_code: str


@dataclass(frozen=True, slots=True)
class RedemptionView:
    redemption: RedemptionRecord
    card_code: str
    status: str


@dataclass(frozen=True, slots=True)
class ActivationResult:
    card_hash: str
    expires_at: str
    already_activated: bool


class CommerceService:
    def __init__(
        self,
        store: GrowthStore,
        points: PointService,
        entitlement: EntitlementService,
        *,
        card_code_factory: Callable[[], str] | None = None,
        secret_key_factory: Callable[[], bytes] | None = None,
    ) -> None:
        self._store = store
        self._points = points
        self._entitlement = entitlement
        self._card_code_factory = card_code_factory or self._generate_card_code
        self._secret_key_factory = secret_key_factory or Fernet.generate_key
        self._reserved_card_hashes: dict[str, dict[str, str]] = {}
        self._pending_activation_ids: dict[str, set[str]] = {}
        self._loaded_reservation_bots: set[str] = set()

    @staticmethod
    def identity_for(bot_uuid: str, sender_id: str) -> str:
        return identity_hash(bot_uuid, sender_id)

    async def create_product(
        self,
        bot_uuid: str,
        name: str,
        points_cost: int,
        duration_days: int,
        *,
        request_id: str | None = None,
        at: str | None = None,
    ) -> ProductRecord:
        normalized_name = self._validate_product(name, points_cost, duration_days)
        timestamp = at or _now_iso()
        _parse_time(timestamp)
        if request_id is not None:
            self._validate_request_id(request_id)
        async with self._store.bot_lock(bot_uuid):
            if request_id is not None:
                operation_id = self._product_operation_id(bot_uuid, request_id)
                operation = await self._store.get_operation(bot_uuid, operation_id)
                if operation is None:
                    product_id = await self._next_product_id_unlocked(bot_uuid)
                    operation = await self._store.begin_operation(
                        bot_uuid,
                        operation_id,
                        'product-create',
                        {
                            'product_id': product_id,
                            'name': normalized_name,
                            'points_cost': points_cost,
                            'duration_days': duration_days,
                            'created_at': timestamp,
                        },
                        timestamp,
                    )
                payload = self._product_payload(operation)
                if (
                    payload['name'] != normalized_name
                    or payload['points_cost'] != points_cost
                    or payload['duration_days'] != duration_days
                ):
                    raise CommerceError('请求 ID 已用于其他商品新增请求。')
                return await self._complete_product_create_unlocked(operation)

            product_id = await self._next_product_id_unlocked(bot_uuid)
            product = ProductRecord(
                bot_uuid=bot_uuid,
                product_id=product_id,
                name=normalized_name,
                points_cost=points_cost,
                duration_days=duration_days,
                enabled=False,
                created_at=timestamp,
                updated_at=timestamp,
            )
            await self._save_product(product)
            return product

    async def _next_product_id_unlocked(self, bot_uuid: str) -> str:
        products = await self._load_products_unlocked(bot_uuid)
        pending = await self._store.list_pending_operations(bot_uuid)
        if pending.skipped_count:
            raise CommerceStorageError('增长操作日志包含损坏记录。')
        reserved_products = tuple(
            self._product_from_operation(operation)
            for operation in pending.records
            if operation.kind == 'product-create'
        )
        reserved_product_ids = tuple(
            product.product_id for product in reserved_products
        )
        if len(set(reserved_product_ids)) != len(reserved_product_ids):
            raise CommerceStorageError('待恢复商品新增操作包含重复商品预留。')
        existing_by_id = {product.product_id: product for product in products}
        for reserved_product in reserved_products:
            existing = existing_by_id.get(reserved_product.product_id)
            if existing is not None and not self._same_product_creation(
                existing,
                reserved_product,
            ):
                raise CommerceStorageError('待恢复商品新增操作预留冲突。')
        product_ids = tuple(product.product_id for product in products) + (
            reserved_product_ids
        )
        product_number = max(
            (
                int(product_id[1:])
                for product_id in product_ids
                if re.fullmatch(r'P\d{6}', product_id)
            ),
            default=0,
        ) + 1
        if product_number > 999999:
            raise OverflowError('商品 ID 已达到六位上限。')
        return f'P{product_number:06d}'

    async def set_enabled(
        self,
        bot_uuid: str,
        product_id: str,
        enabled: bool,
        *,
        request_id: str,
        at: str | None = None,
    ) -> ProductRecord:
        if type(enabled) is not bool:
            raise ValueError('商品状态必须是布尔值。')
        self._validate_request_id(request_id)
        timestamp = at or _now_iso()
        _parse_time(timestamp)
        operation_id = self._product_enabled_operation_id(request_id)
        async with self._store.bot_lock(bot_uuid):
            product = await self._load_product(bot_uuid, product_id)
            if product is None:
                raise ProductNotFoundError('商品不存在。')
            operation = await self._store.get_operation(bot_uuid, operation_id)
            if operation is None:
                await self._recover_pending_product_states_unlocked(bot_uuid)
                product = await self._load_product(bot_uuid, product_id)
                if product is None:
                    raise ProductNotFoundError('商品不存在。')
                timestamp = strictly_later_timestamp(
                    timestamp,
                    product.updated_at,
                )
                operation = await self._store.begin_operation(
                    bot_uuid,
                    operation_id,
                    'admin-product-enabled',
                    {'product_id': product.product_id, 'enabled': enabled},
                    timestamp,
                )
            else:
                payload = self._product_enabled_payload(operation)
                if (
                    payload['product_id'] != product_id
                    or payload['enabled'] is not enabled
                ):
                    raise CommerceError('请求 ID 已用于其他商品状态请求。')
            return await self._complete_product_enabled_unlocked(operation)

    async def add_inventory(
        self,
        bot_uuid: str,
        product_id: str,
        quantity: int,
        *,
        request_id: str,
        at: str | None = None,
    ) -> InventoryResult:
        self._validate_request_id(request_id)
        if type(quantity) is not int or not 1 <= quantity <= 1000:
            raise ValueError('单次增加库存数量必须在 1 至 1000 之间。')
        operation_id = self._inventory_operation_id(bot_uuid, request_id)
        async with self._store.bot_lock(bot_uuid):
            operation = await self._store.get_operation(bot_uuid, operation_id)
            if operation is None:
                product = await self._load_product(bot_uuid, product_id)
                if product is None:
                    raise ProductNotFoundError('商品不存在。')
                timestamp = at or _now_iso()
                _parse_time(timestamp)
                fernet = await self._fernet_unlocked(
                    bot_uuid,
                    create_if_missing=True,
                )
                cards = await self._create_card_payloads(
                    bot_uuid,
                    product,
                    quantity,
                    timestamp,
                    fernet,
                )
                operation = await self._store.begin_operation(
                    bot_uuid,
                    operation_id,
                    'inventory-add',
                    {
                        'product_id': product.product_id,
                        'quantity': quantity,
                        'cards': cards,
                    },
                    timestamp,
                )
            stored_product_id, cards = self._inventory_payload(operation)
            if stored_product_id != product_id or len(cards) != quantity:
                raise CommerceError('请求 ID 已用于其他库存请求。')
            return await self._complete_inventory_unlocked(operation)

    async def list_products(self, bot_uuid: str) -> tuple[ProductStock, ...]:
        async with self._store.bot_lock(bot_uuid):
            products = await self._load_products_unlocked(bot_uuid)
            result: list[ProductStock] = []
            for product in products:
                try:
                    available_stock = await self._store.sharded_index_count(
                        self._card_pool_key(bot_uuid, product.product_id)
                    )
                except (ValueError, UnicodeDecodeError) as exc:
                    raise CommerceStorageError('卡密库存索引包含损坏记录。') from exc
                result.append(
                    ProductStock(
                        product=product,
                        available_stock=available_stock,
                    )
                )
            return tuple(result)

    async def redeem(
        self,
        bot_uuid: str,
        sender_id: str,
        product_id: str,
        *,
        request_id: str,
        at: str | None = None,
    ) -> RedemptionResult:
        self._validate_request_id(request_id)
        identity = identity_hash(bot_uuid, sender_id)
        operation_id = self._redeem_operation_id(bot_uuid, identity, request_id)
        async with self._store.bot_lock(bot_uuid):
            await self._load_pending_reservations_unlocked(bot_uuid)
            operation = await self._store.get_operation(bot_uuid, operation_id)
            if operation is None:
                product = await self._load_product(bot_uuid, product_id)
                if product is None:
                    raise ProductNotFoundError('商品不存在。')
                if not product.enabled:
                    raise ProductUnavailableError('商品当前未上架。')
                card = await self._first_available_card_unlocked(
                    bot_uuid,
                    product.product_id,
                )
                if card is None:
                    raise InventoryEmptyError('商品库存不足。')
                timestamp = at or _now_iso()
                _parse_time(timestamp)
                redemption_id = hashlib.sha256(
                    f'{bot_uuid}\0{operation_id}'.encode('utf-8')
                ).hexdigest()
                payload_extra: dict[str, object] = {
                    'identity_hash': identity,
                    'product_id': product.product_id,
                    'card_hash': card.card_hash,
                    'redemption_id': redemption_id,
                    'points_cost': product.points_cost,
                    'duration_days': card.duration_days,
                    'created_at': timestamp,
                }
                changes: tuple[PointChange, ...] = (
                    PointChange(
                        identity,
                        -product.points_cost,
                        'redeem_debit',
                        'points-debited',
                        product.product_id,
                    ),
                )
            else:
                payload_extra, changes = self._redeem_payload(operation)
                if payload_extra['product_id'] != product_id:
                    raise CommerceError('请求 ID 已用于其他商品。')
                timestamp = str(payload_extra['created_at'])

            card_hash = cast(str, payload_extra['card_hash'])
            self._reserve_card(bot_uuid, card_hash, operation_id)
            try:
                redemption, _, code = await self._complete_redeem_unlocked(
                    bot_uuid,
                    operation_id,
                    payload_extra,
                    changes,
                )
            except Exception:
                current = await self._store.get_operation(bot_uuid, operation_id)
                if current is None:
                    self._release_card(bot_uuid, card_hash)
                raise
            self._release_card(bot_uuid, card_hash)
            return RedemptionResult(redemption=redemption, card_code=code)

    async def list_redemptions(
        self,
        bot_uuid: str,
        sender_id: str,
    ) -> tuple[RedemptionView, ...]:
        identity = identity_hash(bot_uuid, sender_id)
        async with self._store.bot_lock(bot_uuid):
            indexed = await self._store.read_sharded_index(
                growth_storage_key(REDEMPTION_INDEX_PREFIX, bot_uuid, identity)
            )
            if indexed.skipped_count:
                raise CommerceStorageError('兑换索引包含损坏记录。')
            result: list[RedemptionView] = []
            for redemption_id in indexed.items:
                redemption = await self._store.get(
                    growth_storage_key(REDEMPTION_PREFIX, bot_uuid, redemption_id),
                    RedemptionRecord,
                )
                if redemption is None or redemption.identity_hash != identity:
                    raise CommerceStorageError('兑换索引缺少对应记录。')
                card = await self._require_card(bot_uuid, redemption.card_hash)
                code = (
                    await self._decrypt_card_unlocked(bot_uuid, card)
                    if card.status == 'ISSUED'
                    else ''
                )
                result.append(
                    RedemptionView(
                        redemption=redemption,
                        card_code=code,
                        status=card.status,
                    )
                )
            return tuple(result)

    async def activate(
        self,
        bot_uuid: str,
        sender_id: str,
        card_code: str,
        *,
        at: str | None = None,
    ) -> ActivationResult:
        normalized_code = self._normalize_card_code(card_code)
        card_hash = self._card_hash(normalized_code)
        identity = identity_hash(bot_uuid, sender_id)
        operation_id = f'activate:{card_hash}'
        async with self._store.bot_lock(bot_uuid):
            await self._load_pending_reservations_unlocked(bot_uuid)
            await self._recover_pending_activations_unlocked(bot_uuid)
            operation = await self._store.get_operation(bot_uuid, operation_id)
            if operation is None:
                card = await self._require_card(bot_uuid, card_hash)
                if card.status != 'ISSUED':
                    raise CardUnavailableError('卡密当前不可激活。')
                timestamp = at or _now_iso()
                now = _parse_time(timestamp)
                status = await self._entitlement._get_status_by_identity_unlocked(
                    bot_uuid,
                    identity,
                    now=timestamp,
                )
                target = max(now, _parse_time(status.expires_at)) + timedelta(
                    days=card.duration_days
                )
                operation = await self._store.begin_operation(
                    bot_uuid,
                    operation_id,
                    'activate',
                    {
                        'card_hash': card_hash,
                        'identity_hash': identity,
                        'base_expires_at': status.expires_at,
                        'duration_days': card.duration_days,
                        'target_expires_at': target.isoformat(),
                        'activated_at': timestamp,
                    },
                    timestamp,
                )
            payload = self._activation_payload(operation)
            if payload['identity_hash'] != identity:
                raise CardUnavailableError('卡密已被其他用户激活。')
            self._track_activation(operation)
            try:
                result = await self._complete_activation_unlocked(operation)
            except Exception:
                raise
            else:
                self._release_activation(operation)
                return result

    async def recover_operation(self, operation: GrowthOperation) -> None:
        async with self._store.bot_lock(operation.bot_uuid):
            await self._load_pending_reservations_unlocked(operation.bot_uuid)
            current = await self._require_operation(
                operation.bot_uuid,
                operation.operation_id,
            )
            if current.status == 'COMMITTED':
                return
            if current.kind == 'product-create':
                await self._complete_product_create_unlocked(current)
                return
            if current.kind == 'admin-product-enabled':
                await self._complete_product_enabled_unlocked(current)
                return
            if current.kind == 'inventory-add':
                await self._complete_inventory_unlocked(current)
                return
            if current.kind == 'redeem':
                payload, changes = self._redeem_payload(current)
                card_hash = cast(str, payload['card_hash'])
                self._reserve_card(
                    current.bot_uuid,
                    card_hash,
                    current.operation_id,
                )
                try:
                    await self._complete_redeem_unlocked(
                        current.bot_uuid,
                        current.operation_id,
                        payload,
                        changes,
                    )
                except Exception:
                    raise
                else:
                    self._release_card(current.bot_uuid, card_hash)
                return
            if current.kind == 'activate':
                self._track_activation(current)
                try:
                    await self._complete_activation_unlocked(current)
                except Exception:
                    raise
                else:
                    self._release_activation(current)
                return
            raise CommerceError('操作不属于商城恢复范围。')

    async def _complete_product_create_unlocked(
        self,
        operation: GrowthOperation,
    ) -> ProductRecord:
        product = self._product_from_operation(operation)
        current = await self._load_product(operation.bot_uuid, product.product_id)
        if current is None:
            if operation.status == 'COMMITTED':
                raise CommerceStorageError('已提交的商品新增操作缺少商品记录。')
            await self._save_product(product)
            current = product
        if not self._same_product_creation(current, product):
            raise CommerceStorageError('商品新增操作与商品记录不一致。')
        operation = await self._mark_if_pending(
            operation,
            'product-saved',
            product.created_at,
        )
        if operation.status == 'PENDING':
            await self._store.commit_operation(
                operation.bot_uuid,
                operation.operation_id,
                product.created_at,
            )
        return current

    async def _complete_product_enabled_unlocked(
        self,
        operation: GrowthOperation,
    ) -> ProductRecord:
        payload = self._product_enabled_payload(operation)
        product_id = cast(str, payload['product_id'])
        enabled = cast(bool, payload['enabled'])
        product = await self._load_product(operation.bot_uuid, product_id)
        if product is None:
            raise CommerceStorageError('商品状态操作对应的商品不存在。')
        response = replace(
            product,
            enabled=enabled,
            updated_at=operation.created_at,
        )
        operation_time = _parse_time(operation.created_at)
        product_time = _parse_time(product.updated_at)
        superseded = product_time > operation_time
        if product_time == operation_time and product != response:
            raise CommerceStorageError('商品状态事实与操作时间冲突。')
        if operation.status == 'COMMITTED':
            if not superseded and product != response:
                raise CommerceStorageError('已提交的商品状态操作与商品记录不一致。')
            return response
        if 'target-saved' in operation.applied_steps:
            if not superseded and product != response:
                raise CommerceStorageError('商品状态操作步骤与商品记录不一致。')
        else:
            if not superseded and product != response:
                await self._save_product(response)
                product = response
            operation = await self._store.mark_step_applied(
                operation.bot_uuid,
                operation.operation_id,
                'target-saved',
                operation.updated_at,
            )
        await self._store.commit_operation(
            operation.bot_uuid,
            operation.operation_id,
            operation.updated_at,
        )
        return response

    async def _recover_pending_product_states_unlocked(
        self,
        bot_uuid: str,
    ) -> None:
        pending = await self._store.list_pending_operations(bot_uuid)
        if pending.skipped_count:
            raise CommerceStorageError('增长操作日志包含损坏记录。')
        operations = sorted(
            (
                operation
                for operation in pending.records
                if operation.kind == 'admin-product-enabled'
            ),
            key=lambda operation: _parse_time(operation.created_at),
        )
        for operation in operations:
            await self._complete_product_enabled_unlocked(operation)

    @staticmethod
    def _same_product_creation(
        current: ProductRecord,
        expected: ProductRecord,
    ) -> bool:
        return (
            current.bot_uuid == expected.bot_uuid
            and current.product_id == expected.product_id
            and current.name == expected.name
            and current.points_cost == expected.points_cost
            and current.duration_days == expected.duration_days
            and current.created_at == expected.created_at
        )

    @classmethod
    def _product_from_operation(
        cls,
        operation: GrowthOperation,
    ) -> ProductRecord:
        payload = cls._product_payload(operation)
        created_at = cast(str, payload['created_at'])
        return ProductRecord(
            bot_uuid=operation.bot_uuid,
            product_id=cast(str, payload['product_id']),
            name=cast(str, payload['name']),
            points_cost=cast(int, payload['points_cost']),
            duration_days=cast(int, payload['duration_days']),
            enabled=False,
            created_at=created_at,
            updated_at=created_at,
        )

    async def initialize_pending_reservations(self, bot_uuid: str) -> None:
        async with self._store.bot_lock(bot_uuid):
            await self._load_pending_reservations_unlocked(bot_uuid)

    async def _load_pending_reservations_unlocked(self, bot_uuid: str) -> None:
        if bot_uuid in self._loaded_reservation_bots:
            return
        pending = await self._store.list_pending_operations(bot_uuid)
        if pending.skipped_count:
            raise CommerceStorageError('增长操作日志包含损坏记录。')
        reservations: dict[str, str] = {}
        activations: set[str] = set()
        for operation in pending.records:
            if operation.kind == 'redeem':
                payload, _ = self._redeem_payload(operation)
                card_hash = cast(str, payload['card_hash'])
                owner = reservations.get(card_hash)
                if owner is not None and owner != operation.operation_id:
                    raise CommerceStorageError('多笔待恢复兑换操作预留了同一卡密。')
                reservations[card_hash] = operation.operation_id
            elif operation.kind == 'activate':
                self._activation_payload(operation)
                activations.add(operation.operation_id)
        self._reserved_card_hashes[bot_uuid] = reservations
        self._pending_activation_ids[bot_uuid] = activations
        self._loaded_reservation_bots.add(bot_uuid)

    async def _recover_pending_activations_unlocked(self, bot_uuid: str) -> None:
        operation_ids = tuple(self._pending_activation_ids.get(bot_uuid, ()))
        for operation_id in operation_ids:
            operation = await self._require_operation(bot_uuid, operation_id)
            await self._complete_activation_unlocked(operation)
            self._release_activation(operation)

    def _track_activation(self, operation: GrowthOperation) -> None:
        self._pending_activation_ids.setdefault(operation.bot_uuid, set()).add(
            operation.operation_id
        )

    def _release_activation(self, operation: GrowthOperation) -> None:
        operation_ids = self._pending_activation_ids.get(operation.bot_uuid)
        if operation_ids is None:
            return
        operation_ids.discard(operation.operation_id)
        if not operation_ids:
            self._pending_activation_ids.pop(operation.bot_uuid, None)

    async def _complete_inventory_unlocked(
        self,
        operation: GrowthOperation,
    ) -> InventoryResult:
        product_id, cards = self._inventory_payload(operation)
        parsed_cards = tuple(
            self._card_from_payload(
                operation.bot_uuid,
                raw_card,
                expected_product_id=product_id,
            )
            for raw_card in cards
        )
        for card in parsed_cards:
            await self._decrypt_card_unlocked(operation.bot_uuid, card)
        if operation.status == 'PENDING':
            for card in parsed_cards:
                step_id = f'card:{card.card_hash}'
                if step_id not in operation.applied_steps:
                    card_key = growth_storage_key(
                        CARD_PREFIX,
                        operation.bot_uuid,
                        card.card_hash,
                    )
                    existing = await self._store.get(card_key, CardRecord)
                    if existing is None:
                        await self._store.save(card_key, card)
                        existing = card
                    if not self._same_card_snapshot(existing, card):
                        raise CommerceStorageError('库存卡密记录与操作载荷不一致。')
                    if existing.status == 'AVAILABLE':
                        await self._store.append_sharded_index(
                            self._card_pool_key(operation.bot_uuid, product_id),
                            card.card_hash,
                        )
                    elif existing.status not in {'ISSUED', 'ACTIVATED'}:
                        raise CommerceStorageError('库存卡密状态错误。')
                    operation = await self._store.mark_step_applied(
                        operation.bot_uuid,
                        operation.operation_id,
                        step_id,
                        operation.created_at,
                    )
            await self._store.commit_operation(
                operation.bot_uuid,
                operation.operation_id,
                operation.created_at,
            )
        return InventoryResult(product_id=product_id, added_count=len(cards))

    @staticmethod
    def _same_card_snapshot(current: CardRecord, expected: CardRecord) -> bool:
        return (
            current.bot_uuid == expected.bot_uuid
            and current.card_hash == expected.card_hash
            and current.product_id == expected.product_id
            and current.product_name == expected.product_name
            and current.duration_days == expected.duration_days
            and current.encrypted_code == expected.encrypted_code
            and current.created_at == expected.created_at
        )

    async def _complete_redeem_unlocked(
        self,
        bot_uuid: str,
        operation_id: str,
        payload_extra: dict[str, object],
        changes: tuple[PointChange, ...],
    ) -> tuple[RedemptionRecord, CardRecord, str]:
        timestamp = cast(str, payload_extra['created_at'])
        identity = cast(str, payload_extra['identity_hash'])
        product_id = cast(str, payload_extra['product_id'])
        card = await self._require_card(
            bot_uuid,
            cast(str, payload_extra['card_hash']),
        )
        if card.status not in {'AVAILABLE', 'ISSUED', 'ACTIVATED'} or (
            card.status != 'AVAILABLE' and card.issued_to_hash != identity
        ):
            raise CommerceStorageError('兑换操作对应的卡密状态不一致。')
        if (
            card.product_id != product_id
            or card.duration_days != cast(int, payload_extra['duration_days'])
        ):
            raise CommerceStorageError('兑换操作与卡密商品快照不一致。')
        code = await self._decrypt_card_unlocked(bot_uuid, card)
        await self._points._apply_operation_unlocked(
            bot_uuid,
            operation_id,
            changes,
            at=timestamp,
            commit=False,
            operation_kind='redeem',
            payload_extra=payload_extra,
        )
        operation = await self._require_operation(bot_uuid, operation_id)
        was_available = card.status == 'AVAILABLE'
        if was_available:
            card = replace(
                card,
                status='ISSUED',
                issued_to_hash=identity,
                issued_at=timestamp,
            )
            await self._store.save(
                growth_storage_key(CARD_PREFIX, bot_uuid, card.card_hash),
                card,
            )
        elif card.status not in {'ISSUED', 'ACTIVATED'} or (
            card.issued_to_hash != identity
        ):
            raise CommerceStorageError('兑换操作对应的卡密状态不一致。')
        try:
            removed = await self._store.remove_sharded_index(
                self._card_pool_key(bot_uuid, product_id),
                card.card_hash,
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise CommerceStorageError('卡密库存索引包含损坏记录。') from exc
        if was_available and not removed:
            raise CommerceStorageError('可用卡密缺少库存索引。')
        operation = await self._mark_if_pending(
            operation,
            'card-issued',
            timestamp,
        )

        redemption = RedemptionRecord(
            bot_uuid=bot_uuid,
            redemption_id=cast(str, payload_extra['redemption_id']),
            identity_hash=identity,
            product_id=product_id,
            card_hash=card.card_hash,
            points_cost=cast(int, payload_extra['points_cost']),
            duration_days=cast(int, payload_extra['duration_days']),
            created_at=timestamp,
        )
        await self._store.save(
            growth_storage_key(
                REDEMPTION_PREFIX,
                bot_uuid,
                redemption.redemption_id,
            ),
            redemption,
        )
        operation = await self._mark_if_pending(
            operation,
            'redemption-saved',
            timestamp,
        )
        await self._store.append_sharded_index(
            growth_storage_key(REDEMPTION_INDEX_PREFIX, bot_uuid, identity),
            redemption.redemption_id,
        )
        operation = await self._mark_if_pending(
            operation,
            'redemption-indexed',
            timestamp,
        )
        if operation.status == 'PENDING':
            await self._store.commit_operation(bot_uuid, operation_id, timestamp)
        return redemption, card, code

    async def _complete_activation_unlocked(
        self,
        operation: GrowthOperation,
    ) -> ActivationResult:
        payload = self._activation_payload(operation)
        card_hash = cast(str, payload['card_hash'])
        identity = cast(str, payload['identity_hash'])
        timestamp = cast(str, payload['activated_at'])
        target_expiry = cast(str, payload['target_expires_at'])
        if operation.status == 'COMMITTED':
            return ActivationResult(card_hash, target_expiry, True)

        card = await self._require_card(operation.bot_uuid, card_hash)
        if card.duration_days != cast(int, payload['duration_days']):
            raise CommerceStorageError('激活操作与卡密期限快照不一致。')
        if card.status not in {'ISSUED', 'ACTIVATED'} or (
            card.status == 'ACTIVATED' and card.activated_by_hash != identity
        ):
            raise CommerceStorageError('激活操作对应的卡密状态不一致。')
        await self._entitlement._ensure_identity_expiry_unlocked(
            operation.bot_uuid,
            identity,
            expires_at=target_expiry,
            updated_at=timestamp,
        )
        operation = await self._mark_if_pending(
            operation,
            'entitlement-extended',
            timestamp,
        )
        if card.status == 'ISSUED':
            card = replace(
                card,
                status='ACTIVATED',
                activated_by_hash=identity,
                activated_at=timestamp,
            )
            await self._store.save(
                growth_storage_key(CARD_PREFIX, operation.bot_uuid, card_hash),
                card,
            )
        elif card.status != 'ACTIVATED' or card.activated_by_hash != identity:
            raise CommerceStorageError('激活操作对应的卡密状态不一致。')
        operation = await self._mark_if_pending(
            operation,
            'card-activated',
            timestamp,
        )
        await self._store.commit_operation(
            operation.bot_uuid,
            operation.operation_id,
            timestamp,
        )
        return ActivationResult(card_hash, target_expiry, False)

    async def _create_card_payloads(
        self,
        bot_uuid: str,
        product: ProductRecord,
        quantity: int,
        timestamp: str,
        fernet: Fernet,
    ) -> list[dict[str, object]]:
        cards: list[dict[str, object]] = []
        seen: set[str] = set()
        while len(cards) < quantity:
            code = self._normalize_card_code(self._card_code_factory())
            card_hash = self._card_hash(code)
            if card_hash in seen or await self._store.get(
                growth_storage_key(CARD_PREFIX, bot_uuid, card_hash),
                CardRecord,
            ) is not None:
                continue
            seen.add(card_hash)
            cards.append(
                {
                    'card_hash': card_hash,
                    'product_id': product.product_id,
                    'product_name': product.name,
                    'duration_days': product.duration_days,
                    'encrypted_code': fernet.encrypt(code.encode('ascii')).decode('ascii'),
                    'created_at': timestamp,
                }
            )
        return cards

    async def _first_available_card_unlocked(
        self,
        bot_uuid: str,
        product_id: str,
    ) -> CardRecord | None:
        pool_key = self._card_pool_key(bot_uuid, product_id)
        reserved = self._reserved_card_hashes.get(bot_uuid, {})

        async def next_unreserved_hash() -> str | None:
            if reserved:
                card_hashes = await self._store.sharded_index_items(pool_key)
                return next(
                    (item for item in card_hashes if item not in reserved),
                    None,
                )
            return await self._store.first_sharded_index_item(pool_key)

        try:
            card_hash = await next_unreserved_hash()
        except (ValueError, UnicodeDecodeError) as exc:
            raise CommerceStorageError('卡密库存索引包含损坏记录。') from exc
        while card_hash is not None:
            card = await self._store.get(
                growth_storage_key(CARD_PREFIX, bot_uuid, card_hash),
                CardRecord,
            )
            if card is None or card.product_id != product_id:
                raise CommerceStorageError('卡密库存索引缺少对应记录。')
            if card.status == 'AVAILABLE':
                return card
            try:
                await self._store.remove_sharded_index(pool_key, card_hash)
                card_hash = await next_unreserved_hash()
            except (ValueError, UnicodeDecodeError) as exc:
                raise CommerceStorageError('卡密库存索引包含损坏记录。') from exc
        return None

    def _reserve_card(
        self,
        bot_uuid: str,
        card_hash: str,
        operation_id: str,
    ) -> None:
        reserved = self._reserved_card_hashes.setdefault(bot_uuid, {})
        owner = reserved.get(card_hash)
        if owner is not None and owner != operation_id:
            raise CommerceStorageError('卡密已被待恢复兑换操作预留。')
        reserved[card_hash] = operation_id

    def _release_card(self, bot_uuid: str, card_hash: str) -> None:
        reserved = self._reserved_card_hashes.get(bot_uuid)
        if reserved is None:
            return
        reserved.pop(card_hash, None)
        if not reserved:
            self._reserved_card_hashes.pop(bot_uuid, None)

    @staticmethod
    def _card_pool_key(bot_uuid: str, product_id: str) -> str:
        return growth_storage_key(CARD_POOL_PREFIX, bot_uuid, product_id)

    async def _load_products_unlocked(
        self,
        bot_uuid: str,
    ) -> tuple[ProductRecord, ...]:
        listed = await self._store.list_prefix(
            f'{PRODUCT_PREFIX}{bot_uuid}:',
            ProductRecord,
        )
        if listed.skipped_count:
            raise CommerceStorageError('商品记录包含损坏数据。')
        return tuple(sorted(listed.records, key=lambda item: item.product_id))

    async def _load_product(
        self,
        bot_uuid: str,
        product_id: str,
    ) -> ProductRecord | None:
        self._validate_product_id(product_id)
        return await self._store.get(
            growth_storage_key(PRODUCT_PREFIX, bot_uuid, product_id),
            ProductRecord,
        )

    async def _save_product(self, product: ProductRecord) -> None:
        await self._store.save(
            growth_storage_key(
                PRODUCT_PREFIX,
                product.bot_uuid,
                product.product_id,
            ),
            product,
        )

    async def _require_card(self, bot_uuid: str, card_hash: str) -> CardRecord:
        card = await self._store.get(
            growth_storage_key(CARD_PREFIX, bot_uuid, card_hash),
            CardRecord,
        )
        if card is None:
            raise CardUnavailableError('卡密不存在。')
        return card

    async def _fernet_unlocked(
        self,
        bot_uuid: str,
        *,
        create_if_missing: bool = False,
    ) -> Fernet:
        key = await self._store.get_secret(bot_uuid, _CARD_SECRET_NAME)
        if key is None:
            if not create_if_missing:
                raise CommerceStorageError('卡密加密密钥不存在。')
            pending = await self._store.list_pending_operations(bot_uuid)
            if pending.skipped_count:
                raise CommerceStorageError('增长操作日志包含损坏记录。')
            if await self._store.has_prefix(f'{CARD_PREFIX}{bot_uuid}:') or any(
                operation.kind in {'inventory-add', 'redeem'}
                for operation in pending.records
            ):
                raise CommerceStorageError('已有卡密数据但加密密钥不存在。')
            key = self._secret_key_factory()
            try:
                Fernet(key)
            except (TypeError, ValueError) as exc:
                raise CommerceStorageError('卡密加密密钥格式错误。') from exc
            await self._store.save_secret(bot_uuid, _CARD_SECRET_NAME, key)
        try:
            return Fernet(key)
        except (TypeError, ValueError) as exc:
            raise CommerceStorageError('卡密加密密钥格式错误。') from exc

    async def _decrypt_card_unlocked(
        self,
        bot_uuid: str,
        card: CardRecord,
    ) -> str:
        fernet = await self._fernet_unlocked(bot_uuid)
        try:
            code = fernet.decrypt(card.encrypted_code.encode('ascii')).decode('ascii')
        except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
            raise CommerceStorageError('卡密密文损坏。') from exc
        normalized = self._normalize_card_code(code)
        if self._card_hash(normalized) != card.card_hash:
            raise CommerceStorageError('卡密密文与哈希不一致。')
        return normalized

    async def _require_operation(
        self,
        bot_uuid: str,
        operation_id: str,
    ) -> GrowthOperation:
        operation = await self._store.get_operation(bot_uuid, operation_id)
        if operation is None:
            raise CommerceStorageError('商城操作记录不存在。')
        return operation

    async def _mark_if_pending(
        self,
        operation: GrowthOperation,
        step_id: str,
        timestamp: str,
    ) -> GrowthOperation:
        if operation.status == 'PENDING' and step_id not in operation.applied_steps:
            return await self._store.mark_step_applied(
                operation.bot_uuid,
                operation.operation_id,
                step_id,
                timestamp,
            )
        return operation

    @staticmethod
    def _inventory_payload(
        operation: GrowthOperation,
    ) -> tuple[str, list[dict[str, object]]]:
        if operation.kind != 'inventory-add' or operation.status not in {
            'PENDING',
            'COMMITTED',
        }:
            raise CommerceStorageError('库存操作类型错误。')
        if set(operation.payload) != {'product_id', 'quantity', 'cards'}:
            raise CommerceStorageError('库存操作格式错误。')
        product_id = operation.payload.get('product_id')
        quantity = operation.payload.get('quantity')
        cards = operation.payload.get('cards')
        if (
            type(product_id) is not str
            or type(quantity) is not int
            or not 1 <= quantity <= 1000
            or type(cards) is not list
            or len(cards) != quantity
            or not all(type(item) is dict for item in cards)
        ):
            raise CommerceStorageError('库存操作格式错误。')
        expected_prefix = f'inventory:{operation.bot_uuid}:'
        operation_hash = operation.operation_id.removeprefix(expected_prefix)
        if (
            not operation.operation_id.startswith(expected_prefix)
            or _HASH_PATTERN.fullmatch(operation_hash) is None
            or re.fullmatch(r'P\d{6}', product_id) is None
        ):
            raise CommerceStorageError('库存操作主键格式错误。')
        card_hashes = [item.get('card_hash') for item in cards]
        if (
            not all(type(card_hash) is str for card_hash in card_hashes)
            or len(set(card_hashes)) != len(card_hashes)
        ):
            raise CommerceStorageError('库存操作包含重复卡密。')
        return product_id, cards

    @staticmethod
    def _product_payload(operation: GrowthOperation) -> dict[str, object]:
        if operation.kind != 'product-create' or operation.status not in {
            'PENDING',
            'COMMITTED',
        }:
            raise CommerceStorageError('商品新增操作类型错误。')
        required = {
            'product_id': str,
            'name': str,
            'points_cost': int,
            'duration_days': int,
            'created_at': str,
        }
        if set(operation.payload) != set(required) or any(
            type(operation.payload.get(key)) is not expected
            for key, expected in required.items()
        ):
            raise CommerceStorageError('商品新增操作格式错误。')
        product_id = cast(str, operation.payload['product_id'])
        name = cast(str, operation.payload['name'])
        points_cost = cast(int, operation.payload['points_cost'])
        duration_days = cast(int, operation.payload['duration_days'])
        created_at = cast(str, operation.payload['created_at'])
        expected_prefix = f'product-create:{operation.bot_uuid}:'
        operation_hash = operation.operation_id.removeprefix(expected_prefix)
        try:
            _parse_time(created_at)
            CommerceService._validate_product(name, points_cost, duration_days)
        except ValueError as exc:
            raise CommerceStorageError('商品新增操作格式错误。') from exc
        if (
            re.fullmatch(r'P\d{6}', product_id) is None
            or created_at != operation.created_at
            or not operation.operation_id.startswith(expected_prefix)
            or _HASH_PATTERN.fullmatch(operation_hash) is None
            or tuple(operation.applied_steps) not in {
                (),
                ('product-saved',),
            }
            or (
                operation.status == 'COMMITTED'
                and operation.applied_steps != ('product-saved',)
            )
        ):
            raise CommerceStorageError('商品新增操作业务字段不一致。')
        return dict(operation.payload)

    @staticmethod
    def _product_enabled_payload(operation: GrowthOperation) -> dict[str, object]:
        if operation.kind != 'admin-product-enabled' or operation.status not in {
            'PENDING',
            'COMMITTED',
        }:
            raise CommerceStorageError('商品状态操作类型错误。')
        if set(operation.payload) != {'product_id', 'enabled'}:
            raise CommerceStorageError('商品状态操作格式错误。')
        product_id = operation.payload.get('product_id')
        enabled = operation.payload.get('enabled')
        if (
            type(product_id) is not str
            or re.fullmatch(r'P\d{6}', product_id) is None
            or type(enabled) is not bool
            or re.fullmatch(
                r'admin-product-enabled:[0-9a-f]{64}',
                operation.operation_id,
            )
            is None
            or tuple(operation.applied_steps) not in {
                (),
                ('target-saved',),
            }
            or (
                operation.status == 'COMMITTED'
                and operation.applied_steps != ('target-saved',)
            )
        ):
            raise CommerceStorageError('商品状态操作业务字段不一致。')
        return dict(operation.payload)

    @staticmethod
    def _card_from_payload(
        bot_uuid: str,
        raw: dict[str, object],
        *,
        expected_product_id: str,
    ) -> CardRecord:
        expected = {
            'card_hash',
            'product_id',
            'product_name',
            'duration_days',
            'encrypted_code',
            'created_at',
        }
        if set(raw) != expected:
            raise CommerceStorageError('库存卡密操作格式错误。')
        if (
            any(
                type(raw[name]) is not str
                for name in (
                    'card_hash',
                    'product_id',
                    'product_name',
                    'encrypted_code',
                    'created_at',
                )
            )
            or type(raw['duration_days']) is not int
        ):
            raise CommerceStorageError('库存卡密操作格式错误。')
        card_hash = cast(str, raw['card_hash'])
        product_id = cast(str, raw['product_id'])
        product_name = cast(str, raw['product_name'])
        encrypted_code = cast(str, raw['encrypted_code'])
        created_at = cast(str, raw['created_at'])
        duration_days = cast(int, raw['duration_days'])
        try:
            _parse_time(created_at)
        except ValueError as exc:
            raise CommerceStorageError('库存卡密时间格式错误。') from exc
        if (
            _HASH_PATTERN.fullmatch(card_hash) is None
            or product_id != expected_product_id
            or not 1 <= len(product_name) <= 50
            or not 1 <= duration_days <= 3650
            or not encrypted_code
        ):
            raise CommerceStorageError('库存卡密操作格式错误。')
        try:
            return CardRecord(
                bot_uuid=bot_uuid,
                card_hash=card_hash,
                product_id=product_id,
                product_name=product_name,
                duration_days=duration_days,
                status='AVAILABLE',
                encrypted_code=encrypted_code,
                created_at=created_at,
            )
        except (TypeError, ValueError) as exc:
            raise CommerceStorageError('库存卡密操作格式错误。') from exc

    @staticmethod
    def _redeem_payload(
        operation: GrowthOperation,
    ) -> tuple[dict[str, object], tuple[PointChange, ...]]:
        if operation.kind != 'redeem' or operation.status not in {
            'PENDING',
            'COMMITTED',
        }:
            raise CommerceStorageError('兑换操作类型错误。')
        required = {
            'identity_hash': str,
            'product_id': str,
            'card_hash': str,
            'redemption_id': str,
            'points_cost': int,
            'duration_days': int,
            'created_at': str,
        }
        if set(operation.payload) != {*required, 'changes'}:
            raise CommerceStorageError('兑换操作格式错误。')
        extra: dict[str, object] = {}
        for key, expected_type in required.items():
            value = operation.payload.get(key)
            if type(value) is not expected_type:
                raise CommerceStorageError('兑换操作格式错误。')
            extra[key] = value
        identity = cast(str, extra['identity_hash'])
        product_id = cast(str, extra['product_id'])
        card_hash = cast(str, extra['card_hash'])
        redemption_id = cast(str, extra['redemption_id'])
        points_cost = cast(int, extra['points_cost'])
        duration_days = cast(int, extra['duration_days'])
        created_at = cast(str, extra['created_at'])
        operation_prefix = f'redeem:{operation.bot_uuid}:{identity}:'
        operation_hash = operation.operation_id.removeprefix(operation_prefix)
        try:
            _parse_time(created_at)
        except ValueError as exc:
            raise CommerceStorageError('兑换操作时间格式错误。') from exc
        if (
            _HASH_PATTERN.fullmatch(identity) is None
            or re.fullmatch(r'P\d{6}', product_id) is None
            or _HASH_PATTERN.fullmatch(card_hash) is None
            or _HASH_PATTERN.fullmatch(redemption_id) is None
            or not 1 <= points_cost <= 1_000_000
            or not 1 <= duration_days <= 3650
            or not operation.operation_id.startswith(operation_prefix)
            or _HASH_PATTERN.fullmatch(operation_hash) is None
            or redemption_id
            != hashlib.sha256(
                f'{operation.bot_uuid}\0{operation.operation_id}'.encode('utf-8')
            ).hexdigest()
        ):
            raise CommerceStorageError('兑换操作业务字段不一致。')
        changes = operation.payload.get('changes')
        if type(changes) is not list or len(changes) != 1:
            raise CommerceStorageError('兑换操作缺少积分变更。')
        change = changes[0]
        expected_change_fields = {
            'identity_hash',
            'amount',
            'entry_type',
            'step_id',
            'reason',
        }
        if type(change) is not dict or set(change) != expected_change_fields:
            raise CommerceStorageError('兑换积分变更格式错误。')
        try:
            parsed = PointChange(
                identity_hash=change['identity_hash'],
                amount=change['amount'],
                entry_type=change['entry_type'],
                step_id=change['step_id'],
                reason=change['reason'],
            )
        except (KeyError, TypeError) as exc:
            raise CommerceStorageError('兑换积分变更格式错误。') from exc
        if parsed != PointChange(
            identity_hash=identity,
            amount=-points_cost,
            entry_type='redeem_debit',
            step_id='points-debited',
            reason=product_id,
        ):
            raise CommerceStorageError('兑换积分变更与业务载荷不一致。')
        return extra, (parsed,)

    @staticmethod
    def _activation_payload(operation: GrowthOperation) -> dict[str, object]:
        if operation.kind != 'activate' or operation.status not in {
            'PENDING',
            'COMMITTED',
        }:
            raise CommerceStorageError('激活操作类型错误。')
        required = {
            'card_hash': str,
            'identity_hash': str,
            'base_expires_at': str,
            'duration_days': int,
            'target_expires_at': str,
            'activated_at': str,
        }
        if set(operation.payload) != set(required):
            raise CommerceStorageError('激活操作格式错误。')
        payload: dict[str, object] = {}
        for key, expected_type in required.items():
            value = operation.payload.get(key)
            if type(value) is not expected_type:
                raise CommerceStorageError('激活操作格式错误。')
            payload[key] = value
        try:
            base_expires_at = _parse_time(str(payload['base_expires_at']))
            target_expires_at = _parse_time(str(payload['target_expires_at']))
            activated_at = _parse_time(str(payload['activated_at']))
        except ValueError as exc:
            raise CommerceStorageError('激活操作时间格式错误。') from exc
        card_hash = cast(str, payload['card_hash'])
        identity = cast(str, payload['identity_hash'])
        duration_days = cast(int, payload['duration_days'])
        expected_target = max(activated_at, base_expires_at) + timedelta(
            days=duration_days
        )
        if (
            _HASH_PATTERN.fullmatch(card_hash) is None
            or _HASH_PATTERN.fullmatch(identity) is None
            or not 1 <= duration_days <= 3650
            or operation.operation_id != f'activate:{card_hash}'
            or target_expires_at != expected_target
        ):
            raise CommerceStorageError('激活操作业务字段不一致。')
        return payload

    @staticmethod
    def _validate_product(name: str, points_cost: int, duration_days: int) -> str:
        if type(name) is not str:
            raise ValueError('商品名称格式错误。')
        normalized_name = name.strip()
        if not 1 <= len(normalized_name) <= 50:
            raise ValueError('商品名称长度必须在 1 至 50 个字符之间。')
        if type(points_cost) is not int or not 1 <= points_cost <= 1_000_000:
            raise ValueError('商品积分价格必须在 1 至 1000000 之间。')
        if type(duration_days) is not int or not 1 <= duration_days <= 3650:
            raise ValueError('商品延期天数必须在 1 至 3650 之间。')
        return normalized_name

    @staticmethod
    def _validate_product_id(product_id: str) -> None:
        if type(product_id) is not str or not re.fullmatch(r'P\d{6}', product_id):
            raise ValueError('商品 ID 格式错误。')

    @staticmethod
    def _validate_request_id(request_id: str) -> None:
        if type(request_id) is not str or not request_id:
            raise ValueError('请求 ID 不能为空。')

    @staticmethod
    def _inventory_operation_id(
        bot_uuid: str,
        request_id: str,
    ) -> str:
        request_hash = hashlib.sha256(request_id.encode('utf-8')).hexdigest()
        return f'inventory:{bot_uuid}:{request_hash}'

    @staticmethod
    def _product_operation_id(bot_uuid: str, request_id: str) -> str:
        request_hash = hashlib.sha256(request_id.encode('utf-8')).hexdigest()
        return f'product-create:{bot_uuid}:{request_hash}'

    @staticmethod
    def _product_enabled_operation_id(request_id: str) -> str:
        request_hash = hashlib.sha256(request_id.encode('utf-8')).hexdigest()
        return f'admin-product-enabled:{request_hash}'

    @staticmethod
    def _redeem_operation_id(
        bot_uuid: str,
        identity: str,
        request_id: str,
    ) -> str:
        request_hash = hashlib.sha256(request_id.encode('utf-8')).hexdigest()
        return f'redeem:{bot_uuid}:{identity}:{request_hash}'

    @staticmethod
    def _normalize_card_code(value: str) -> str:
        if type(value) is not str:
            raise ValueError('卡密格式错误。')
        normalized = value.strip()
        if _CARD_PATTERN.fullmatch(normalized) is None:
            raise ValueError('卡密格式错误。')
        return normalized

    @staticmethod
    def _card_hash(card_code: str) -> str:
        return hashlib.sha256(card_code.encode('ascii')).hexdigest()

    @staticmethod
    def _generate_card_code() -> str:
        return f'WD-{secrets.token_urlsafe(32)}'


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('商城时间必须包含时区。')
    return parsed

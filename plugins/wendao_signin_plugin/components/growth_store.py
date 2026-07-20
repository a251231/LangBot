from __future__ import annotations

import asyncio
import hashlib
import json
import unicodedata
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Generic, TypeVar, cast, get_type_hints

from components.account_store import PluginStorage
from components.growth_models import (
    CardRecord,
    EntitlementRecord,
    GrowthOperation,
    GrowthRecord,
    PointAccount,
    PointEntry,
    ProductRecord,
    PromoterRecord,
    RedemptionRecord,
    ReferralRecord,
)


PROMOTER_PREFIX = 'promoter:v1:'
INVITE_CODE_PREFIX = 'invite-code:v1:'
REFERRAL_PREFIX = 'referral:v1:'
POINT_ACCOUNT_PREFIX = 'point-account:v1:'
POINT_ENTRY_PREFIX = 'point-entry:v1:'
PRODUCT_PREFIX = 'product:v1:'
CARD_PREFIX = 'card:v1:'
CARD_POOL_PREFIX = 'card-pool:v1:'
REDEMPTION_PREFIX = 'redemption:v1:'
REDEMPTION_INDEX_PREFIX = 'redemption-index:v1:'
ENTITLEMENT_PREFIX = 'entitlement:v1:'
GROWTH_OPERATION_PREFIX = 'growth-op:v1:'
GROWTH_CONFIG_PREFIX = 'growth-config:v1:'
GROWTH_SECRET_PREFIX = 'growth-secret:v1:'

SHARD_CAPACITY = 500
MAX_SHARD_ID = 999999

_GROWTH_PREFIXES = (
    PROMOTER_PREFIX,
    INVITE_CODE_PREFIX,
    REFERRAL_PREFIX,
    POINT_ACCOUNT_PREFIX,
    POINT_ENTRY_PREFIX,
    PRODUCT_PREFIX,
    CARD_PREFIX,
    CARD_POOL_PREFIX,
    REDEMPTION_PREFIX,
    REDEMPTION_INDEX_PREFIX,
    ENTITLEMENT_PREFIX,
    GROWTH_OPERATION_PREFIX,
    GROWTH_CONFIG_PREFIX,
    GROWTH_SECRET_PREFIX,
)
_RECORD_TYPES = (
    PromoterRecord,
    ReferralRecord,
    PointAccount,
    PointEntry,
    ProductRecord,
    CardRecord,
    RedemptionRecord,
    EntitlementRecord,
    GrowthOperation,
)
_RECORD_KEY_SPECS: dict[type[object], tuple[tuple[str, str], ...]] = {
    PromoterRecord: (
        (PROMOTER_PREFIX, 'identity_hash'),
        (INVITE_CODE_PREFIX, 'invite_code'),
    ),
    ReferralRecord: ((REFERRAL_PREFIX, 'invitee_hash'),),
    PointAccount: ((POINT_ACCOUNT_PREFIX, 'identity_hash'),),
    PointEntry: ((POINT_ENTRY_PREFIX, 'entry_id'),),
    ProductRecord: ((PRODUCT_PREFIX, 'product_id'),),
    CardRecord: ((CARD_PREFIX, 'card_hash'),),
    RedemptionRecord: ((REDEMPTION_PREFIX, 'redemption_id'),),
    EntitlementRecord: ((ENTITLEMENT_PREFIX, 'identity_hash'),),
    GrowthOperation: ((GROWTH_OPERATION_PREFIX, 'operation_id'),),
}

RecordT = TypeVar('RecordT', bound=GrowthRecord)


@dataclass(frozen=True, slots=True)
class RecordListResult(Generic[RecordT]):
    records: tuple[RecordT, ...]
    skipped_count: int


@dataclass(frozen=True, slots=True)
class ShardedIndexResult:
    items: tuple[str, ...]
    shard_count: int
    skipped_count: int


@dataclass(frozen=True, slots=True)
class _ShardAppendCache:
    item_shards: dict[str, int]
    tail_shard_id: int
    tail_items: tuple[str, ...]
    tail_exists: bool
    tail_corrupt: bool


def identity_hash(bot_uuid: str, sender_id: str) -> str:
    identity = f'{bot_uuid}\0{sender_id}'.encode('utf-8')
    return hashlib.sha256(identity).hexdigest()


def growth_storage_key(prefix: str, bot_uuid: str, *parts: str) -> str:
    if prefix not in _GROWTH_PREFIXES:
        raise ValueError('未知的增长存储键前缀。')
    if (
        type(bot_uuid) is not str
        or not bot_uuid
        or ':' in bot_uuid
        or _has_control_characters(bot_uuid)
    ):
        raise ValueError('增长存储键 bot_uuid 格式错误。')
    if any(
        type(value) is not str
        or not value
        or _has_control_characters(value)
        for value in parts
    ):
        raise ValueError('增长存储键片段格式错误。')
    return prefix + ':'.join((bot_uuid, *parts))


def _has_control_characters(value: str) -> bool:
    return any(unicodedata.category(character).startswith('C') for character in value)


def _validate_record_type(record_type: type[Any]) -> None:
    if record_type not in _RECORD_TYPES:
        raise TypeError('不支持的增长记录类型。')


def _validate_record_payload(
    payload: dict[str, Any],
    record_type: type[Any],
    *,
    json_form: bool,
) -> None:
    expected_fields = {item.name for item in fields(record_type)}
    if set(payload) != expected_fields:
        raise ValueError('增长记录格式错误。')
    if (
        type(payload.get('schema_version')) is not int
        or payload['schema_version'] != 1
    ):
        raise ValueError('不支持的增长存储版本。')

    type_hints = get_type_hints(record_type)
    for name, expected_type in type_hints.items():
        if expected_type in (str, int, bool) and type(payload[name]) is not expected_type:
            raise ValueError('增长记录字段类型错误。')

    if record_type is GrowthOperation:
        operation_payload = payload['payload']
        applied_steps = payload['applied_steps']
        if type(operation_payload) is not dict or not all(
            type(key) is str for key in operation_payload
        ):
            raise ValueError('增长操作记录格式错误。')
        expected_steps_type = list if json_form else tuple
        if type(applied_steps) is not expected_steps_type or not all(
            type(step) is str for step in applied_steps
        ):
            raise ValueError('增长操作步骤格式错误。')


def _record_storage_keys(record: object) -> tuple[str, ...]:
    record_type = type(record)
    _validate_record_type(record_type)
    return tuple(
        growth_storage_key(
            prefix,
            cast(str, getattr(record, 'bot_uuid')),
            cast(str, getattr(record, key_field)),
        )
        for prefix, key_field in _RECORD_KEY_SPECS[record_type]
    )


def serialize_record(record: object) -> bytes:
    record_type = type(record)
    _validate_record_type(record_type)
    payload = cast(dict[str, Any], asdict(cast(Any, record)))
    _validate_record_payload(payload, record_type, json_form=False)
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')


def deserialize_record(raw: bytes, record_type: type[RecordT]) -> RecordT:
    _validate_record_type(record_type)
    decoded = json.loads(raw.decode('utf-8'))
    if not isinstance(decoded, dict):
        raise ValueError('增长记录格式错误。')
    payload = cast(dict[str, Any], decoded)
    _validate_record_payload(payload, record_type, json_form=True)

    init_fields = {item.name for item in fields(record_type) if item.init}
    kwargs = {name: payload[name] for name in init_fields}
    if record_type is GrowthOperation:
        kwargs['applied_steps'] = tuple(cast(list[str], kwargs['applied_steps']))
    try:
        return cast(RecordT, record_type(**kwargs))
    except TypeError as exc:
        raise ValueError('增长记录格式错误。') from exc


def _shard_key(base_key: str, shard_id: int) -> str:
    if not base_key or shard_id < 0:
        raise ValueError('分片键参数错误。')
    if shard_id > MAX_SHARD_ID:
        raise OverflowError('索引分片 ID 超出六位范围。')
    return f'{base_key}:{shard_id:06d}'


def _decode_shard(raw: bytes) -> list[str]:
    payload = json.loads(raw.decode('utf-8'))
    if (
        not isinstance(payload, dict)
        or set(payload) != {'schema_version', 'items'}
        or type(payload.get('schema_version')) is not int
        or payload['schema_version'] != 1
    ):
        raise ValueError('不支持的索引分片版本。')
    items = payload.get('items')
    if (
        not isinstance(items, list)
        or len(items) > SHARD_CAPACITY
        or not all(type(item) is str for item in items)
        or any(not item for item in items)
        or len(set(items)) != len(items)
    ):
        raise ValueError('索引分片格式错误。')
    return items


def _encode_shard(items: list[str]) -> bytes:
    return json.dumps(
        {'schema_version': 1, 'items': items},
        ensure_ascii=False,
        separators=(',', ':'),
    ).encode('utf-8')


class GrowthStore:
    def __init__(self, storage: PluginStorage) -> None:
        self._storage = storage
        self._bot_locks: dict[str, asyncio.Lock] = {}
        self._shard_append_caches: dict[str, _ShardAppendCache] = {}
        self._shard_ids_by_base: dict[str, set[int]] | None = None

    def bot_lock(self, bot_uuid: str) -> asyncio.Lock:
        lock = self._bot_locks.get(bot_uuid)
        if lock is None:
            lock = asyncio.Lock()
            self._bot_locks[bot_uuid] = lock
        return lock

    async def save(self, key: str, record: GrowthRecord) -> None:
        raw = serialize_record(record)
        if key not in _record_storage_keys(record):
            raise ValueError('增长记录 bot_uuid 或主键与存储键不一致。')
        await self._storage.set_plugin_storage(key, raw)

    async def get(self, key: str, record_type: type[RecordT]) -> RecordT | None:
        _validate_record_type(record_type)
        keys = await self._storage.get_plugin_storage_keys()
        if key not in keys:
            return None
        raw = await self._storage.get_plugin_storage(key)
        record = deserialize_record(raw, record_type)
        if key not in _record_storage_keys(record):
            raise ValueError('增长记录 bot_uuid 或主键与存储键不一致。')
        return record

    async def delete(self, key: str) -> bool:
        keys = await self._storage.get_plugin_storage_keys()
        if key not in keys:
            return False
        await self._storage.delete_plugin_storage(key)
        base_key, separator, suffix = key.rpartition(':')
        if separator and len(suffix) == 6 and suffix.isdigit():
            self._shard_append_caches.pop(base_key, None)
            if self._shard_ids_by_base is not None:
                shard_ids = self._shard_ids_by_base.get(base_key)
                if shard_ids is not None:
                    shard_ids.discard(int(suffix))
                    if not shard_ids:
                        self._shard_ids_by_base.pop(base_key, None)
        return True

    async def list_prefix(
        self,
        prefix: str,
        record_type: type[RecordT],
    ) -> RecordListResult[RecordT]:
        _validate_record_type(record_type)
        records: list[RecordT] = []
        skipped_count = 0
        keys = await self._storage.get_plugin_storage_keys()
        for key in sorted(key for key in keys if key.startswith(prefix)):
            raw = await self._storage.get_plugin_storage(key)
            try:
                record = deserialize_record(raw, record_type)
                if key not in _record_storage_keys(record):
                    raise ValueError('增长记录存储键不一致。')
                records.append(record)
            except (ValueError, UnicodeDecodeError):
                skipped_count += 1
        return RecordListResult(tuple(records), skipped_count)

    async def get_operation(
        self,
        bot_uuid: str,
        operation_id: str,
    ) -> GrowthOperation | None:
        key = growth_storage_key(GROWTH_OPERATION_PREFIX, bot_uuid, operation_id)
        return await self.get(key, GrowthOperation)

    async def begin_operation(
        self,
        bot_uuid: str,
        operation_id: str,
        kind: str,
        payload: dict[str, object],
        created_at: str,
    ) -> GrowthOperation:
        existing = await self.get_operation(bot_uuid, operation_id)
        if existing is not None:
            if existing.kind != kind or existing.payload != payload:
                raise ValueError('操作 ID 已用于其他增长请求。')
            return existing
        operation = GrowthOperation(
            bot_uuid=bot_uuid,
            operation_id=operation_id,
            kind=kind,
            status='PENDING',
            payload=payload,
            applied_steps=(),
            created_at=created_at,
            updated_at=created_at,
        )
        key = growth_storage_key(GROWTH_OPERATION_PREFIX, bot_uuid, operation_id)
        await self.save(key, operation)
        return operation

    async def mark_step_applied(
        self,
        bot_uuid: str,
        operation_id: str,
        step: str,
        updated_at: str,
    ) -> GrowthOperation:
        operation = await self.get_operation(bot_uuid, operation_id)
        if operation is None:
            raise ValueError('增长操作不存在。')
        if step in operation.applied_steps:
            return operation
        if operation.status != 'PENDING':
            raise ValueError('已提交的增长操作不能增加步骤。')
        updated = replace(
            operation,
            applied_steps=(*operation.applied_steps, step),
            updated_at=updated_at,
        )
        key = growth_storage_key(GROWTH_OPERATION_PREFIX, bot_uuid, operation_id)
        await self.save(key, updated)
        return updated

    async def commit_operation(
        self,
        bot_uuid: str,
        operation_id: str,
        updated_at: str,
    ) -> GrowthOperation:
        operation = await self.get_operation(bot_uuid, operation_id)
        if operation is None:
            raise ValueError('增长操作不存在。')
        if operation.status == 'COMMITTED':
            return operation
        if operation.status != 'PENDING':
            raise ValueError('增长操作状态错误。')
        committed = replace(operation, status='COMMITTED', updated_at=updated_at)
        key = growth_storage_key(GROWTH_OPERATION_PREFIX, bot_uuid, operation_id)
        await self.save(key, committed)
        return committed

    async def list_pending_operations(
        self,
        bot_uuid: str,
    ) -> RecordListResult[GrowthOperation]:
        listed = await self.list_prefix(
            f'{GROWTH_OPERATION_PREFIX}{bot_uuid}:',
            GrowthOperation,
        )
        return RecordListResult(
            tuple(record for record in listed.records if record.status == 'PENDING'),
            listed.skipped_count,
        )

    async def append_sharded_index(self, base_key: str, item: str) -> int:
        if not item:
            raise ValueError('索引项不能为空。')
        cache = await self._get_shard_append_cache(base_key)
        existing_shard_id = cache.item_shards.get(item)
        if existing_shard_id is not None:
            return existing_shard_id

        shard_id = cache.tail_shard_id
        shard_items = list(cache.tail_items)
        if cache.tail_exists and (
            cache.tail_corrupt or len(shard_items) >= SHARD_CAPACITY
        ):
            if shard_id >= MAX_SHARD_ID:
                raise OverflowError('索引分片 ID 已达到六位上限。')
            shard_id += 1
            shard_items = []
        updated_items = [*shard_items, item]
        await self._storage.set_plugin_storage(
            _shard_key(base_key, shard_id),
            _encode_shard(updated_items),
        )
        if self._shard_ids_by_base is not None:
            self._shard_ids_by_base.setdefault(base_key, set()).add(shard_id)
        cache.item_shards[item] = shard_id
        self._shard_append_caches[base_key] = _ShardAppendCache(
            item_shards=cache.item_shards,
            tail_shard_id=shard_id,
            tail_items=tuple(updated_items),
            tail_exists=True,
            tail_corrupt=False,
        )
        return shard_id

    async def read_sharded_index(self, base_key: str) -> ShardedIndexResult:
        items: list[str] = []
        seen_items: set[str] = set()
        skipped_count = 0
        shard_ids = await self._shard_ids(base_key)
        for shard_id in shard_ids:
            raw = await self._storage.get_plugin_storage(_shard_key(base_key, shard_id))
            try:
                shard_items = _decode_shard(raw)
            except (ValueError, UnicodeDecodeError):
                skipped_count += 1
                continue
            if any(item in seen_items for item in shard_items):
                skipped_count += 1
            for item in shard_items:
                if item not in seen_items:
                    items.append(item)
                    seen_items.add(item)
        return ShardedIndexResult(tuple(items), len(shard_ids), skipped_count)

    async def _get_shard_append_cache(self, base_key: str) -> _ShardAppendCache:
        cached = self._shard_append_caches.get(base_key)
        if cached is not None:
            return cached

        shard_ids = await self._shard_ids(base_key)
        item_shards: dict[str, int] = {}
        tail_shard_id = shard_ids[-1] if shard_ids else 0
        tail_items: tuple[str, ...] = ()
        tail_corrupt = False
        for shard_id in shard_ids:
            raw = await self._storage.get_plugin_storage(_shard_key(base_key, shard_id))
            try:
                shard_items = _decode_shard(raw)
            except (ValueError, UnicodeDecodeError):
                if shard_id == tail_shard_id:
                    tail_corrupt = True
                continue
            has_cross_shard_duplicate = any(
                shard_item in item_shards for shard_item in shard_items
            )
            for shard_item in shard_items:
                item_shards.setdefault(shard_item, shard_id)
            if shard_id == tail_shard_id:
                tail_corrupt = has_cross_shard_duplicate
                if not tail_corrupt:
                    tail_items = tuple(shard_items)

        cache = _ShardAppendCache(
            item_shards=item_shards,
            tail_shard_id=tail_shard_id,
            tail_items=tail_items,
            tail_exists=bool(shard_ids),
            tail_corrupt=tail_corrupt,
        )
        self._shard_append_caches[base_key] = cache
        return cache

    async def _shard_ids(self, base_key: str) -> list[int]:
        if self._shard_ids_by_base is None:
            self._shard_ids_by_base = {}
            keys = await self._storage.get_plugin_storage_keys()
            for key in keys:
                stored_base_key, separator, suffix = key.rpartition(':')
                if separator and len(suffix) == 6 and suffix.isdigit():
                    self._shard_ids_by_base.setdefault(
                        stored_base_key,
                        set(),
                    ).add(int(suffix))
        return sorted(self._shard_ids_by_base.get(base_key, set()))

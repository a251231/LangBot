from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, fields, replace
from typing import Generic, TypeVar, cast, get_type_hints

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


def identity_hash(bot_uuid: str, sender_id: str) -> str:
    identity = f'{bot_uuid}\0{sender_id}'.encode('utf-8')
    return hashlib.sha256(identity).hexdigest()


def growth_storage_key(prefix: str, bot_uuid: str, *parts: str) -> str:
    if prefix not in _GROWTH_PREFIXES:
        raise ValueError('未知的增长存储键前缀。')
    values = (bot_uuid, *parts)
    if any(
        not value or '\0' in value or '\r' in value or '\n' in value
        for value in values
    ):
        raise ValueError('增长存储键片段格式错误。')
    return prefix + ':'.join(values)


def serialize_record(record: object) -> bytes:
    if not isinstance(record, _RECORD_TYPES):
        raise TypeError('不支持的增长记录类型。')
    return json.dumps(
        asdict(record),
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')


def deserialize_record(raw: bytes, record_type: type[RecordT]) -> RecordT:
    if record_type not in _RECORD_TYPES:
        raise TypeError('不支持的增长记录类型。')
    payload = json.loads(raw.decode('utf-8'))
    if not isinstance(payload, dict) or payload.get('schema_version') != 1:
        raise ValueError('不支持的增长存储版本。')

    init_fields = {item.name for item in fields(record_type) if item.init}
    kwargs = {name: payload[name] for name in init_fields if name in payload}
    type_hints = get_type_hints(record_type)
    for name, value in kwargs.items():
        expected_type = type_hints[name]
        if expected_type in (str, int, bool) and type(value) is not expected_type:
            raise ValueError('增长记录字段类型错误。')
    if record_type is GrowthOperation:
        operation_payload = kwargs.get('payload')
        applied_steps = kwargs.get('applied_steps')
        if not isinstance(operation_payload, dict) or not isinstance(applied_steps, list):
            raise ValueError('增长操作记录格式错误。')
        if not all(isinstance(step, str) for step in applied_steps):
            raise ValueError('增长操作步骤格式错误。')
        kwargs['applied_steps'] = tuple(applied_steps)
    try:
        return cast(RecordT, record_type(**kwargs))
    except (KeyError, TypeError) as exc:
        raise ValueError('增长记录格式错误。') from exc


def _shard_key(base_key: str, shard_id: int) -> str:
    if not base_key or shard_id < 0:
        raise ValueError('分片键参数错误。')
    return f'{base_key}:{shard_id:06d}'


def _decode_shard(raw: bytes) -> list[str]:
    payload = json.loads(raw.decode('utf-8'))
    if not isinstance(payload, dict) or payload.get('schema_version') != 1:
        raise ValueError('不支持的索引分片版本。')
    items = payload.get('items')
    if (
        not isinstance(items, list)
        or len(items) > SHARD_CAPACITY
        or not all(isinstance(item, str) for item in items)
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

    def bot_lock(self, bot_uuid: str) -> asyncio.Lock:
        lock = self._bot_locks.get(bot_uuid)
        if lock is None:
            lock = asyncio.Lock()
            self._bot_locks[bot_uuid] = lock
        return lock

    async def save(self, key: str, record: GrowthRecord) -> None:
        bot_key_matches = any(
            key == prefix + record.bot_uuid
            or key.startswith(prefix + record.bot_uuid + ':')
            for prefix in _GROWTH_PREFIXES
        )
        if not bot_key_matches:
            raise ValueError('增长记录 bot_uuid 与存储键不一致。')
        await self._storage.set_plugin_storage(key, serialize_record(record))

    async def get(self, key: str, record_type: type[RecordT]) -> RecordT | None:
        keys = await self._storage.get_plugin_storage_keys()
        if key not in keys:
            return None
        return deserialize_record(await self._storage.get_plugin_storage(key), record_type)

    async def delete(self, key: str) -> bool:
        keys = await self._storage.get_plugin_storage_keys()
        if key not in keys:
            return False
        await self._storage.delete_plugin_storage(key)
        return True

    async def list_prefix(
        self,
        prefix: str,
        record_type: type[RecordT],
    ) -> RecordListResult[RecordT]:
        records: list[RecordT] = []
        skipped_count = 0
        keys = await self._storage.get_plugin_storage_keys()
        for key in sorted(key for key in keys if key.startswith(prefix)):
            try:
                record = deserialize_record(
                    await self._storage.get_plugin_storage(key),
                    record_type,
                )
                records.append(record)
            except (TypeError, ValueError, UnicodeDecodeError):
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
        shard_ids = await self._shard_ids(base_key)
        shard_id = shard_ids[-1] if shard_ids else 0
        shard_items: list[str] = []
        if shard_ids:
            try:
                shard_items = _decode_shard(
                    await self._storage.get_plugin_storage(_shard_key(base_key, shard_id))
                )
            except (TypeError, ValueError, UnicodeDecodeError):
                shard_id += 1
                shard_items = []

        if item in shard_items:
            return shard_id
        if len(shard_items) >= SHARD_CAPACITY:
            shard_id += 1
            shard_items = []
        shard_items.append(item)
        await self._storage.set_plugin_storage(
            _shard_key(base_key, shard_id),
            _encode_shard(shard_items),
        )
        return shard_id

    async def read_sharded_index(self, base_key: str) -> ShardedIndexResult:
        items: list[str] = []
        skipped_count = 0
        shard_ids = await self._shard_ids(base_key)
        for shard_id in shard_ids:
            try:
                items.extend(
                    _decode_shard(
                        await self._storage.get_plugin_storage(
                            _shard_key(base_key, shard_id)
                        )
                    )
                )
            except (TypeError, ValueError, UnicodeDecodeError):
                skipped_count += 1
        return ShardedIndexResult(tuple(items), len(shard_ids), skipped_count)

    async def _shard_ids(self, base_key: str) -> list[int]:
        prefix = base_key + ':'
        keys = await self._storage.get_plugin_storage_keys()
        shard_ids: list[int] = []
        for key in keys:
            if not key.startswith(prefix):
                continue
            suffix = key[len(prefix) :]
            if len(suffix) == 6 and suffix.isdigit():
                shard_ids.append(int(suffix))
        return sorted(set(shard_ids))

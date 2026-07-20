from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Protocol

from components.models import AccountRecord, Credentials


ACCOUNT_PREFIX = 'account:v1:'


class PluginStorage(Protocol):
    async def set_plugin_storage(self, key: str, value: bytes) -> None: ...

    async def get_plugin_storage(self, key: str) -> bytes: ...

    async def get_plugin_storage_keys(self) -> list[str]: ...

    async def delete_plugin_storage(self, key: str) -> None: ...


def account_storage_key(bot_uuid: str, sender_id: str) -> str:
    identity = f'{bot_uuid}\0{sender_id}'.encode('utf-8')
    return ACCOUNT_PREFIX + hashlib.sha256(identity).hexdigest()


def _serialize(record: AccountRecord) -> bytes:
    credentials = record.credentials
    payload = {
        'schema_version': 1,
        'bot_uuid': record.bot_uuid,
        'sender_id': record.sender_id,
        'credentials': {
            'token': credentials.token,
            'device': credentials.device,
            'version': credentials.version,
            'version_code': credentials.version_code,
            'guest_id': credentials.guest_id,
            'client_type': credentials.client_type,
            'refresh_token': credentials.refresh_token,
            'access_token_valid_time': credentials.access_token_valid_time,
        },
        'target_type': record.target_type,
        'target_id': record.target_id,
        'schedule_time': record.schedule_time,
        'auto_signin': record.auto_signin,
        'auto_resign': record.auto_resign,
        'auto_milestone': record.auto_milestone,
        'auto_weekly_report': record.auto_weekly_report,
        'nickname': record.nickname,
        'user_identifier': record.user_identifier,
        'needs_rebind': record.needs_rebind,
        'next_retry_at': record.next_retry_at,
        'retry_count': record.retry_count,
        'retry_origin_date': record.retry_origin_date,
        'last_completed_date': record.last_completed_date,
        'last_resign_attempt_date': record.last_resign_attempt_date,
        'last_run_at': record.last_run_at,
        'last_result': record.last_result,
        'last_weekly_report_period': record.last_weekly_report_period,
        'weekly_report_next_retry_at': record.weekly_report_next_retry_at,
        'weekly_report_retry_count': record.weekly_report_retry_count,
        'weekly_report_retry_origin_period': record.weekly_report_retry_origin_period,
        'weekly_report_last_attempt_date': record.weekly_report_last_attempt_date,
        'weekly_report_last_run_at': record.weekly_report_last_run_at,
        'weekly_report_last_result': record.weekly_report_last_result,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _deserialize(raw: bytes) -> AccountRecord:
    payload = json.loads(raw.decode('utf-8'))
    if not isinstance(payload, dict) or payload.get('schema_version') != 1:
        raise ValueError('不支持的账号存储版本。')
    credentials_raw = payload.get('credentials')
    if not isinstance(credentials_raw, dict):
        raise ValueError('账号凭据格式错误。')
    credentials = Credentials(
        token=str(credentials_raw['token']),
        device=str(credentials_raw['device']),
        version=str(credentials_raw['version']),
        version_code=str(credentials_raw['version_code']),
        guest_id=str(credentials_raw['guest_id']),
        client_type=str(credentials_raw['client_type']),
        refresh_token=str(credentials_raw.get('refresh_token') or ''),
        access_token_valid_time=int(credentials_raw.get('access_token_valid_time') or 0),
    )
    return AccountRecord(
        bot_uuid=str(payload['bot_uuid']),
        sender_id=str(payload['sender_id']),
        credentials=credentials,
        target_type=str(payload.get('target_type') or 'person'),
        target_id=str(payload['target_id']),
        schedule_time=str(payload.get('schedule_time') or '08:00'),
        auto_signin=bool(payload.get('auto_signin', True)),
        auto_resign=bool(payload.get('auto_resign', True)),
        auto_milestone=bool(payload.get('auto_milestone', True)),
        auto_weekly_report=bool(payload.get('auto_weekly_report', True)),
        nickname=str(payload.get('nickname') or ''),
        user_identifier=str(payload.get('user_identifier') or ''),
        needs_rebind=bool(payload.get('needs_rebind', False)),
        next_retry_at=str(payload.get('next_retry_at') or ''),
        retry_count=int(payload.get('retry_count') or 0),
        retry_origin_date=str(payload.get('retry_origin_date') or ''),
        last_completed_date=str(payload.get('last_completed_date') or ''),
        last_resign_attempt_date=str(payload.get('last_resign_attempt_date') or ''),
        last_run_at=str(payload.get('last_run_at') or ''),
        last_result=str(payload.get('last_result') or ''),
        last_weekly_report_period=str(
            payload.get('last_weekly_report_period') or ''
        ),
        weekly_report_next_retry_at=str(
            payload.get('weekly_report_next_retry_at') or ''
        ),
        weekly_report_retry_count=int(
            payload.get('weekly_report_retry_count') or 0
        ),
        weekly_report_retry_origin_period=str(
            payload.get('weekly_report_retry_origin_period') or ''
        ),
        weekly_report_last_attempt_date=str(
            payload.get('weekly_report_last_attempt_date') or ''
        ),
        weekly_report_last_run_at=str(
            payload.get('weekly_report_last_run_at') or ''
        ),
        weekly_report_last_result=str(
            payload.get('weekly_report_last_result') or ''
        ),
    )


class AccountStore:
    def __init__(self, storage: PluginStorage) -> None:
        self._storage = storage
        self._account_locks: dict[str, asyncio.Lock] = {}

    def account_lock(self, bot_uuid: str, sender_id: str) -> asyncio.Lock:
        key = account_storage_key(bot_uuid, sender_id)
        lock = self._account_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._account_locks[key] = lock
        return lock

    async def save(self, record: AccountRecord) -> None:
        key = account_storage_key(record.bot_uuid, record.sender_id)
        await self._storage.set_plugin_storage(key, _serialize(record))

    async def get(self, bot_uuid: str, sender_id: str) -> AccountRecord | None:
        key = account_storage_key(bot_uuid, sender_id)
        keys = await self._storage.get_plugin_storage_keys()
        if key not in keys:
            return None
        return _deserialize(await self._storage.get_plugin_storage(key))

    async def list_accounts(self) -> list[AccountRecord]:
        accounts: list[AccountRecord] = []
        keys = await self._storage.get_plugin_storage_keys()
        for key in sorted(key for key in keys if key.startswith(ACCOUNT_PREFIX)):
            try:
                accounts.append(_deserialize(await self._storage.get_plugin_storage(key)))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
                continue
        return accounts

    async def delete(self, bot_uuid: str, sender_id: str) -> bool:
        key = account_storage_key(bot_uuid, sender_id)
        keys = await self._storage.get_plugin_storage_keys()
        if key not in keys:
            return False
        await self._storage.delete_plugin_storage(key)
        return True

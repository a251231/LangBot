from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from typing import Generic, Protocol, TypeVar
from zoneinfo import ZoneInfo

from components.account_store import AccountStore
from components.api_client import (
    WendaoApiError,
    WendaoAuthError,
    WendaoBusinessError,
    WendaoRetryableError,
)
from components.models import AccountRecord, Credentials, credentials_fingerprint


TOKEN_REFRESH_LEEWAY_MS = 60_000
ACCOUNT_NOT_BOUND_MESSAGE = '尚未绑定问道账号，请发送“问道登录 11位手机号”开始登录。'
ResultT = TypeVar('ResultT')


class AuthenticatedClient(Protocol):
    async def refresh_access_token(self) -> Credentials: ...

    async def aclose(self) -> None: ...


ClientT = TypeVar('ClientT', bound=AuthenticatedClient)


class AccountNotBoundError(RuntimeError):
    pass


class AccountNeedsRebindError(RuntimeError):
    pass


class AuthenticatedAccountSession(Generic[ClientT]):
    def __init__(
        self,
        store: AccountStore,
        *,
        client_factory: Callable[[AccountRecord], ClientT],
        max_concurrency: int = 3,
        semaphore: asyncio.Semaphore | None = None,
        timezone: ZoneInfo | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._client_factory = client_factory
        self._semaphore = semaphore or asyncio.Semaphore(max(1, max_concurrency))
        self._timezone = timezone or ZoneInfo('Asia/Shanghai')
        timezone_value = self._timezone
        self._now = now or (lambda: datetime.now(timezone_value))

    def _current_time(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=self._timezone)
        return value.astimezone(self._timezone)

    async def execute(
        self,
        bot_uuid: str,
        sender_id: str,
        operation: Callable[[AccountRecord, ClientT], Awaitable[ResultT]],
    ) -> ResultT:
        lock = self._store.account_lock(bot_uuid, sender_id)
        async with lock:
            async with self._semaphore:
                return await self._execute_locked(bot_uuid, sender_id, operation)

    async def _execute_locked(
        self,
        bot_uuid: str,
        sender_id: str,
        operation: Callable[[AccountRecord, ClientT], Awaitable[ResultT]],
    ) -> ResultT:
        account = await self._store.get(bot_uuid, sender_id)
        if account is None:
            raise AccountNotBoundError(ACCOUNT_NOT_BOUND_MESSAGE)
        if account.needs_rebind:
            raise AccountNeedsRebindError(
                '问道账号凭据已失效，请重新登录并发送新的登录响应。'
            )

        credentials = account.credentials
        valid_time = credentials.access_token_valid_time
        now_ms = int(self._current_time().timestamp() * 1000)
        refresh_due = valid_time > 0 and now_ms + TOKEN_REFRESH_LEEWAY_MS >= valid_time
        if refresh_due and not credentials.refresh_token:
            await self._mark_needs_rebind(account)
            raise AccountNeedsRebindError(
                '问道访问凭据已过期，请重新登录并发送新的登录响应。'
            )

        client = self._client_factory(account)
        credentials_refreshed = False
        try:
            if refresh_due:
                refreshed_account = await self._refresh_credentials(account, client)
                account = replace(
                    account,
                    credentials=refreshed_account.credentials,
                    needs_rebind=False,
                )
                credentials_refreshed = True
            try:
                return await operation(account, client)
            except WendaoAuthError:
                if not credentials_refreshed:
                    refreshed_account = await self._refresh_credentials(account, client)
                    account = replace(
                        account,
                        credentials=refreshed_account.credentials,
                        needs_rebind=False,
                    )
                return await operation(account, client)
        except WendaoApiError as exc:
            exc.credentials_fingerprint = credentials_fingerprint(account.credentials)
            if isinstance(exc, WendaoAuthError):
                await self._mark_needs_rebind(account)
            raise
        finally:
            await client.aclose()

    async def _mark_needs_rebind(self, account: AccountRecord) -> None:
        latest = await self._store.get(account.bot_uuid, account.sender_id)
        current = latest or account
        await self._store.save(
            replace(
                current,
                needs_rebind=True,
                next_retry_at='',
                retry_count=0,
                retry_origin_date='',
            )
        )

    async def _refresh_credentials(
        self,
        account: AccountRecord,
        client: ClientT,
    ) -> AccountRecord:
        if not account.credentials.refresh_token:
            await self._mark_needs_rebind(account)
            raise AccountNeedsRebindError(
                '问道刷新凭据缺失，请重新登录并发送新的登录响应。'
            )
        try:
            refreshed = await client.refresh_access_token()
        except WendaoRetryableError:
            raise
        except (WendaoAuthError, WendaoBusinessError):
            await self._mark_needs_rebind(account)
            raise AccountNeedsRebindError(
                '问道刷新凭据已失效，请重新登录并发送新的登录响应。'
            ) from None

        latest = await self._store.get(account.bot_uuid, account.sender_id)
        current = latest or account
        updated = replace(
            current,
            credentials=refreshed,
            needs_rebind=False,
            next_retry_at='',
            retry_count=0,
            retry_origin_date='',
        )
        await self._store.save(updated)
        return updated

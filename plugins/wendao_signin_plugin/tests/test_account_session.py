from __future__ import annotations

import asyncio
import importlib
from dataclasses import replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from components.account_store import AccountStore
from components.api_client import (
    WendaoAuthError,
    WendaoBusinessError,
    WendaoRetryableError,
)
from components.models import AccountRecord, Credentials, credentials_fingerprint


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 16, 8, 0, tzinfo=SHANGHAI)


class MemoryPluginStorage:
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


class FakeClient:
    def __init__(self, refresh_result: Credentials | Exception | None = None) -> None:
        self.refresh_result = refresh_result
        self.refresh_calls = 0
        self.closed = False

    async def refresh_access_token(self) -> Credentials:
        self.refresh_calls += 1
        if isinstance(self.refresh_result, Exception):
            raise self.refresh_result
        if self.refresh_result is None:
            raise AssertionError("测试未提供刷新结果")
        return self.refresh_result

    async def aclose(self) -> None:
        self.closed = True


def run(coro):
    return asyncio.run(coro)


def session_types() -> tuple[type[Any], type[RuntimeError], type[RuntimeError]]:
    module = importlib.import_module("components.account_session")
    return (
        module.AuthenticatedAccountSession,
        module.AccountNotBoundError,
        module.AccountNeedsRebindError,
    )


def make_credentials(**changes: Any) -> Credentials:
    credentials = Credentials(
        token="ACCESS_TOKEN_SESSION_OLD",
        device="DEVICE_SESSION/Android/16",
        version="2.26.1",
        version_code="260604",
        guest_id="1030000000000000",
        client_type="wd_android",
        refresh_token="REFRESH_TOKEN_SESSION_OLD",
        access_token_valid_time=int(NOW.timestamp() * 1000) + 3_600_000,
    )
    return replace(credentials, **changes)


def make_account(
    *,
    bot_uuid: str = "bot-session-1",
    sender_id: str = "user-session-1",
    credentials: Credentials | None = None,
    **changes: Any,
) -> AccountRecord:
    account = AccountRecord.create(
        bot_uuid=bot_uuid,
        sender_id=sender_id,
        credentials=credentials or make_credentials(),
        target_id=f"target-{sender_id}",
        schedule_time="08:00",
        auto_signin=True,
        auto_resign=True,
        auto_milestone=True,
    )
    return replace(account, **changes)


def make_store(*accounts: AccountRecord) -> AccountStore:
    store = AccountStore(MemoryPluginStorage())

    async def save_all() -> None:
        for account in accounts:
            await store.save(account)

    run(save_all())
    return store


def test_near_expiry_refreshes_before_operation_and_persists_credentials() -> None:
    session_class, _, _ = session_types()
    old_credentials = make_credentials(
        access_token_valid_time=int(NOW.timestamp() * 1000) + 59_000,
    )
    new_credentials = replace(
        old_credentials,
        token="ACCESS_TOKEN_SESSION_NEW",
        refresh_token="REFRESH_TOKEN_SESSION_NEW",
        access_token_valid_time=int(NOW.timestamp() * 1000) + 3_600_000,
    )
    account = make_account(credentials=old_credentials)
    store = make_store(account)
    client = FakeClient(new_credentials)
    seen_tokens: list[str] = []
    session = session_class(
        store,
        client_factory=lambda _: client,
        now=lambda: NOW,
    )

    async def operation(current: AccountRecord, _: FakeClient) -> str:
        seen_tokens.append(current.credentials.token)
        return "operation-result"

    result = run(session.execute(account.bot_uuid, account.sender_id, operation))

    assert result == "operation-result"
    assert client.refresh_calls == 1
    assert seen_tokens == [new_credentials.token]
    saved = run(store.get(account.bot_uuid, account.sender_id))
    assert saved is not None
    assert saved.credentials == new_credentials
    assert saved.needs_rebind is False
    assert client.closed is True


def test_first_auth_failure_refreshes_and_retries_whole_operation_once() -> None:
    session_class, _, _ = session_types()
    old_credentials = make_credentials(access_token_valid_time=0)
    new_credentials = replace(
        old_credentials,
        token="ACCESS_TOKEN_AUTH_NEW",
        refresh_token="REFRESH_TOKEN_AUTH_NEW",
        access_token_valid_time=int(NOW.timestamp() * 1000) + 3_600_000,
    )
    account = make_account(credentials=old_credentials)
    store = make_store(account)
    client = FakeClient(new_credentials)
    operation_tokens: list[str] = []
    session = session_class(store, client_factory=lambda _: client, now=lambda: NOW)

    async def operation(current: AccountRecord, _: FakeClient) -> str:
        operation_tokens.append(current.credentials.token)
        if len(operation_tokens) == 1:
            raise WendaoAuthError("expired", retryable=False)
        return "retried-result"

    result = run(session.execute(account.bot_uuid, account.sender_id, operation))

    assert result == "retried-result"
    assert operation_tokens == [old_credentials.token, new_credentials.token]
    assert client.refresh_calls == 1
    assert client.closed is True
    saved = run(store.get(account.bot_uuid, account.sender_id))
    assert saved is not None
    assert saved.credentials == new_credentials


def test_near_expiry_refresh_then_first_auth_failure_retries_without_second_refresh() -> None:
    session_class, _, _ = session_types()
    old_credentials = make_credentials(
        access_token_valid_time=int(NOW.timestamp() * 1000) + 59_000,
    )
    new_credentials = replace(
        old_credentials,
        token="ACCESS_TOKEN_PREFRESH_NEW",
        refresh_token="REFRESH_TOKEN_PREFRESH_NEW",
        access_token_valid_time=int(NOW.timestamp() * 1000) + 3_600_000,
    )
    account = make_account(credentials=old_credentials)
    store = make_store(account)
    client = FakeClient(new_credentials)
    operation_tokens: list[str] = []
    session = session_class(store, client_factory=lambda _: client, now=lambda: NOW)

    async def operation(current: AccountRecord, _: FakeClient) -> str:
        operation_tokens.append(current.credentials.token)
        if len(operation_tokens) == 1:
            raise WendaoAuthError("expired after pre-refresh", retryable=False)
        return "retried-result"

    result = run(session.execute(account.bot_uuid, account.sender_id, operation))

    assert result == "retried-result"
    assert operation_tokens == [new_credentials.token, new_credentials.token]
    assert client.refresh_calls == 1
    assert client.closed is True


def test_second_auth_failure_marks_rebind_after_exactly_one_refresh() -> None:
    session_class, _, _ = session_types()
    old_credentials = make_credentials(access_token_valid_time=0)
    new_credentials = replace(
        old_credentials,
        token="ACCESS_TOKEN_SECOND_AUTH_NEW",
        refresh_token="REFRESH_TOKEN_SECOND_AUTH_NEW",
        access_token_valid_time=int(NOW.timestamp() * 1000) + 3_600_000,
    )
    account = make_account(credentials=old_credentials)
    store = make_store(account)
    client = FakeClient(new_credentials)
    operation_tokens: list[str] = []
    session = session_class(store, client_factory=lambda _: client, now=lambda: NOW)

    async def operation(current: AccountRecord, _: FakeClient) -> None:
        operation_tokens.append(current.credentials.token)
        raise WendaoAuthError("still expired", retryable=False)

    with pytest.raises(WendaoAuthError) as exc_info:
        run(session.execute(account.bot_uuid, account.sender_id, operation))

    assert operation_tokens == [old_credentials.token, new_credentials.token]
    assert client.refresh_calls == 1
    assert exc_info.value.credentials_fingerprint == credentials_fingerprint(
        new_credentials
    )
    saved = run(store.get(account.bot_uuid, account.sender_id))
    assert saved is not None
    assert saved.credentials == new_credentials
    assert saved.needs_rebind is True
    assert client.closed is True


def test_missing_refresh_token_marks_rebind_without_creating_client() -> None:
    session_class, _, needs_rebind_error = session_types()
    credentials = make_credentials(
        refresh_token="",
        access_token_valid_time=int(NOW.timestamp() * 1000) + 59_000,
    )
    account = make_account(credentials=credentials)
    store = make_store(account)
    client_created = False

    def client_factory(_: AccountRecord) -> FakeClient:
        nonlocal client_created
        client_created = True
        return FakeClient()

    session = session_class(store, client_factory=client_factory, now=lambda: NOW)

    async def operation(_: AccountRecord, __: FakeClient) -> None:
        raise AssertionError("缺少刷新令牌时不应执行账号操作")

    with pytest.raises(needs_rebind_error, match="登录响应"):
        run(session.execute(account.bot_uuid, account.sender_id, operation))

    assert client_created is False
    saved = run(store.get(account.bot_uuid, account.sender_id))
    assert saved is not None
    assert saved.needs_rebind is True


def test_refresh_network_error_does_not_mark_rebind_and_closes_client() -> None:
    session_class, _, _ = session_types()
    credentials = make_credentials(
        access_token_valid_time=int(NOW.timestamp() * 1000) + 59_000,
    )
    account = make_account(credentials=credentials)
    store = make_store(account)
    client = FakeClient(WendaoRetryableError("network", retryable=True))
    session = session_class(store, client_factory=lambda _: client, now=lambda: NOW)

    async def operation(_: AccountRecord, __: FakeClient) -> None:
        raise AssertionError("刷新失败后不应执行账号操作")

    with pytest.raises(WendaoRetryableError) as exc_info:
        run(session.execute(account.bot_uuid, account.sender_id, operation))

    assert exc_info.value.credentials_fingerprint == credentials_fingerprint(credentials)
    saved = run(store.get(account.bot_uuid, account.sender_id))
    assert saved is not None
    assert saved.credentials == credentials
    assert saved.needs_rebind is False
    assert client.closed is True


@pytest.mark.parametrize(
    "refresh_error",
    [
        WendaoAuthError("refresh expired", retryable=False),
        WendaoBusinessError("refresh rejected", retryable=False, code=4001),
    ],
)
def test_nonretryable_refresh_error_marks_rebind(
    refresh_error: Exception,
) -> None:
    session_class, _, needs_rebind_error = session_types()
    credentials = make_credentials(
        access_token_valid_time=int(NOW.timestamp() * 1000) + 59_000,
    )
    account = make_account(credentials=credentials)
    store = make_store(account)
    client = FakeClient(refresh_error)
    session = session_class(store, client_factory=lambda _: client, now=lambda: NOW)

    async def operation(_: AccountRecord, __: FakeClient) -> None:
        raise AssertionError("刷新失败后不应执行账号操作")

    with pytest.raises(needs_rebind_error):
        run(session.execute(account.bot_uuid, account.sender_id, operation))

    saved = run(store.get(account.bot_uuid, account.sender_id))
    assert saved is not None
    assert saved.needs_rebind is True
    assert client.closed is True


def test_error_after_refresh_carries_refreshed_credentials_fingerprint() -> None:
    session_class, _, _ = session_types()
    old_credentials = make_credentials(access_token_valid_time=0)
    new_credentials = replace(
        old_credentials,
        token="ACCESS_TOKEN_ERROR_NEW",
        refresh_token="REFRESH_TOKEN_ERROR_NEW",
        access_token_valid_time=int(NOW.timestamp() * 1000) + 3_600_000,
    )
    account = make_account(credentials=old_credentials)
    store = make_store(account)
    client = FakeClient(new_credentials)
    attempts = 0
    session = session_class(store, client_factory=lambda _: client, now=lambda: NOW)

    async def operation(_: AccountRecord, __: FakeClient) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise WendaoAuthError("expired", retryable=False)
        raise WendaoRetryableError("network", retryable=True)

    with pytest.raises(WendaoRetryableError) as exc_info:
        run(session.execute(account.bot_uuid, account.sender_id, operation))

    assert attempts == 2
    assert exc_info.value.credentials_fingerprint == credentials_fingerprint(
        new_credentials
    )
    assert client.closed is True


def test_client_closes_when_domain_operation_fails() -> None:
    session_class, _, _ = session_types()
    account = make_account()
    store = make_store(account)
    client = FakeClient()
    session = session_class(store, client_factory=lambda _: client, now=lambda: NOW)

    async def operation(_: AccountRecord, __: FakeClient) -> None:
        raise WendaoBusinessError("domain failure", retryable=False, code=4102)

    with pytest.raises(WendaoBusinessError):
        run(session.execute(account.bot_uuid, account.sender_id, operation))

    assert client.closed is True


def test_same_account_operations_are_serialized_by_account_lock() -> None:
    session_class, _, _ = session_types()
    account = make_account()
    store = make_store(account)
    clients: list[FakeClient] = []
    active = 0
    max_active = 0

    def client_factory(_: AccountRecord) -> FakeClient:
        client = FakeClient()
        clients.append(client)
        return client

    session = session_class(
        store,
        client_factory=client_factory,
        max_concurrency=3,
        now=lambda: NOW,
    )

    async def operation(_: AccountRecord, __: FakeClient) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1

    async def scenario() -> None:
        await asyncio.gather(
            session.execute(account.bot_uuid, account.sender_id, operation),
            session.execute(account.bot_uuid, account.sender_id, operation),
        )

    run(scenario())

    assert max_active == 1
    assert len(clients) == 2
    assert all(client.closed for client in clients)


def test_global_semaphore_limits_different_accounts() -> None:
    session_class, _, _ = session_types()
    first = make_account(bot_uuid="bot-session-a", sender_id="user-session-a")
    second = make_account(bot_uuid="bot-session-b", sender_id="user-session-b")
    store = make_store(first, second)
    active = 0
    max_active = 0
    session = session_class(
        store,
        client_factory=lambda _: FakeClient(),
        max_concurrency=1,
        now=lambda: NOW,
    )

    async def operation(_: AccountRecord, __: FakeClient) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1

    async def scenario() -> None:
        await asyncio.gather(
            session.execute(first.bot_uuid, first.sender_id, operation),
            session.execute(second.bot_uuid, second.sender_id, operation),
        )

    run(scenario())

    assert max_active == 1


def test_missing_and_previously_invalid_accounts_fail_before_client_creation() -> None:
    session_class, not_bound_error, needs_rebind_error = session_types()
    invalid = make_account(needs_rebind=True)
    store = make_store(invalid)
    client_created = False

    def client_factory(_: AccountRecord) -> FakeClient:
        nonlocal client_created
        client_created = True
        return FakeClient()

    session = session_class(store, client_factory=client_factory)

    async def operation(_: AccountRecord, __: FakeClient) -> None:
        raise AssertionError("无有效绑定时不应执行账号操作")

    with pytest.raises(not_bound_error):
        run(session.execute("missing-bot", "missing-user", operation))
    with pytest.raises(needs_rebind_error):
        run(session.execute(invalid.bot_uuid, invalid.sender_id, operation))

    assert client_created is False

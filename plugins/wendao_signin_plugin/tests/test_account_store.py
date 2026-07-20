from __future__ import annotations

import asyncio
from dataclasses import replace
import json

from components.account_store import AccountStore
from components.models import AccountRecord, Credentials


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


def run(coro):
    return asyncio.run(coro)


def credentials(token: str = "TOKEN_TEST_FIRST") -> Credentials:
    return Credentials(
        token=token,
        device="DEVICE_TEST/Android/16",
        version="2.26.1",
        version_code="260604",
        guest_id="1030000000000000",
        client_type="wd_android",
    )


def account(token: str = "TOKEN_TEST_FIRST") -> AccountRecord:
    return AccountRecord.create(
        bot_uuid="bot-test-1",
        sender_id="user-test-1",
        credentials=credentials(token),
        target_id="person-target-1",
        schedule_time="08:00",
        auto_signin=True,
        auto_resign=True,
        auto_milestone=True,
        nickname="测试用户",
        user_identifier="user-remote-1",
    )


def test_save_and_get_round_trip_all_account_state() -> None:
    backend = MemoryPluginStorage()
    store = AccountStore(backend)
    original = replace(
        account(),
        credentials=replace(
            credentials(),
            refresh_token="REFRESH_TEST_STORAGE",
            access_token_valid_time=1784219114121,
        ),
        next_retry_at="2026-07-16T08:05:00+08:00",
        retry_count=1,
        retry_origin_date="2026-07-16",
        last_completed_date="2026-07-15",
        last_resign_attempt_date="2026-07-15",
        last_result="签到完成",
    )

    run(store.save(original))
    loaded = run(store.get("bot-test-1", "user-test-1"))

    assert loaded == original
    [key] = backend.values
    assert key.startswith("account:v1:")
    assert "bot-test-1" not in key
    assert "user-test-1" not in key
    assert backend.values[key].startswith(b"{")


def test_save_overwrites_same_identity_and_keeps_other_bot_isolated() -> None:
    backend = MemoryPluginStorage()
    store = AccountStore(backend)
    first = account()
    replacement = replace(first, credentials=credentials("TOKEN_TEST_REPLACEMENT"))
    other_bot = replace(first, bot_uuid="bot-test-2")

    run(store.save(first))
    run(store.save(replacement))
    run(store.save(other_bot))

    assert len(backend.values) == 2
    assert run(store.get("bot-test-1", "user-test-1")).credentials.token == (
        "TOKEN_TEST_REPLACEMENT"
    )
    assert run(store.get("bot-test-2", "user-test-1")) == other_bot


def test_existing_v1_record_defaults_weekly_report_fields_without_migration() -> None:
    backend = MemoryPluginStorage()
    store = AccountStore(backend)
    run(store.save(account()))
    [key] = backend.values
    payload = json.loads(backend.values[key].decode("utf-8"))
    for field in (
        "auto_weekly_report",
        "last_weekly_report_period",
        "weekly_report_next_retry_at",
        "weekly_report_retry_count",
        "weekly_report_retry_origin_period",
        "weekly_report_last_attempt_date",
        "weekly_report_last_run_at",
        "weekly_report_last_result",
    ):
        payload.pop(field)
    backend.values[key] = json.dumps(payload).encode("utf-8")

    loaded = run(store.get("bot-test-1", "user-test-1"))

    assert loaded.auto_weekly_report is True
    assert loaded.last_weekly_report_period == ""
    assert loaded.weekly_report_retry_count == 0


def test_list_accounts_ignores_other_storage_and_delete_unbinds() -> None:
    backend = MemoryPluginStorage()
    backend.values["other:key"] = b"not-an-account"
    store = AccountStore(backend)
    original = account()
    run(store.save(original))

    assert run(store.list_accounts()) == [original]
    assert run(store.delete("bot-test-1", "user-test-1")) is True
    assert run(store.get("bot-test-1", "user-test-1")) is None
    assert run(store.delete("bot-test-1", "user-test-1")) is False

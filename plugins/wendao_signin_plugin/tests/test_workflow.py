from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from components.account_store import AccountStore
from components.api_client import (
    WendaoAuthError,
    WendaoBusinessError,
    WendaoRetryableError,
)
from components.models import (
    AccountRecord,
    ApiResponse,
    Credentials,
    credentials_fingerprint,
)
from components.workflow import AccountNeedsRebindError, SigninWorkflow


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
    def __init__(
        self,
        list_results: list[dict | Exception],
        *,
        resign_error: Exception | None = None,
        resign_results: list[ApiResponse | Exception] | None = None,
        report_read_error: Exception | None = None,
        refresh_result: Credentials | Exception | None = None,
    ) -> None:
        self.list_results = list(list_results)
        self.resign_error = resign_error
        self.resign_results = list(resign_results or [])
        self.report_read_error = report_read_error
        self.refresh_result = refresh_result
        self.signin_calls: list[int] = []
        self.report_read_calls: list[str] = []
        self.claim_calls = 0
        self.refresh_calls = 0
        self.events: list[str] = []
        self.closed = False

    async def list_signin(self) -> ApiResponse:
        self.events.append("list")
        if not self.list_results:
            raise AssertionError("测试未提供足够的列表响应")
        result = self.list_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return ApiResponse(code=2000, data=result)

    async def signin(self, signin_type: int) -> ApiResponse:
        self.signin_calls.append(signin_type)
        if signin_type == 2 and self.resign_results:
            result = self.resign_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        if signin_type == 2 and self.resign_error is not None:
            raise self.resign_error
        return ApiResponse(code=2000, data={"award": {"name": "测试奖励", "num": 1}})

    async def report_post_read(self, source_id: str) -> ApiResponse:
        self.report_read_calls.append(source_id)
        if self.report_read_error is not None:
            raise self.report_read_error
        return ApiResponse(code=2000, data={})

    async def claim_milestone(self) -> ApiResponse:
        self.claim_calls += 1
        return ApiResponse(code=2000, data={"name": "里程碑奖励"})

    async def refresh_access_token(self) -> Credentials:
        self.refresh_calls += 1
        self.events.append("refresh")
        if isinstance(self.refresh_result, Exception):
            raise self.refresh_result
        if self.refresh_result is None:
            raise AssertionError("测试未提供刷新结果")
        return self.refresh_result

    async def aclose(self) -> None:
        self.closed = True


class StatefulClient:
    def __init__(self) -> None:
        self.signed = False
        self.signin_calls = 0

    async def list_signin(self) -> ApiResponse:
        return ApiResponse(
            code=2000,
            data={
                "signInStatus": 2 if self.signed else 1,
                "signNum": 1 if self.signed else 0,
                "reSignInStatus": 4,
                "reSignNum": 0,
                "reSignNumLimit": 1,
                "milestoneData": {"state": 1},
            },
        )

    async def signin(self, signin_type: int) -> ApiResponse:
        assert signin_type == 1
        self.signin_calls += 1
        await asyncio.sleep(0.01)
        self.signed = True
        return ApiResponse(code=2000, data={})

    async def claim_milestone(self) -> ApiResponse:
        raise AssertionError("不应领取里程碑")

    async def aclose(self) -> None:
        pass


def run(coro):
    return asyncio.run(coro)


def make_account(**changes) -> AccountRecord:
    base = AccountRecord.create(
        bot_uuid="bot-test-1",
        sender_id="user-test-1",
        credentials=Credentials(
            token="TOKEN_TEST_WORKFLOW",
            device="DEVICE_TEST/Android/16",
            version="2.26.1",
            version_code="260604",
            guest_id="1030000000000000",
            client_type="wd_android",
        ),
        target_id="person-target-1",
        schedule_time="08:00",
        auto_signin=True,
        auto_resign=True,
        auto_milestone=True,
    )
    return replace(base, **changes)


def status(
    *,
    signed: bool = True,
    can_resign: bool = False,
    resign_num: int = 0,
    resign_limit: int = 1,
    milestone_state: int = 1,
) -> dict:
    return {
        "signInStatus": 2 if signed else 1,
        "signNum": 5 if signed else 4,
        "reSignInStatus": 1 if can_resign else 4,
        "reSignNum": resign_num,
        "reSignNumLimit": resign_limit,
        "milestoneData": {"state": milestone_state},
    }


def setup_workflow(
    client_factory: Callable[[AccountRecord], object],
    account: AccountRecord | None = None,
) -> tuple[AccountStore, SigninWorkflow]:
    store = AccountStore(MemoryPluginStorage())
    run(store.save(account or make_account()))
    workflow = SigninWorkflow(
        store,
        client_factory=client_factory,
        max_concurrency=3,
        now=lambda: NOW,
    )
    return store, workflow


def test_auto_signs_in_then_rechecks_final_status() -> None:
    client = FakeClient(
        [
            status(signed=False),
            status(signed=True),
            status(signed=True),
        ]
    )
    account = make_account(auto_resign=False, auto_milestone=False)
    store, workflow = setup_workflow(lambda record: client, account)

    outcome = run(workflow.execute("bot-test-1", "user-test-1", mode="auto"))

    assert client.signin_calls == [1]
    assert outcome.actions == ("signin",)
    assert outcome.run_date == "2026-07-16"
    assert outcome.status["signInStatus"] == 2
    saved = run(store.get("bot-test-1", "user-test-1"))
    assert saved.last_completed_date == "2026-07-16"
    assert saved.retry_count == 0
    assert saved.next_retry_at == ""
    assert client.closed is True


def test_query_never_mutates_signin_state() -> None:
    client = FakeClient([status(signed=False, can_resign=True, milestone_state=3)])
    _, workflow = setup_workflow(lambda record: client)

    outcome = run(workflow.execute("bot-test-1", "user-test-1", mode="query"))

    assert outcome.actions == ()
    assert client.signin_calls == []
    assert client.claim_calls == 0


def test_auto_resign_attempts_at_most_once_per_shanghai_day() -> None:
    first_client = FakeClient([status(can_resign=True), status(), status()])
    second_client = FakeClient([status(can_resign=True), status(can_resign=True)])
    clients = iter([first_client, second_client])
    store, workflow = setup_workflow(lambda record: next(clients))

    first = run(workflow.execute("bot-test-1", "user-test-1", mode="auto"))
    second = run(workflow.execute("bot-test-1", "user-test-1", mode="auto"))

    assert first.actions == ("resign",)
    assert second.actions == ()
    assert first_client.signin_calls == [2]
    assert second_client.signin_calls == []
    saved = run(store.get("bot-test-1", "user-test-1"))
    assert saved.last_resign_attempt_date == "2026-07-16"


def test_resign_4102_publish_task_still_returns_app_prompt() -> None:
    error = WendaoBusinessError(
        "业务错误 4102",
        retryable=False,
        code=4102,
        data={"reSignData": {"type": 1, "title": "测试任务"}},
    )
    client = FakeClient(
        [status(can_resign=True), status(can_resign=True)],
        resign_error=error,
    )
    _, workflow = setup_workflow(lambda record: client)

    outcome = run(workflow.execute("bot-test-1", "user-test-1", mode="resign"))

    assert outcome.actions == ("resign_task",)
    assert "发布动态" in outcome.message
    assert "App" in outcome.message
    assert client.report_read_calls == []


def test_resign_4102_read_task_reports_article_then_retries_once() -> None:
    read_task = WendaoBusinessError(
        "业务错误 4102",
        retryable=False,
        code=4102,
        data={"reSignData": {"type": 2, "sourceId": "30555"}},
    )
    client = FakeClient(
        [
            status(can_resign=True),
            status(can_resign=False, resign_num=1),
            status(can_resign=False, resign_num=1),
        ],
        resign_results=[read_task, ApiResponse(code=2000, data={})],
    )
    _, workflow = setup_workflow(lambda record: client)

    outcome = run(workflow.execute("bot-test-1", "user-test-1", mode="resign"))

    assert client.signin_calls == [2, 2]
    assert client.report_read_calls == ["30555"]
    assert outcome.actions == ("resign",)
    assert "补签成功" in outcome.message


def test_resign_read_task_without_source_id_keeps_manual_prompt() -> None:
    read_task = WendaoBusinessError(
        "业务错误 4102",
        retryable=False,
        code=4102,
        data={"reSignData": {"type": 2}},
    )
    client = FakeClient(
        [status(can_resign=True), status(can_resign=True)],
        resign_results=[read_task],
    )
    _, workflow = setup_workflow(lambda record: client)

    outcome = run(workflow.execute("bot-test-1", "user-test-1", mode="resign"))

    assert client.signin_calls == [2]
    assert client.report_read_calls == []
    assert outcome.actions == ("resign_task",)
    assert "阅读指定文章" in outcome.message


def test_resign_read_task_second_4102_stops_after_one_retry() -> None:
    first_task = WendaoBusinessError(
        "业务错误 4102",
        retryable=False,
        code=4102,
        data={"reSignData": {"type": 2, "sourceId": "30555"}},
    )
    second_task = WendaoBusinessError(
        "业务错误 4102",
        retryable=False,
        code=4102,
        data={"reSignData": {"type": 2, "sourceId": "30555"}},
    )
    client = FakeClient(
        [status(can_resign=True), status(can_resign=True)],
        resign_results=[first_task, second_task],
    )
    _, workflow = setup_workflow(lambda record: client)

    outcome = run(workflow.execute("bot-test-1", "user-test-1", mode="resign"))

    assert client.signin_calls == [2, 2]
    assert client.report_read_calls == ["30555"]
    assert outcome.actions == ("resign_task",)
    assert "已上报阅读" in outcome.message


def test_resign_read_report_network_error_remains_retryable() -> None:
    read_task = WendaoBusinessError(
        "业务错误 4102",
        retryable=False,
        code=4102,
        data={"reSignData": {"type": 2, "sourceId": "30555"}},
    )
    client = FakeClient(
        [status(can_resign=True), status(can_resign=True)],
        resign_results=[read_task],
        report_read_error=WendaoRetryableError("network", retryable=True),
    )
    _, workflow = setup_workflow(lambda record: client)

    with pytest.raises(WendaoRetryableError):
        run(workflow.execute("bot-test-1", "user-test-1", mode="resign"))

    assert client.signin_calls == [2]
    assert client.report_read_calls == ["30555"]


def test_monthly_resign_limit_prevents_request() -> None:
    client = FakeClient([status(can_resign=True, resign_num=2, resign_limit=2), status()])
    _, workflow = setup_workflow(lambda record: client)

    outcome = run(workflow.execute("bot-test-1", "user-test-1", mode="resign"))

    assert client.signin_calls == []
    assert "补签次数" in outcome.message


def test_milestone_state_get_claims_once_per_run() -> None:
    client = FakeClient([status(milestone_state=3), status(milestone_state=4)])
    account = make_account(auto_signin=False, auto_resign=False)
    _, workflow = setup_workflow(lambda record: client, account)

    outcome = run(workflow.execute("bot-test-1", "user-test-1", mode="auto"))

    assert client.claim_calls == 1
    assert outcome.actions == ("milestone",)


def test_auth_failure_without_refresh_marks_rebind_and_retryable_error_propagates() -> None:
    auth_client = FakeClient([WendaoAuthError("expired", retryable=False)])
    store, auth_workflow = setup_workflow(lambda record: auth_client)

    with pytest.raises(AccountNeedsRebindError):
        run(auth_workflow.execute("bot-test-1", "user-test-1", mode="query"))
    assert run(store.get("bot-test-1", "user-test-1")).needs_rebind is True

    retry_client = FakeClient([WendaoRetryableError("network", retryable=True)])
    _, retry_workflow = setup_workflow(lambda record: retry_client)
    with pytest.raises(WendaoRetryableError):
        run(retry_workflow.execute("bot-test-1", "user-test-1", mode="query"))


def test_expired_legacy_binding_without_refresh_token_requires_rebind() -> None:
    account = make_account(
        credentials=replace(
            make_account().credentials,
            access_token_valid_time=int(NOW.timestamp() * 1000) - 1,
        )
    )
    client_created = False

    def client_factory(record: AccountRecord):
        nonlocal client_created
        client_created = True
        raise AssertionError("缺少刷新令牌时不应创建 HTTP 客户端")

    store, workflow = setup_workflow(client_factory, account)

    with pytest.raises(AccountNeedsRebindError, match="登录响应"):
        run(workflow.execute("bot-test-1", "user-test-1", mode="query"))

    assert client_created is False
    assert run(store.get("bot-test-1", "user-test-1")).needs_rebind is True


def test_access_token_near_expiry_refreshes_before_query_and_persists_credentials() -> None:
    old_credentials = replace(
        make_account().credentials,
        refresh_token="REFRESH_TOKEN_WORKFLOW_OLD",
        access_token_valid_time=int(NOW.timestamp() * 1000) + 59_000,
    )
    new_credentials = replace(
        old_credentials,
        token="ACCESS_TOKEN_WORKFLOW_NEW",
        refresh_token="REFRESH_TOKEN_WORKFLOW_NEW",
        access_token_valid_time=int(NOW.timestamp() * 1000) + 3_600_000,
    )
    account = make_account(credentials=old_credentials)
    client = FakeClient([status()], refresh_result=new_credentials)
    store, workflow = setup_workflow(lambda record: client, account)

    outcome = run(workflow.execute("bot-test-1", "user-test-1", mode="query"))

    assert outcome.status["signInStatus"] == 2
    assert client.events == ["refresh", "list"]
    assert outcome.credentials_fingerprint == credentials_fingerprint(new_credentials)
    saved = run(store.get("bot-test-1", "user-test-1"))
    assert saved.credentials == new_credentials
    assert saved.needs_rebind is False


def test_first_query_auth_failure_refreshes_and_restarts_workflow_once() -> None:
    old_credentials = replace(
        make_account().credentials,
        refresh_token="REFRESH_TOKEN_AUTH_OLD",
    )
    new_credentials = replace(
        old_credentials,
        token="ACCESS_TOKEN_AUTH_NEW",
        refresh_token="REFRESH_TOKEN_AUTH_NEW",
        access_token_valid_time=int(NOW.timestamp() * 1000) + 3_600_000,
    )
    client = FakeClient(
        [WendaoAuthError("expired", retryable=False), status()],
        refresh_result=new_credentials,
    )
    store, workflow = setup_workflow(
        lambda record: client,
        make_account(credentials=old_credentials),
    )

    outcome = run(workflow.execute("bot-test-1", "user-test-1", mode="query"))

    assert outcome.status["signInStatus"] == 2
    assert client.events == ["list", "refresh", "list"]
    assert client.refresh_calls == 1
    assert run(store.get("bot-test-1", "user-test-1")).credentials == new_credentials


def test_auto_resign_auth_failure_replays_original_domain_state_after_refresh() -> None:
    old_credentials = replace(
        make_account().credentials,
        refresh_token="REFRESH_TOKEN_RESIGN_OLD",
    )
    new_credentials = replace(
        old_credentials,
        token="ACCESS_TOKEN_RESIGN_NEW",
        refresh_token="REFRESH_TOKEN_RESIGN_NEW",
        access_token_valid_time=int(NOW.timestamp() * 1000) + 3_600_000,
    )
    client = FakeClient(
        [
            status(can_resign=True),
            status(can_resign=True),
            status(can_resign=False, resign_num=1),
            status(can_resign=False, resign_num=1),
        ],
        resign_results=[
            WendaoAuthError("expired", retryable=False),
            ApiResponse(code=2000, data={}),
        ],
        refresh_result=new_credentials,
    )
    store, workflow = setup_workflow(
        lambda record: client,
        make_account(credentials=old_credentials),
    )

    outcome = run(workflow.execute("bot-test-1", "user-test-1", mode="auto"))

    assert client.signin_calls == [2, 2]
    assert client.refresh_calls == 1
    assert outcome.actions == ("resign",)
    saved = run(store.get("bot-test-1", "user-test-1"))
    assert saved.credentials == new_credentials
    assert saved.last_resign_attempt_date == "2026-07-16"
    assert saved.last_completed_date == "2026-07-16"


def test_retryable_error_after_refresh_carries_refreshed_credentials_fingerprint() -> None:
    old_credentials = replace(
        make_account().credentials,
        refresh_token="REFRESH_TOKEN_RETRY_OLD",
    )
    new_credentials = replace(
        old_credentials,
        token="ACCESS_TOKEN_RETRY_NEW",
        refresh_token="REFRESH_TOKEN_RETRY_NEW",
        access_token_valid_time=int(NOW.timestamp() * 1000) + 3_600_000,
    )
    client = FakeClient(
        [
            WendaoAuthError("expired", retryable=False),
            WendaoRetryableError("network", retryable=True),
        ],
        refresh_result=new_credentials,
    )
    store, workflow = setup_workflow(
        lambda record: client,
        make_account(credentials=old_credentials),
    )

    with pytest.raises(WendaoRetryableError) as exc_info:
        run(workflow.execute("bot-test-1", "user-test-1", mode="query"))

    assert exc_info.value.credentials_fingerprint == credentials_fingerprint(
        new_credentials
    )
    assert run(store.get("bot-test-1", "user-test-1")).credentials == new_credentials


def test_refresh_network_failure_propagates_without_marking_rebind() -> None:
    credentials = replace(
        make_account().credentials,
        refresh_token="REFRESH_TOKEN_NETWORK",
        access_token_valid_time=int(NOW.timestamp() * 1000) - 1,
    )
    client = FakeClient(
        [],
        refresh_result=WendaoRetryableError("network", retryable=True),
    )
    store, workflow = setup_workflow(
        lambda record: client,
        make_account(credentials=credentials),
    )

    with pytest.raises(WendaoRetryableError):
        run(workflow.execute("bot-test-1", "user-test-1", mode="query"))

    saved = run(store.get("bot-test-1", "user-test-1"))
    assert saved.credentials == credentials
    assert saved.needs_rebind is False


@pytest.mark.parametrize(
    "refresh_error",
    [
        WendaoAuthError("refresh expired", retryable=False),
        WendaoBusinessError("refresh rejected", retryable=False, code=4001),
    ],
)
def test_nonretryable_refresh_failure_marks_account_for_rebind(
    refresh_error: Exception,
) -> None:
    credentials = replace(
        make_account().credentials,
        refresh_token="REFRESH_TOKEN_REJECTED",
        access_token_valid_time=int(NOW.timestamp() * 1000) - 1,
    )
    client = FakeClient([], refresh_result=refresh_error)
    store, workflow = setup_workflow(
        lambda record: client,
        make_account(credentials=credentials),
    )

    with pytest.raises(AccountNeedsRebindError):
        run(workflow.execute("bot-test-1", "user-test-1", mode="query"))

    assert client.refresh_calls == 1
    assert run(store.get("bot-test-1", "user-test-1")).needs_rebind is True


def test_manual_and_auto_concurrency_only_sign_once() -> None:
    client = StatefulClient()
    account = make_account(auto_resign=False, auto_milestone=False)
    _, workflow = setup_workflow(lambda record: client, account)

    async def concurrent_run() -> None:
        await asyncio.gather(
            workflow.execute("bot-test-1", "user-test-1", mode="signin"),
            workflow.execute("bot-test-1", "user-test-1", mode="auto"),
        )

    run(concurrent_run())

    assert client.signin_calls == 1

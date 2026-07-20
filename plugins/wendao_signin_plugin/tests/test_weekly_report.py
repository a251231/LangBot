from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from components.account_session import AuthenticatedAccountSession
from components.account_store import AccountStore
from components.api_client import WendaoAuthError, WendaoRetryableError
from components.models import AccountRecord, ApiResponse, Credentials
from components.weekly_report import (
    WeeklyReportNotReadyError,
    WeeklyReportService,
    expected_weekly_report_period,
    format_weekly_report,
)
from components.weekly_report_client import (
    WeeklyReportTicket,
    WeeklyReportTokenError,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 13, 9, 0, tzinfo=SHANGHAI)


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


class FakeCommunityClient:
    def __init__(self, action_urls: list[str]) -> None:
        self.action_urls = list(action_urls)
        self.ticket_calls = 0
        self.closed = False

    async def get_third_url(self) -> ApiResponse:
        self.ticket_calls += 1
        return ApiResponse(
            code=2000,
            data={"actionUrl": self.action_urls.pop(0)},
        )

    async def refresh_access_token(self) -> Credentials:
        raise AssertionError("本测试不应刷新社区凭据")

    async def aclose(self) -> None:
        self.closed = True


class FakeActivityClient:
    def __init__(self, results: list[dict[str, Any] | Exception]) -> None:
        self.results = list(results)
        self.tickets: list[WeeklyReportTicket] = []
        self.closed = False

    async def fetch(self, ticket: WeeklyReportTicket) -> dict[str, Any]:
        self.tickets.append(ticket)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def aclose(self) -> None:
        self.closed = True


class RefreshingCommunityClient(FakeCommunityClient):
    def __init__(self, action_url_value: str, refreshed: Credentials) -> None:
        super().__init__([action_url_value])
        self.refreshed = refreshed
        self.refresh_calls = 0

    async def get_third_url(self) -> ApiResponse:
        self.ticket_calls += 1
        if self.ticket_calls == 1:
            raise WendaoAuthError("expired", retryable=False)
        return ApiResponse(
            code=2000,
            data={"actionUrl": self.action_urls.pop(0)},
        )

    async def refresh_access_token(self) -> Credentials:
        self.refresh_calls += 1
        return self.refreshed


def run(coro):
    return asyncio.run(coro)


def make_account(**changes: Any) -> AccountRecord:
    base = AccountRecord.create(
        bot_uuid="bot-weekly-1",
        sender_id="user-weekly-1",
        credentials=Credentials(
            token="COMMUNITY_TOKEN_WEEKLY_TEST",
            device="DEVICE_WEEKLY/Android/16",
            version="2.26.1",
            version_code="260604",
            guest_id="1030000000000000",
            client_type="wd_android",
        ),
        target_id="target-weekly-1",
        schedule_time="08:00",
        auto_signin=True,
        auto_resign=True,
        auto_milestone=True,
    )
    return replace(base, **changes)


def report_payload(period: str = "2026年07月06日-2026年07月12日") -> dict[str, Any]:
    return {
        "user": {
            "week": period,
            "role_name": "测试角色",
            "zone_name": "测试区服",
            "level": "79",
        },
        "overview": {
            "login_days": "7",
            "cum_active": "1742",
            "active_over": "87.00",
            "shuad_times": "24384",
            "shuad_times_over": "99.00",
            "tao_add": "4872",
        },
        "highlights": [
            {"title": "成为中洲欧皇！", "content": ["获得女娲石<span>1个</span>！"]},
            {"title": "通天塔的神！", "content": ["通天<span>7次</span>！"]},
        ],
        "luckwords": {"words": "八仙引我入梦，梦境皆是福缘"},
    }


def action_url(token: str) -> str:
    return (
        "https://actscp01.leiting.com/wd/act/202405/hand/"
        "?sid=SID_WEEKLY&rid=RID_WEEKLY&role_name=ROLE_WEEKLY&zone_id=101"
        "&zone_name=ZONE_WEEKLY&channel_no=130008&polar=5&gender=FEMALE"
        f"&identify=xdzb&is_sub=1&token={token}"
    )


def setup_service(
    community: FakeCommunityClient,
    activity: FakeActivityClient,
    *,
    account: AccountRecord | None = None,
) -> tuple[AccountStore, WeeklyReportService, MemoryPluginStorage]:
    backend = MemoryPluginStorage()
    store = AccountStore(backend)
    run(store.save(account or make_account()))
    session = AuthenticatedAccountSession(
        store,
        client_factory=lambda _: community,
        now=lambda: NOW,
    )
    service = WeeklyReportService(
        store,
        session=session,
        activity_client_factory=lambda: activity,
        now=lambda: NOW,
    )
    return store, service, backend


def test_expected_weekly_report_period_is_previous_monday_through_sunday() -> None:
    assert expected_weekly_report_period(NOW) == "2026年07月06日-2026年07月12日"
    saturday = datetime(2026, 7, 18, 12, 0, tzinfo=SHANGHAI)
    assert expected_weekly_report_period(saturday) == "2026年07月06日-2026年07月12日"


def test_format_weekly_report_uses_fixed_title_and_strips_highlight_html() -> None:
    message = format_weekly_report(report_payload())

    assert message.splitlines()[0] == "问道周报"
    assert "测试角色｜测试区服｜79级" in message
    assert "周期：2026年07月06日-2026年07月12日" in message
    assert "登录天数：7天" in message
    assert "累计活跃：1742（达成 87.00%）" in message
    assert "刷道次数：24384（超过 99.00% 道友）" in message
    assert "道行增长：13年192天" in message
    assert "成为中洲欧皇！：获得女娲石1个！" in message
    assert "<span>" not in message
    assert "本周签语：八仙引我入梦，梦境皆是福缘" in message


@pytest.mark.parametrize(
    ("raw_days", "expected"),
    [
        ("0", "0天"),
        ("359", "359天"),
        ("360", "1年"),
        ("720", "2年"),
        ("4872", "13年192天"),
        ("未知", "未知"),
    ],
)
def test_format_weekly_report_converts_tao_days_using_360_day_year(
    raw_days: str,
    expected: str,
) -> None:
    payload = report_payload()
    payload["overview"]["tao_add"] = raw_days

    message = format_weekly_report(payload)

    assert f"道行增长：{expected}" in message


def test_manual_weekly_report_persists_period_without_activity_token() -> None:
    community = FakeCommunityClient([action_url("ACTIVITY_TOKEN_ONE")])
    activity = FakeActivityClient([report_payload()])
    store, service, backend = setup_service(community, activity)

    outcome = run(service.execute("bot-weekly-1", "user-weekly-1"))

    assert outcome.report_period == "2026年07月06日-2026年07月12日"
    assert outcome.message.startswith("问道周报\n")
    assert community.ticket_calls == 1
    assert community.closed is True
    assert activity.closed is True
    saved = run(store.get("bot-weekly-1", "user-weekly-1"))
    assert saved.last_weekly_report_period == outcome.report_period
    assert saved.weekly_report_last_result == outcome.message
    serialized = b"\n".join(backend.values.values())
    assert b"ACTIVITY_TOKEN_ONE" not in serialized


def test_auto_weekly_report_rechecks_period_inside_account_lock() -> None:
    period = "2026年07月06日-2026年07月12日"
    community = FakeCommunityClient([])
    activity = FakeActivityClient([])
    store, service, _ = setup_service(community, activity)
    existing = run(store.get("bot-weekly-1", "user-weekly-1"))
    run(
        store.save(
            replace(
                existing,
                last_weekly_report_period=period,
                weekly_report_last_result="问道周报\n手动查询已完成",
            )
        )
    )

    outcome = run(
        service.execute(
            "bot-weekly-1",
            "user-weekly-1",
            expected_period=period,
        )
    )

    assert outcome.already_completed is True
    assert outcome.message == "问道周报\n手动查询已完成"
    assert community.ticket_calls == 0
    assert community.closed is True
    assert activity.closed is False


def test_activity_token_failure_exchanges_ticket_once_then_succeeds() -> None:
    first_token = "ACTIVITY_TOKEN_FIRST"
    second_token = "ACTIVITY_TOKEN_SECOND"
    community = FakeCommunityClient(
        [action_url(first_token), action_url(second_token)]
    )
    activity = FakeActivityClient(
        [
            WeeklyReportTokenError("token invalid", retryable=False, code=0),
            report_payload(),
        ]
    )
    _, service, _ = setup_service(community, activity)

    outcome = run(service.execute("bot-weekly-1", "user-weekly-1"))

    assert outcome.report_period == "2026年07月06日-2026年07月12日"
    assert community.ticket_calls == 2
    assert [ticket.token for ticket in activity.tickets] == [
        first_token,
        second_token,
    ]


def test_community_auth_failure_refreshes_and_replays_weekly_report_once() -> None:
    old_account = make_account(
        credentials=replace(
            make_account().credentials,
            refresh_token="REFRESH_TOKEN_WEEKLY_OLD",
        )
    )
    refreshed = replace(
        old_account.credentials,
        token="COMMUNITY_TOKEN_WEEKLY_NEW",
        refresh_token="REFRESH_TOKEN_WEEKLY_NEW",
        access_token_valid_time=int(NOW.timestamp() * 1000) + 3_600_000,
    )
    community = RefreshingCommunityClient(
        action_url("ACTIVITY_TOKEN_AFTER_REFRESH"),
        refreshed,
    )
    activity = FakeActivityClient([report_payload()])
    store, service, _ = setup_service(
        community,
        activity,
        account=old_account,
    )

    outcome = run(service.execute("bot-weekly-1", "user-weekly-1"))

    assert outcome.report_period == "2026年07月06日-2026年07月12日"
    assert community.ticket_calls == 2
    assert community.refresh_calls == 1
    saved = run(store.get("bot-weekly-1", "user-weekly-1"))
    assert saved.credentials == refreshed


def test_second_activity_token_failure_stops_after_one_reexchange() -> None:
    community = FakeCommunityClient(
        [action_url("ACTIVITY_TOKEN_FIRST"), action_url("ACTIVITY_TOKEN_SECOND")]
    )
    activity = FakeActivityClient(
        [
            WeeklyReportTokenError("first", retryable=False, code=0),
            WeeklyReportTokenError("second", retryable=False, code=0),
        ]
    )
    _, service, _ = setup_service(community, activity)

    with pytest.raises(WeeklyReportTokenError):
        run(service.execute("bot-weekly-1", "user-weekly-1"))

    assert community.ticket_calls == 2
    assert len(activity.tickets) == 2
    assert community.closed is True
    assert activity.closed is True


def test_expected_period_mismatch_is_retryable_and_not_persisted() -> None:
    community = FakeCommunityClient([action_url("ACTIVITY_TOKEN_STALE")])
    activity = FakeActivityClient(
        [report_payload("2026年06月29日-2026年07月05日")]
    )
    store, service, _ = setup_service(community, activity)

    with pytest.raises(WeeklyReportNotReadyError) as exc_info:
        run(
            service.execute(
                "bot-weekly-1",
                "user-weekly-1",
                expected_period="2026年07月06日-2026年07月12日",
            )
        )

    assert isinstance(exc_info.value, WendaoRetryableError)
    saved = run(store.get("bot-weekly-1", "user-weekly-1"))
    assert saved.last_weekly_report_period == ""
    assert activity.closed is True

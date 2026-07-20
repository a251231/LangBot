from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

from components.api_client import WendaoBusinessError, WendaoRetryableError
from components.weekly_report import WeeklyReportNotReadyError
from components.weekly_report_client import (
    WeeklyReportClient,
    WeeklyReportTokenError,
    parse_weekly_report_ticket,
)


ACTION_URL = (
    "https://actscp01.leiting.com/wd/act/202405/hand/"
    "?rid=RID_TEST_001&role_name=ROLE_TEST&zone_id=101&zone_name=ZONE_TEST"
    "&channel_no=130008&polar=5&gender=FEMALE&identify=xdzb"
    "&sid=SID_TEST_001&token=ACTIVITY_TOKEN_TEST_001&is_sub=1"
)


def run(coro):
    return asyncio.run(coro)


def test_parse_weekly_report_ticket_accepts_only_confirmed_activity_url() -> None:
    ticket = parse_weekly_report_ticket(ACTION_URL)

    assert ticket.sid == "SID_TEST_001"
    assert ticket.rid == "RID_TEST_001"
    assert ticket.token == "ACTIVITY_TOKEN_TEST_001"
    assert ticket.user_data_params == {
        "sid": "SID_TEST_001",
        "rid": "RID_TEST_001",
        "role_name": "ROLE_TEST",
        "zone_id": "101",
        "zone_name": "ZONE_TEST",
        "channel_no": "130008",
        "polar": "5",
        "gender": "FEMALE",
        "identify": "xdzb",
        "is_sub": "1",
        "token": "ACTIVITY_TOKEN_TEST_001",
    }


@pytest.mark.parametrize(
    "url",
    [
        ACTION_URL.replace("actscp01.leiting.com", "example.test"),
        ACTION_URL.replace("/wd/act/202405/hand/", "/other/"),
        ACTION_URL.replace("&token=ACTIVITY_TOKEN_TEST_001", ""),
        ACTION_URL.replace("&identify=xdzb", ""),
    ],
)
def test_parse_weekly_report_ticket_rejects_untrusted_or_incomplete_url(url: str) -> None:
    with pytest.raises(WendaoBusinessError, match="周报地址"):
        parse_weekly_report_ticket(url)


def test_weekly_report_client_initializes_page_status_before_fetching_report() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/userData"):
            return httpx.Response(
                200,
                headers={"set-cookie": "PHPSESSID=SESSION_TEST; path=/"},
                json={"code": 1, "msg": "请求成功"},
            )
        if request.url.path.endswith("/page_status"):
            assert request.headers["cookie"] == "PHPSESSID=SESSION_TEST"
            return httpx.Response(200, json={"code": 1, "data": {"data": 3}})
        return httpx.Response(
            200,
            json={
                "code": 1,
                "msg": "请求成功",
                "data": {
                    "user": {"week": "2026年07月06日-2026年07月12日"},
                    "overview": {"login_days": "7"},
                },
            },
        )

    client = WeeklyReportClient(transport=httpx.MockTransport(handler))
    payload = run(client.fetch(parse_weekly_report_ticket(ACTION_URL)))
    run(client.aclose())

    assert payload["user"]["week"] == "2026年07月06日-2026年07月12日"
    assert len(captured) == 3
    user_data, page_status, index = captured
    assert user_data.method == "GET"
    assert user_data.url.path.endswith("/api/wd/p202404xd/userData")
    assert dict(user_data.url.params) == parse_weekly_report_ticket(
        ACTION_URL
    ).user_data_params
    assert page_status.method == "POST"
    assert page_status.url.path.endswith("/api/wd/p202404xd/page_status")
    assert index.method == "POST"
    assert index.url == httpx.URL(
        "https://actscpapi01.leiting.com/php/ltproject/public/"
        "index.php/api/wd/p202404xd/index"
    )
    assert index.headers["content-type"].startswith(
        "application/x-www-form-urlencoded"
    )
    expected_form = {
        "sid": ["SID_TEST_001"],
        "rid": ["RID_TEST_001"],
        "token": ["ACTIVITY_TOKEN_TEST_001"],
    }
    assert parse_qs(page_status.content.decode("ascii")) == expected_form
    assert parse_qs(index.content.decode("ascii")) == expected_form
    assert index.headers["cookie"] == "PHPSESSID=SESSION_TEST"


@pytest.mark.parametrize(
    ("state", "error_type", "message"),
    [
        (1, WeeklyReportNotReadyError, "生成中"),
        (2, WendaoBusinessError, "暂无"),
        (9, WendaoBusinessError, "状态"),
    ],
)
def test_weekly_report_client_maps_page_status_without_calling_index(
    state: int,
    error_type: type[Exception],
    message: str,
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/userData"):
            return httpx.Response(200, json={"code": 1})
        if request.url.path.endswith("/page_status"):
            return httpx.Response(200, json={"code": 1, "data": {"data": state}})
        raise AssertionError("页面状态不可读取时不应请求 index")

    client = WeeklyReportClient(transport=httpx.MockTransport(handler))

    with pytest.raises(error_type, match=message):
        run(client.fetch(parse_weekly_report_ticket(ACTION_URL)))
    run(client.aclose())

    assert len(paths) == 2


def test_weekly_report_client_classifies_code_zero_as_token_error_and_redacts() -> None:
    token = "ACTIVITY_TOKEN_SECRET_TEST"
    ticket = parse_weekly_report_ticket(ACTION_URL.replace(
        "ACTIVITY_TOKEN_TEST_001", token
    ))
    client = WeeklyReportClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"code": 0, "msg": f"token校验失败：{token}"},
            )
        )
    )

    with pytest.raises(WeeklyReportTokenError) as exc_info:
        run(client.fetch(ticket))
    run(client.aclose())

    assert token not in str(exc_info.value)
    assert exc_info.value.retryable is False


@pytest.mark.parametrize("status_code", [500, 502, 503])
def test_weekly_report_client_classifies_server_errors_as_retryable(
    status_code: int,
) -> None:
    client = WeeklyReportClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status_code))
    )

    with pytest.raises(WendaoRetryableError):
        run(client.fetch(parse_weekly_report_ticket(ACTION_URL)))
    run(client.aclose())

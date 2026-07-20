from __future__ import annotations

import asyncio
import json
import traceback

import httpx
import pytest

from components.api_client import (
    WendaoApiClient,
    WendaoAuthError,
    WendaoBusinessError,
    WendaoRetryableError,
    calculate_sign,
)
from components.models import Credentials


CREDENTIALS = Credentials(
    token="TOKEN_TEST_1234567890",
    device="2211133C/Android/16",
    version="2.26.1",
    version_code="260604",
    guest_id="1030000000000000",
    client_type="wd_android",
)

REFRESH_CREDENTIALS = Credentials(
    token="ACCESS_TOKEN_TEST_1234567890",
    device="2211133C/Android/16",
    version="2.26.1",
    version_code="260604",
    guest_id="1030000000000000",
    client_type="wd_android",
    refresh_token="REFRESH_TOKEN_TEST_0987654321",
    access_token_valid_time=1784285000000,
)


def run(coro):
    return asyncio.run(coro)


def test_calculate_sign_matches_verified_capture_samples() -> None:
    assert calculate_sign("2211133C/Android/16", "1784164615024") == (
        "292ce529e24578a6047535b833ba21b9"
    )
    assert calculate_sign("2211133C/Android/16", "1784164618607") == (
        "877022365c6a90b741dc35fc4648a2ce"
    )
    assert calculate_sign("2211133C/Android/16", "1784285128508") == (
        "c9d33d83cb8b65e814de1115b4856541"
    )


def test_get_third_url_uses_weekly_report_ticket_contract() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "code": 2000,
                "data": {
                    "actionUrl": (
                        "https://actscp01.leiting.com/wd/act/202405/hand/"
                        "?sid=SID_TEST&rid=RID_TEST&token=ACTIVITY_TOKEN_TEST"
                    )
                },
            },
        )

    client = WendaoApiClient(
        CREDENTIALS,
        transport=httpx.MockTransport(handler),
        clock_ms=lambda: 1784356922894,
    )
    response = run(client.get_third_url())
    run(client.aclose())

    assert response.data["actionUrl"].endswith("token=ACTIVITY_TOKEN_TEST")
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert request.url == httpx.URL(
        "https://vwdservice.roguelike.com/v2/api/wd_content/home/get_third_url"
    )
    assert request.headers["token"] == CREDENTIALS.token
    assert request.headers["timestamp"] == "1784356922894"
    assert request.headers["sign"] == calculate_sign(
        CREDENTIALS.device, "1784356922894"
    )
    assert json.loads(request.content) == {
        "actionUrl": "https://actscp01.leiting.com/wd/act/202405/hand/",
        "identify": "xdzb",
    }


def test_refresh_access_token_uses_refresh_contract_and_updates_credentials() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "code": 2000,
                "data": {
                    "accessToken": "ACCESS_TOKEN_NEW_1234567890",
                    "accessTokenValidTime": 1784288728233,
                    "refreshToken": "REFRESH_TOKEN_NEW_0987654321",
                },
            },
        )

    client = WendaoApiClient(
        REFRESH_CREDENTIALS,
        transport=httpx.MockTransport(handler),
        clock_ms=lambda: 1784285128508,
    )
    refreshed = run(client.refresh_access_token())
    run(client.aclose())

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "PUT"
    assert request.url == httpx.URL(
        "https://wdappapi.roguelike.com/api/app/account/token/refresh"
    )
    assert request.url.host == "wdappapi.roguelike.com"
    assert request.headers["token"] == REFRESH_CREDENTIALS.token
    assert request.headers["timestamp"] == "1784285128508"
    assert request.headers["device"] == REFRESH_CREDENTIALS.device
    assert request.headers["version"] == REFRESH_CREDENTIALS.version
    assert request.headers["versioncode"] == REFRESH_CREDENTIALS.version_code
    assert request.headers["guestid"] == REFRESH_CREDENTIALS.guest_id
    assert request.headers["clienttype"] == REFRESH_CREDENTIALS.client_type
    assert request.headers["sign"] == "c9d33d83cb8b65e814de1115b4856541"
    assert request.headers["content-type"] == "application/json; charset=UTF-8"
    assert json.loads(request.content) == {
        "refreshToken": REFRESH_CREDENTIALS.refresh_token,
    }
    assert refreshed == client.credentials
    assert refreshed.token == "ACCESS_TOKEN_NEW_1234567890"
    assert refreshed.access_token_valid_time == 1784288728233
    assert refreshed.refresh_token == "REFRESH_TOKEN_NEW_0987654321"
    assert refreshed.device == REFRESH_CREDENTIALS.device


def test_refresh_access_token_requires_refresh_token() -> None:
    client = WendaoApiClient(CREDENTIALS, transport=httpx.MockTransport(lambda _: None))

    with pytest.raises(WendaoAuthError, match="重新绑定") as exc_info:
        run(client.refresh_access_token())
    run(client.aclose())

    assert exc_info.value.retryable is False


@pytest.mark.parametrize(
    "missing_field",
    ["accessToken", "accessTokenValidTime", "refreshToken"],
)
def test_refresh_access_token_rejects_incomplete_success_data(missing_field: str) -> None:
    data = {
        "accessToken": "ACCESS_TOKEN_NEW_1234567890",
        "accessTokenValidTime": 1784288728233,
        "refreshToken": "REFRESH_TOKEN_NEW_0987654321",
    }
    data.pop(missing_field)

    client = WendaoApiClient(
        REFRESH_CREDENTIALS,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"code": 2000, "data": data})
        ),
    )
    with pytest.raises(WendaoBusinessError, match="响应格式错误") as exc_info:
        run(client.refresh_access_token())
    run(client.aclose())

    assert exc_info.value.retryable is False
    assert client.credentials == REFRESH_CREDENTIALS


@pytest.mark.parametrize("field", ["accessToken", "refreshToken"])
def test_refresh_access_token_rejects_whitespace_tokens(field: str) -> None:
    data = {
        "accessToken": "ACCESS_TOKEN_NEW_1234567890",
        "accessTokenValidTime": 1784288728233,
        "refreshToken": "REFRESH_TOKEN_NEW_0987654321",
    }
    data[field] = "   "
    client = WendaoApiClient(
        REFRESH_CREDENTIALS,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"code": 2000, "data": data})
        ),
    )

    with pytest.raises(WendaoBusinessError, match="响应格式错误"):
        run(client.refresh_access_token())
    run(client.aclose())

    assert client.credentials == REFRESH_CREDENTIALS


def test_refresh_access_token_classifies_http_and_network_errors() -> None:
    auth_client = WendaoApiClient(
        REFRESH_CREDENTIALS,
        transport=httpx.MockTransport(lambda _: httpx.Response(403)),
    )
    with pytest.raises(WendaoAuthError) as auth_exc:
        run(auth_client.refresh_access_token())
    run(auth_client.aclose())
    assert auth_exc.value.retryable is False

    retry_client = WendaoApiClient(
        REFRESH_CREDENTIALS,
        transport=httpx.MockTransport(lambda _: httpx.Response(502)),
    )
    with pytest.raises(WendaoRetryableError) as retry_exc:
        run(retry_client.refresh_access_token())
    run(retry_client.aclose())
    assert retry_exc.value.retryable is True

    def connection_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    network_client = WendaoApiClient(
        REFRESH_CREDENTIALS,
        transport=httpx.MockTransport(connection_failure),
    )
    with pytest.raises(WendaoRetryableError) as network_exc:
        run(network_client.refresh_access_token())
    run(network_client.aclose())
    assert network_exc.value.retryable is True


def test_refresh_access_token_redacts_access_and_refresh_tokens_from_errors() -> None:
    message = (
        f"expired access={REFRESH_CREDENTIALS.token} "
        f"refresh={REFRESH_CREDENTIALS.refresh_token}"
    )
    client = WendaoApiClient(
        REFRESH_CREDENTIALS,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"code": 4001, "message": message},
            )
        ),
    )

    with pytest.raises(WendaoBusinessError) as exc_info:
        run(client.refresh_access_token())
    run(client.aclose())

    rendered = str(exc_info.value)
    assert REFRESH_CREDENTIALS.token not in rendered
    assert REFRESH_CREDENTIALS.refresh_token not in rendered


def test_refresh_network_error_traceback_redacts_both_tokens() -> None:
    def connection_failure(request: httpx.Request) -> httpx.Response:
        message = (
            f"access={REFRESH_CREDENTIALS.token} "
            f"refresh={REFRESH_CREDENTIALS.refresh_token}"
        )
        raise httpx.ConnectError(message, request=request)

    client = WendaoApiClient(
        REFRESH_CREDENTIALS,
        transport=httpx.MockTransport(connection_failure),
    )

    with pytest.raises(WendaoRetryableError) as exc_info:
        run(client.refresh_access_token())
    run(client.aclose())

    rendered = "".join(
        traceback.format_exception(
            type(exc_info.value),
            exc_info.value,
            exc_info.value.__traceback__,
        )
    )
    assert REFRESH_CREDENTIALS.token not in rendered
    assert REFRESH_CREDENTIALS.refresh_token not in rendered


def test_list_signin_builds_headers_without_signing_path_or_token() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"code": 2000, "data": {"signInStatus": 1}, "timestamp": 1784164619455},
        )

    client = WendaoApiClient(
        CREDENTIALS,
        transport=httpx.MockTransport(handler),
        clock_ms=lambda: 1784164615024,
    )
    response = run(client.list_signin())
    run(client.aclose())

    assert response.data["signInStatus"] == 1
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "GET"
    assert request.url == httpx.URL(
        "https://vwdservice.roguelike.com/v2/api/wd_app/outer/user_signin/list"
    )
    assert request.headers["token"] == CREDENTIALS.token
    assert request.headers["timestamp"] == "1784164615024"
    assert request.headers["device"] == CREDENTIALS.device
    assert request.headers["version"] == CREDENTIALS.version
    assert request.headers["versioncode"] == CREDENTIALS.version_code
    assert request.headers["guestid"] == CREDENTIALS.guest_id
    assert request.headers["clienttype"] == CREDENTIALS.client_type
    assert request.headers["sign"] == "292ce529e24578a6047535b833ba21b9"


@pytest.mark.parametrize(
    ("operation", "expected_path", "expected_body"),
    [
        ("signin", "/v2/api/wd_app/outer/user_signin/signin", {"type": 1}),
        ("resign", "/v2/api/wd_app/outer/user_signin/signin", {"type": 2}),
        ("milestone", "/v2/api/wd_app/outer/user_signin/get_milestone_reward", None),
    ],
)
def test_post_operations_use_expected_path_and_body(
    operation: str,
    expected_path: str,
    expected_body: dict[str, int] | None,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"code": 2000, "data": {}})

    client = WendaoApiClient(CREDENTIALS, transport=httpx.MockTransport(handler))
    if operation == "signin":
        run(client.signin(1))
    elif operation == "resign":
        run(client.signin(2))
    else:
        run(client.claim_milestone())
    run(client.aclose())

    request = captured[0]
    assert request.method == "POST"
    assert request.url.path == expected_path
    actual_body = json.loads(request.content) if request.content else None
    assert actual_body == expected_body


def test_report_post_read_uses_source_id_and_post_type_query() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"code": 2000, "timestamp": 1784289805048})

    client = WendaoApiClient(
        CREDENTIALS,
        transport=httpx.MockTransport(handler),
        clock_ms=lambda: 1784289803940,
    )
    operation = getattr(client, "report_post_read", None)
    assert operation is not None

    response = run(operation("30555"))
    run(client.aclose())

    assert response.code == 2000
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "GET"
    assert request.url == httpx.URL(
        "https://vwdservice.roguelike.com/"
        "v2/api/wd_content/post/read_report/30555?postType=1"
    )
    assert request.headers["token"] == CREDENTIALS.token
    assert request.headers["timestamp"] == "1784289803940"
    assert request.headers["sign"] == calculate_sign(
        CREDENTIALS.device,
        "1784289803940",
    )
    assert request.content == b""


def test_report_post_read_rejects_invalid_source_id_without_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"code": 2000})

    client = WendaoApiClient(CREDENTIALS, transport=httpx.MockTransport(handler))
    operation = getattr(client, "report_post_read", None)
    assert operation is not None

    with pytest.raises(ValueError, match="文章 ID"):
        run(operation("../signin"))
    run(client.aclose())

    assert calls == 0


def test_http_auth_failure_is_classified_and_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"expired {CREDENTIALS.token}")

    client = WendaoApiClient(CREDENTIALS, transport=httpx.MockTransport(handler))
    with pytest.raises(WendaoAuthError) as exc_info:
        run(client.list_signin())
    run(client.aclose())

    assert exc_info.value.retryable is False
    assert CREDENTIALS.token not in str(exc_info.value)


def test_http_5xx_and_connection_errors_are_retryable() -> None:
    def http_failure(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="maintenance")

    http_client = WendaoApiClient(CREDENTIALS, transport=httpx.MockTransport(http_failure))
    with pytest.raises(WendaoRetryableError, match="503"):
        run(http_client.list_signin())
    run(http_client.aclose())

    def connection_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    connection_client = WendaoApiClient(
        CREDENTIALS,
        transport=httpx.MockTransport(connection_failure),
    )
    with pytest.raises(WendaoRetryableError, match="网络"):
        run(connection_client.list_signin())
    run(connection_client.aclose())


def test_business_4102_preserves_resign_task_without_echoing_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 4102,
                "msg": f"complete task {CREDENTIALS.token}",
                "data": {"reSignData": {"type": 2, "title": "阅读指定文章"}},
            },
        )

    client = WendaoApiClient(CREDENTIALS, transport=httpx.MockTransport(handler))
    with pytest.raises(WendaoBusinessError) as exc_info:
        run(client.signin(2))
    run(client.aclose())

    assert exc_info.value.code == 4102
    assert exc_info.value.data["reSignData"]["type"] == 2
    assert CREDENTIALS.token not in str(exc_info.value)


def test_redirect_is_not_followed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "https://example.test/redirect"})

    client = WendaoApiClient(CREDENTIALS, transport=httpx.MockTransport(handler))
    with pytest.raises(WendaoBusinessError, match="302"):
        run(client.list_signin())
    run(client.aclose())

    assert calls == 1

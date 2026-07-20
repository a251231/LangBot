from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from components.api_client import WendaoBusinessError, WendaoRetryableError, calculate_sign
from components.command_parser import BindingInput
from components.login import (
    LoginSessionData,
    LoginSessionError,
    WendaoLoginClient,
    WendaoLoginService,
    aes_cbc_encode,
    generate_guest_id,
)
from components.models import Credentials


SESSION = LoginSessionData(
    phone_number="13800138000",
    guest_id="1118603298635886",
    serial_uuid="00000000-0000-4000-8000-000000000000",
    android_id="0123456789abcdef",
    oaid="fedcba9876543210",
    created_at_ms=1784362760000,
    expires_at_ms=1784363360000,
)


def run(coro):
    return asyncio.run(coro)


def test_aes_cbc_encode_matches_native_contract_vectors() -> None:
    assert aes_cbc_encode("13800138000") == "mehVzb+pJkoNCMEV++fUoQ=="
    assert aes_cbc_encode("00000000-0000-4000-8000-000000000000") == (
        "tFajUafufJJzW5NOeyYle0f+cZ8NyMrgmOWgl84Wjz2MDkR57gAzZvg2bx07FwgU"
    )


def test_generate_guest_id_matches_app_timestamp_formula() -> None:
    assert generate_guest_id(1784362729863, random_suffix=5886) == "1118603298635886"


def test_send_sms_uses_dynamic_guest_identity_and_captcha_ticket() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"code": 2000, "data": {"status": 0}})

    client = WendaoLoginClient(
        transport=httpx.MockTransport(handler),
        clock_ms=lambda: 1784362765587,
    )
    run(client.send_sms(SESSION, randstr="@Dqa", ticket="TICKET_TEST_*"))
    run(client.aclose())

    [request] = captured
    assert request.method == "POST"
    assert request.url == httpx.URL(
        "https://vwdservice.roguelike.com/v2/api/app/sms/leiting/send"
    )
    assert request.headers["guestid"] == SESSION.guest_id
    assert request.headers["timestamp"] == "1784362765587"
    assert request.headers["sign"] == calculate_sign(
        "2211133C/Android/16", "1784362765587"
    )
    body = json.loads(request.content)
    assert body == {
        "phoneNo": "mehVzb+pJkoNCMEV++fUoQ==",
        "scene": "LOGIN_LEC",
        "verificationCode": {
            "imei": SESSION.guest_id,
            "macAddress": "",
            "randStr": "@Dqa",
            "scene": 1,
            "ticket": "TICKET_TEST_*",
        },
    }


def test_login_client_fetches_tencent_captcha_app_id_from_switch_config() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "code": 2000,
                "data": {
                    "verificationCodeCaptchaConfig": {
                        "appId": "APP_ID_TEST_123",
                        "isClose": 0,
                    }
                },
            },
        )

    client = WendaoLoginClient(
        transport=httpx.MockTransport(handler),
        clock_ms=lambda: 1784362765587,
    )
    app_id = run(client.get_captcha_app_id(SESSION))
    run(client.aclose())

    [request] = captured
    assert request.method == "GET"
    assert request.url == httpx.URL(
        "https://vwdservice.roguelike.com/v2/api/app/account/login/switch"
    )
    assert request.headers["guestid"] == SESSION.guest_id
    assert request.headers["sign"] == calculate_sign(
        "2211133C/Android/16", "1784362765587"
    )
    assert app_id == "APP_ID_TEST_123"


def test_phone_login_builds_fly_data_and_returns_dynamic_credentials() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "code": 2000,
                "data": {
                    "ltUid": "311900000000000000",
                    "token": {
                        "accessToken": "ACCESS_TOKEN_LOGIN_TEST",
                        "accessTokenValidTime": 1784366360000,
                        "refreshToken": "REFRESH_TOKEN_LOGIN_TEST",
                    },
                    "bindServer": {"name": "登录测试角色"},
                },
            },
        )

    client = WendaoLoginClient(
        transport=httpx.MockTransport(handler),
        clock_ms=lambda: 1784362768607,
    )
    binding = run(client.login_with_code(SESSION, verification_code="873157"))
    run(client.aclose())

    [request] = captured
    assert request.method == "POST"
    assert request.url == httpx.URL(
        "https://vwdservice.roguelike.com/v2/api/app/account/login/phone_code"
    )
    body = json.loads(request.content)
    assert body["phoneNo"] == "mehVzb+pJkoNCMEV++fUoQ=="
    assert body["serial"] == (
        "tFajUafufJJzW5NOeyYle0f+cZ8NyMrgmOWgl84Wjz2MDkR57gAzZvg2bx07FwgU"
    )
    assert body["verificationCode"] == "873157"
    assert body["flyData"] == {
        "clientVer": "260604",
        "extend": (
            "NgGicLpHewXxie4Op7N9oIZAWdlHk0TyUEU8F6000rCSizn3LhtTN52wVZR6xOwr"
            "NNn5EFwi91rcXPj6T3icG1Pcn7PCx+bqvk2JVOWqjys="
        ),
        "media": "0",
        "osVer": "16",
        "terminInfo": "2211133C",
    }
    assert binding.nickname == "登录测试角色"
    assert binding.user_identifier == "311900000000000000"
    assert binding.credentials == Credentials(
        token="ACCESS_TOKEN_LOGIN_TEST",
        device="2211133C/Android/16",
        version="2.26.1",
        version_code="260604",
        guest_id=SESSION.guest_id,
        client_type="wd_android",
        refresh_token="REFRESH_TOKEN_LOGIN_TEST",
        access_token_valid_time=1784366360000,
    )


def test_login_client_classifies_errors_without_echoing_sensitive_input() -> None:
    ticket = "TICKET_SECRET_TEST_*"
    client = WendaoLoginClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"code": 4101, "msg": f"invalid {ticket} 13800138000"},
            )
        )
    )

    with pytest.raises(WendaoBusinessError) as exc_info:
        run(client.send_sms(SESSION, randstr="@Dqa", ticket=ticket))
    run(client.aclose())

    rendered = str(exc_info.value)
    assert ticket not in rendered
    assert SESSION.phone_number not in rendered
    assert exc_info.value.code == 4101


class FakeLoginClient:
    def __init__(self) -> None:
        self.sms_calls: list[tuple[LoginSessionData, str, str]] = []
        self.login_calls: list[tuple[LoginSessionData, str]] = []
        self.closed = 0
        self.sms_error: Exception | None = None
        self.login_error: Exception | None = None
        self.binding = BindingInput(
            credentials=Credentials(
                token="ACCESS_TOKEN_SERVICE_TEST",
                device="2211133C/Android/16",
                version="2.26.1",
                version_code="260604",
                guest_id="1118603298635886",
                client_type="wd_android",
                refresh_token="REFRESH_TOKEN_SERVICE_TEST",
                access_token_valid_time=1784366360000,
            ),
            nickname="状态机角色",
            user_identifier="service-user-1",
        )

    async def send_sms(
        self,
        session: LoginSessionData,
        *,
        randstr: str,
        ticket: str,
    ) -> None:
        self.sms_calls.append((session, randstr, ticket))
        if self.sms_error:
            raise self.sms_error

    async def login_with_code(
        self,
        session: LoginSessionData,
        *,
        verification_code: str,
    ) -> BindingInput:
        self.login_calls.append((session, verification_code))
        if self.login_error:
            raise self.login_error
        return self.binding

    async def aclose(self) -> None:
        self.closed += 1


def make_service(client: FakeLoginClient, now: list[int]) -> WendaoLoginService:
    return WendaoLoginService(
        client_factory=lambda: client,
        ttl_seconds=600,
        clock_ms=lambda: now[0],
        random_suffix=lambda: 5886,
        uuid_factory=lambda: "00000000-0000-4000-8000-000000000000",
        token_hex=lambda _: "0123456789abcdef",
    )


def test_login_service_runs_short_lived_three_step_state_machine() -> None:
    async def scenario() -> None:
        now = [1784362729863]
        client = FakeLoginClient()
        service = make_service(client, now)

        session = await service.begin("bot-1", "sender-1", "13800138000")
        await service.submit_captcha(
            "bot-1",
            "sender-1",
            randstr="@Dqa",
            ticket="TICKET_TEST_*",
        )
        binding = await service.submit_code(
            "bot-1", "sender-1", verification_code="873157"
        )
        repeated = await service.submit_code(
            "bot-1", "sender-1", verification_code="873157"
        )

        assert session.guest_id == "1118603298635886"
        assert len(client.sms_calls) == 1
        assert len(client.login_calls) == 1
        assert binding == repeated == client.binding
        assert client.closed == 2
        await service.complete("bot-1", "sender-1")
        with pytest.raises(LoginSessionError, match="登录"):
            await service.submit_code(
                "bot-1", "sender-1", verification_code="873157"
            )

    run(scenario())


def test_login_service_validates_order_input_and_expiry() -> None:
    async def scenario() -> None:
        now = [1784362765587]
        client = FakeLoginClient()
        service = make_service(client, now)

        with pytest.raises(LoginSessionError, match="手机号"):
            await service.begin("bot-1", "sender-1", "123")
        await service.begin("bot-1", "sender-1", "13800138000")
        with pytest.raises(LoginSessionError, match="人机验证"):
            await service.submit_code(
                "bot-1", "sender-1", verification_code="873157"
            )
        with pytest.raises(LoginSessionError, match="票据"):
            await service.submit_captcha(
                "bot-1", "sender-1", randstr="", ticket=""
            )

        now[0] += 600_000
        with pytest.raises(LoginSessionError, match="过期"):
            await service.submit_captcha(
                "bot-1", "sender-1", randstr="@Dqa", ticket="TICKET_TEST_*"
            )
        assert client.sms_calls == []

    run(scenario())


def test_login_service_allows_retry_after_network_error_without_storing_ticket() -> None:
    async def scenario() -> None:
        now = [1784362765587]
        client = FakeLoginClient()
        client.sms_error = WendaoRetryableError("network", retryable=True)
        service = make_service(client, now)
        session = await service.begin("bot-1", "sender-1", "13800138000")

        with pytest.raises(WendaoRetryableError):
            await service.submit_captcha(
                "bot-1", "sender-1", randstr="@Dqa", ticket="TICKET_SECRET_*"
            )
        client.sms_error = None
        await service.submit_captcha(
            "bot-1", "sender-1", randstr="@NEW", ticket="TICKET_NEW_*"
        )

        assert "ticket" not in session.__dataclass_fields__
        assert "verification_code" not in session.__dataclass_fields__
        assert len(client.sms_calls) == 2

    run(scenario())


def test_login_service_only_waits_for_plain_code_after_sms_is_sent() -> None:
    async def scenario() -> None:
        now = [1784362765587]
        client = FakeLoginClient()
        service = make_service(client, now)

        assert await service.is_waiting_for_code("bot-1", "sender-1") is False
        await service.begin("bot-1", "sender-1", "13800138000")
        assert await service.is_waiting_for_code("bot-1", "sender-1") is False

        await service.submit_captcha(
            "bot-1",
            "sender-1",
            randstr="@Dqa",
            ticket="TICKET_TEST_*",
        )
        assert await service.is_waiting_for_code("bot-1", "sender-1") is True

        await service.complete("bot-1", "sender-1")
        assert await service.is_waiting_for_code("bot-1", "sender-1") is False

    run(scenario())


def test_login_service_purges_abandoned_expired_phone_session() -> None:
    async def scenario() -> None:
        now = [1784362729863]
        client = FakeLoginClient()
        service = make_service(client, now)
        await service.begin("bot-1", "sender-1", "13800138000")

        now[0] += 600_001
        await service.purge_expired()

        assert service._sessions == {}
        with pytest.raises(LoginSessionError, match="登录"):
            await service.submit_captcha(
                "bot-1", "sender-1", randstr="@Dqa", ticket="TICKET_TEST_*"
            )
        service.close()

    run(scenario())

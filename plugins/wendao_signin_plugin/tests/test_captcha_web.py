from __future__ import annotations

import asyncio
import importlib.util
from urllib.parse import urlsplit

from aiohttp.test_utils import TestClient, TestServer
import pytest

from components.api_client import WendaoRetryableError
from components.login import LoginSessionError


CAPTCHA_MODULE_AVAILABLE = (
    importlib.util.find_spec("components.captcha_web") is not None
)

if CAPTCHA_MODULE_AVAILABLE:
    from components.captcha_web import CaptchaWebServer
else:
    CaptchaWebServer = None


requires_captcha_module = pytest.mark.skipif(
    not CAPTCHA_MODULE_AVAILABLE,
    reason="components.captcha_web 尚未实现",
)


class FakeLoginService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []
        self.error: Exception | None = None

    async def submit_captcha(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        randstr: str,
        ticket: str,
    ) -> None:
        self.calls.append((bot_uuid, sender_id, randstr, ticket))
        if self.error is not None:
            raise self.error


def run(coro):
    return asyncio.run(coro)


def make_server(
    service: FakeLoginService,
    now: list[int],
    notifications: list[tuple[str, str, str, str, str]],
):
    async def notify(
        bot_uuid: str,
        sender_id: str,
        target_type: str,
        target_id: str,
        text: str,
    ) -> None:
        notifications.append((bot_uuid, sender_id, target_type, target_id, text))

    assert CaptchaWebServer is not None
    return CaptchaWebServer(
        login_service=service,
        public_base_url="http://server.example:8788",
        bind_host="127.0.0.1",
        bind_port=8788,
        clock_ms=lambda: now[0],
        token_urlsafe=lambda _: "NONCE_TEST_1234567890",
        notify=notify,
    )


def test_captcha_module_is_available() -> None:
    assert CAPTCHA_MODULE_AVAILABLE


@requires_captcha_module
def test_captcha_page_uses_one_time_server_url_without_exposing_phone() -> None:
    async def scenario() -> None:
        service = FakeLoginService()
        now = [1784362760000]
        notifications: list[tuple[str, str, str, str, str]] = []
        server = make_server(service, now, notifications)
        url = server.create_challenge(
            bot_uuid="bot-test",
            sender_id="sender-test",
            target_type="group",
            target_id="target-test",
            captcha_app_id="APP_ID_TEST_123",
            expires_at_ms=now[0] + 600_000,
        )

        assert url == (
            "http://server.example:8788/wendao/captcha/"
            "NONCE_TEST_1234567890"
        )
        assert "13800138000" not in url

        async with TestClient(TestServer(server.app)) as client:
            path = urlsplit(url).path
            response = await client.get(path)
            html = await response.text()

            assert response.status == 200
            assert "https://turing.captcha.qcloud.com/TCaptcha.js" in html
            assert "new TencentCaptcha" in html
            assert "APP_ID_TEST_123" in html
            assert "13800138000" not in html
            assert response.headers["Cache-Control"] == "no-store"

            verified = await client.post(
                path,
                json={"randstr": "@TEST", "ticket": "TICKET_TEST_*"},
            )
            payload = await verified.json()

            assert verified.status == 200
            assert payload == {
                "ok": True,
                "message": "验证码已发送，请返回机器人并直接回复短信中的验证码。",
            }
            assert service.calls == [
                ("bot-test", "sender-test", "@TEST", "TICKET_TEST_*")
            ]
            assert notifications == [
                (
                    "bot-test",
                    "sender-test",
                    "group",
                    "target-test",
                    "验证码已发送，请直接回复短信中的验证码，例如：123456。",
                )
            ]

            replay = await client.post(
                path,
                json={"randstr": "@TEST", "ticket": "TICKET_TEST_*"},
            )
            assert replay.status == 410
            assert len(service.calls) == 1

    run(scenario())


@requires_captcha_module
def test_captcha_page_rejects_expired_or_unknown_nonce() -> None:
    async def scenario() -> None:
        service = FakeLoginService()
        now = [1784362760000]
        server = make_server(service, now, [])
        url = server.create_challenge(
            bot_uuid="bot-test",
            sender_id="sender-test",
            target_type="person",
            target_id="target-test",
            captcha_app_id="APP_ID_TEST_123",
            expires_at_ms=now[0] + 1,
        )
        now[0] += 1

        async with TestClient(TestServer(server.app)) as client:
            expired = await client.get(urlsplit(url).path)
            unknown = await client.get("/wendao/captcha/UNKNOWN_NONCE_TEST")

            assert expired.status == 410
            assert unknown.status == 404
            assert service.calls == []

    run(scenario())


@requires_captcha_module
def test_captcha_callback_keeps_challenge_for_retry_and_masks_ticket() -> None:
    async def scenario() -> None:
        service = FakeLoginService()
        service.error = WendaoRetryableError(
            "upstream TICKET_SECRET_TEST_*",
            retryable=True,
        )
        now = [1784362760000]
        server = make_server(service, now, [])
        url = server.create_challenge(
            bot_uuid="bot-test",
            sender_id="sender-test",
            target_type="person",
            target_id="target-test",
            captcha_app_id="APP_ID_TEST_123",
            expires_at_ms=now[0] + 600_000,
        )

        async with TestClient(TestServer(server.app)) as client:
            path = urlsplit(url).path
            failed = await client.post(
                path,
                json={"randstr": "@TEST", "ticket": "TICKET_SECRET_TEST_*"},
            )
            failed_text = await failed.text()

            assert failed.status == 502
            assert "TICKET_SECRET_TEST" not in failed_text

            service.error = None
            retried = await client.post(
                path,
                json={"randstr": "@NEW", "ticket": "TICKET_NEW_TEST_*"},
            )
            assert retried.status == 200
            assert len(service.calls) == 2

    run(scenario())


@requires_captcha_module
def test_captcha_callback_validates_payload_without_echoing_input() -> None:
    async def scenario() -> None:
        service = FakeLoginService()
        now = [1784362760000]
        server = make_server(service, now, [])
        url = server.create_challenge(
            bot_uuid="bot-test",
            sender_id="sender-test",
            target_type="person",
            target_id="target-test",
            captcha_app_id="APP_ID_TEST_123",
            expires_at_ms=now[0] + 600_000,
        )

        async with TestClient(TestServer(server.app)) as client:
            response = await client.post(
                urlsplit(url).path,
                json={"randstr": "", "ticket": "TICKET_INVALID_SECRET"},
            )
            text = await response.text()

            assert response.status == 400
            assert "TICKET_INVALID_SECRET" not in text
            assert service.calls == []

    run(scenario())


@requires_captcha_module
def test_captcha_callback_maps_expired_login_session_to_gone() -> None:
    async def scenario() -> None:
        service = FakeLoginService()
        service.error = LoginSessionError("问道登录会话已过期")
        now = [1784362760000]
        server = make_server(service, now, [])
        url = server.create_challenge(
            bot_uuid="bot-test",
            sender_id="sender-test",
            target_type="person",
            target_id="target-test",
            captcha_app_id="APP_ID_TEST_123",
            expires_at_ms=now[0] + 600_000,
        )

        async with TestClient(TestServer(server.app)) as client:
            response = await client.post(
                urlsplit(url).path,
                json={"randstr": "@TEST", "ticket": "TICKET_TEST_*"},
            )
            assert response.status == 410

    run(scenario())

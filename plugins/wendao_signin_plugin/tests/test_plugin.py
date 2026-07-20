from __future__ import annotations

import asyncio
from collections import deque
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import weakref

from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain

from components.api_client import WendaoAuthError
from components.command_parser import BindingInput, ParsedCommand
from components.models import ApiResponse, Credentials
from components.weekly_report import WeeklyReportOutcome


PLUGIN_MAIN = Path(__file__).parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("wendao_signin_plugin_main", PLUGIN_MAIN)
assert SPEC is not None and SPEC.loader is not None
MAIN_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MAIN_MODULE)
WendaoSigninPlugin = MAIN_MODULE.WendaoSigninPlugin


BASE_CURL = r"""
curl -X GET 'https://vwdservice.roguelike.com/v2/api/wd_app/outer/user_signin/list' \
-H 'token: TOKEN_TEST_PLUGIN_FIRST' \
-H 'device: DEVICE_TEST/Android/16' \
-H 'version: 2.26.1' \
-H 'versionCode: 260604' \
-H 'guestId: 1030000000000000' \
-H 'clientType: wd_android'
"""

LOGIN_RESPONSE = """{
  "code": 2000,
  "data": {
    "ltUid": "311900000000000000",
    "token": {
      "accessToken": "TOKEN_LOGIN_PLUGIN_TEST",
      "accessTokenValidTime": 1784219114121,
      "refreshToken": "REFRESH_LOGIN_PLUGIN_TEST"
    },
    "bindServer": {"name": "登录响应角色"}
  }
}"""


class FakeApiClient:
    def __init__(self, result: dict | Exception) -> None:
        self.result = result
        self.closed = False

    async def list_signin(self) -> ApiResponse:
        if isinstance(self.result, Exception):
            raise self.result
        return ApiResponse(code=2000, data=self.result)

    async def signin(self, signin_type: int) -> ApiResponse:
        return ApiResponse(code=2000, data={})

    async def claim_milestone(self) -> ApiResponse:
        return ApiResponse(code=2000, data={})

    async def aclose(self) -> None:
        self.closed = True


class BlockingQueryClient(FakeApiClient):
    def __init__(self) -> None:
        super().__init__(
            {
                "signInStatus": 2,
                "signNum": 7,
                "reSignInStatus": 4,
                "reSignNum": 0,
                "reSignNumLimit": 1,
                "milestoneData": {"state": 1},
            }
        )
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def list_signin(self) -> ApiResponse:
        self.entered.set()
        await self.release.wait()
        return await super().list_signin()


class BindingConcurrencyProbe:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.first_entered = asyncio.Event()
        self.release = asyncio.Event()


class ConcurrentBindingClient(FakeApiClient):
    def __init__(self, probe: BindingConcurrencyProbe) -> None:
        super().__init__({"signInStatus": 2})
        self.probe = probe

    async def list_signin(self) -> ApiResponse:
        self.probe.active += 1
        self.probe.max_active = max(self.probe.max_active, self.probe.active)
        self.probe.first_entered.set()
        await self.probe.release.wait()
        self.probe.active -= 1
        return await super().list_signin()


class FakeLoginService:
    def __init__(self, binding: BindingInput) -> None:
        self.binding = binding
        self.begin_calls: list[tuple[str, str, str]] = []
        self.captcha_calls: list[tuple[str, str, str, str]] = []
        self.code_calls: list[tuple[str, str, str]] = []
        self.complete_calls: list[tuple[str, str]] = []

    async def get_captcha_app_id(self, bot_uuid: str, sender_id: str) -> str:
        return "APP_ID_PLUGIN_TEST"

    async def begin(self, bot_uuid: str, sender_id: str, phone: str):
        self.begin_calls.append((bot_uuid, sender_id, phone))
        return SimpleNamespace(expires_at_ms=1784363360000)

    async def submit_captcha(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        randstr: str,
        ticket: str,
    ) -> None:
        self.captcha_calls.append((bot_uuid, sender_id, randstr, ticket))

    async def submit_code(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        verification_code: str,
    ) -> BindingInput:
        self.code_calls.append((bot_uuid, sender_id, verification_code))
        return self.binding

    async def complete(self, bot_uuid: str, sender_id: str) -> None:
        self.complete_calls.append((bot_uuid, sender_id))

    def close(self) -> None:
        pass


class MemoryWendaoPlugin(WendaoSigninPlugin):
    def __init__(self, clients: list[FakeApiClient] | None = None) -> None:
        super().__init__()
        self.config = {
            "timezone": "Asia/Shanghai",
            "default_schedule_time": "08:00",
            "request_timeout_seconds": 15,
            "scheduler_poll_seconds": 30,
            "max_concurrency": 3,
            "default_auto_signin": True,
            "default_auto_resign": True,
            "default_auto_milestone": True,
            "default_auto_weekly_report": True,
        }
        self.values: dict[str, bytes] = {}
        self.clients = deque(clients or [])
        self.created_credentials: list[Credentials] = []
        self.sent_messages: list[dict] = []

    async def set_plugin_storage(self, key: str, value: bytes) -> None:
        self.values[key] = value

    async def get_plugin_storage(self, key: str) -> bytes:
        return self.values[key]

    async def get_plugin_storage_keys(self) -> list[str]:
        return list(self.values)

    async def delete_plugin_storage(self, key: str) -> None:
        del self.values[key]

    async def send_message(self, **kwargs) -> None:
        self.sent_messages.append(kwargs)

    def _create_api_client(self, credentials: Credentials) -> FakeApiClient:
        self.created_credentials.append(credentials)
        return self.clients.popleft()


class FakeCaptchaServer:
    def __init__(self) -> None:
        self.challenges: list[dict] = []

    def create_challenge(self, **kwargs) -> str:
        self.challenges.append(kwargs)
        return "http://server.example:8788/wendao/captcha/NONCE_PLUGIN_TEST"

    def discard_identity(self, bot_uuid: str, sender_id: str) -> None:
        pass

    def stop(self) -> None:
        pass


async def command(plugin: WendaoSigninPlugin, kind: str, argument: str = "") -> str:
    return await plugin.handle_wendao_command(
        bot_uuid="bot-test-1",
        sender_id="sender-test-1",
        target_id="person-target-1",
        is_group=False,
        command=ParsedCommand(kind=kind, argument=argument),
    )


def run(coro):
    return asyncio.run(coro)


def test_initialize_owns_store_workflow_and_scheduler() -> None:
    async def scenario() -> None:
        plugin = MemoryWendaoPlugin()
        await plugin.initialize()

        assert plugin.account_store is not None
        assert plugin.account_session is not None
        assert plugin.login_service is not None
        assert plugin.workflow is not None
        assert plugin.weekly_report is not None
        assert plugin.workflow._session is plugin.account_session
        assert plugin.weekly_report._session is plugin.account_session
        assert plugin.scheduler is not None
        assert plugin.scheduler._task is not None
        plugin.scheduler.stop()

    run(scenario())


def test_phone_login_commands_validate_and_save_dynamic_credentials() -> None:
    async def scenario() -> None:
        validation_client = FakeApiClient({"signInStatus": 2})
        plugin = MemoryWendaoPlugin([validation_client])
        await plugin.initialize()
        binding = BindingInput(
            credentials=Credentials(
                token="ACCESS_TOKEN_COMMAND_TEST",
                device="2211133C/Android/16",
                version="2.26.1",
                version_code="260604",
                guest_id="1118603298635886",
                client_type="wd_android",
                refresh_token="REFRESH_TOKEN_COMMAND_TEST",
                access_token_valid_time=1784366360000,
            ),
            nickname="手机号登录角色",
            user_identifier="phone-user-test",
        )
        login_service = FakeLoginService(binding)
        plugin.login_service = login_service
        captcha_server = FakeCaptchaServer()
        plugin.captcha_server = captcha_server

        start_reply = await command(plugin, "login_start", "13800138000")
        captcha_reply = await command(
            plugin,
            "login_captcha",
            "@Dqa TICKET_COMMAND_SECRET_*",
        )
        login_reply = await command(plugin, "login_code", "873157")
        saved = await plugin.account_store.get("bot-test-1", "sender-test-1")

        assert (
            "http://server.example:8788/wendao/captcha/NONCE_PLUGIN_TEST"
            in start_reply
        )
        assert "13800138000" not in start_reply
        assert "验证码已发送" in captcha_reply
        assert "直接回复" in captcha_reply
        assert "TICKET_COMMAND_SECRET" not in captcha_reply
        assert "绑定成功" in login_reply
        assert "873157" not in login_reply
        assert saved.credentials == binding.credentials
        assert saved.nickname == "手机号登录角色"
        assert saved.user_identifier == "phone-user-test"
        assert login_service.begin_calls == [
            ("bot-test-1", "sender-test-1", "13800138000")
        ]
        assert captcha_server.challenges == [
            {
                "bot_uuid": "bot-test-1",
                "sender_id": "sender-test-1",
                "target_id": "person-target-1",
                "captcha_app_id": "APP_ID_PLUGIN_TEST",
                "expires_at_ms": 1784363360000,
            }
        ]
        assert login_service.captcha_calls == [
            (
                "bot-test-1",
                "sender-test-1",
                "@Dqa",
                "TICKET_COMMAND_SECRET_*",
            )
        ]
        assert login_service.code_calls == [
            ("bot-test-1", "sender-test-1", "873157")
        ]
        assert login_service.complete_calls == [("bot-test-1", "sender-test-1")]
        assert validation_client.closed is True
        plugin.scheduler.stop()

    run(scenario())


def test_phone_login_invalid_phone_returns_copyable_command_hint() -> None:
    async def scenario() -> None:
        plugin = MemoryWendaoPlugin()
        await plugin.initialize()

        missing = await command(plugin, "login_start", "")
        malformed = await command(plugin, "login_start", "123")

        expected = (
            "手机号格式错误，请发送：问道登录 11位手机号\n"
            "示例：问道登录 13800138000"
        )
        assert missing == expected
        assert malformed == expected
        plugin.scheduler.stop()

    run(scenario())


def test_login_captcha_requires_randstr_and_ticket_without_calling_service() -> None:
    async def scenario() -> None:
        plugin = MemoryWendaoPlugin()
        await plugin.initialize()
        binding = BindingInput(
            credentials=Credentials(
                token="TOKEN_UNUSED",
                device="2211133C/Android/16",
                version="2.26.1",
                version_code="260604",
                guest_id="1118603298635886",
                client_type="wd_android",
            )
        )
        service = FakeLoginService(binding)
        plugin.login_service = service

        reply = await command(plugin, "login_captcha", "only-one-value")

        assert "格式" in reply
        assert service.captcha_calls == []
        plugin.scheduler.stop()

    run(scenario())


def test_unbind_also_clears_pending_phone_login_session() -> None:
    async def scenario() -> None:
        plugin = MemoryWendaoPlugin()
        await plugin.initialize()
        binding = BindingInput(
            credentials=Credentials(
                token="TOKEN_UNUSED",
                device="2211133C/Android/16",
                version="2.26.1",
                version_code="260604",
                guest_id="1118603298635886",
                client_type="wd_android",
            )
        )
        service = FakeLoginService(binding)
        plugin.login_service = service

        reply = await command(plugin, "unbind")

        assert reply == "当前未绑定问道账号。"
        assert service.complete_calls == [("bot-test-1", "sender-test-1")]
        plugin.scheduler.stop()

    run(scenario())


def test_hot_reload_drops_plugin_and_cancels_scheduler_without_gc_cycle() -> None:
    async def scenario() -> None:
        plugin = MemoryWendaoPlugin()
        await plugin.initialize()
        plugin_ref = weakref.ref(plugin)
        scheduler_ref = weakref.ref(plugin.scheduler)
        task = plugin.scheduler._task

        plugin = None
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert plugin_ref() is None
        assert scheduler_ref() is None
        assert task.done() is True

    run(scenario())


def test_binding_validates_then_saves_profile_without_echoing_token() -> None:
    async def scenario() -> None:
        client = FakeApiClient(
            {
                "signInStatus": 2,
                "userInfo": {"nickname": "问道玩家", "ltUid": "remote-user-1"},
            }
        )
        plugin = MemoryWendaoPlugin([client])
        await plugin.initialize()

        reply = await command(plugin, "bind", BASE_CURL)
        saved = await plugin.account_store.get("bot-test-1", "sender-test-1")

        assert "绑定成功" in reply
        assert "问道玩家" in reply
        assert "TOKEN_TEST_PLUGIN_FIRST" not in reply
        assert "删除聊天记录" in reply
        assert saved.credentials.token == "TOKEN_TEST_PLUGIN_FIRST"
        assert saved.nickname == "问道玩家"
        assert saved.user_identifier == "remote-user-1"
        assert saved.auto_weekly_report is True
        assert client.closed is True
        plugin.scheduler.stop()

    run(scenario())


def test_binding_accepts_login_response_and_saves_refresh_credentials() -> None:
    async def scenario() -> None:
        client = FakeApiClient({"signInStatus": 2})
        plugin = MemoryWendaoPlugin([client])
        await plugin.initialize()

        reply = await command(plugin, "bind", LOGIN_RESPONSE)
        saved = await plugin.account_store.get("bot-test-1", "sender-test-1")

        assert "绑定成功" in reply
        assert "登录响应角色" in reply
        assert "TOKEN_LOGIN_PLUGIN_TEST" not in reply
        assert saved.credentials.token == "TOKEN_LOGIN_PLUGIN_TEST"
        assert saved.credentials.refresh_token == "REFRESH_LOGIN_PLUGIN_TEST"
        assert saved.credentials.access_token_valid_time == 1784219114121
        assert saved.credentials.device == "2211133C/Android/16"
        assert saved.credentials.guest_id == "1030620167932793"
        assert saved.nickname == "登录响应角色"
        assert saved.user_identifier == "311900000000000000"
        assert plugin.created_credentials[-1] == saved.credentials
        plugin.scheduler.stop()

    run(scenario())


def test_binding_validation_uses_global_concurrency_limit() -> None:
    async def scenario() -> None:
        probe = BindingConcurrencyProbe()
        plugin = MemoryWendaoPlugin(
            [ConcurrentBindingClient(probe), ConcurrentBindingClient(probe)]
        )
        plugin.config["max_concurrency"] = 1
        await plugin.initialize()

        first = asyncio.create_task(
            plugin.handle_wendao_command(
                bot_uuid="bot-test-1",
                sender_id="sender-test-1",
                target_id="target-test-1",
                is_group=False,
                command=ParsedCommand("bind", BASE_CURL),
            )
        )
        await probe.first_entered.wait()
        second = asyncio.create_task(
            plugin.handle_wendao_command(
                bot_uuid="bot-test-1",
                sender_id="sender-test-2",
                target_id="target-test-2",
                is_group=False,
                command=ParsedCommand("bind", BASE_CURL),
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert probe.max_active == 1
        probe.release.set()
        await asyncio.gather(first, second)
        assert probe.max_active == 1
        plugin.scheduler.stop()

    run(scenario())


def test_failed_rebind_keeps_old_credentials_and_user_settings() -> None:
    async def scenario() -> None:
        first = FakeApiClient({"signInStatus": 2})
        invalid = FakeApiClient(WendaoAuthError("expired", retryable=False))
        plugin = MemoryWendaoPlugin([first, invalid])
        await plugin.initialize()
        await command(plugin, "bind", BASE_CURL)
        await command(plugin, "schedule_time", "09:30")
        await command(plugin, "auto_resign", "关")

        replacement_curl = BASE_CURL.replace(
            "TOKEN_TEST_PLUGIN_FIRST", "TOKEN_TEST_PLUGIN_REPLACEMENT"
        )
        reply = await command(plugin, "bind", replacement_curl)
        saved = await plugin.account_store.get("bot-test-1", "sender-test-1")

        assert "验证失败" in reply
        assert "TOKEN_TEST_PLUGIN_REPLACEMENT" not in reply
        assert saved.credentials.token == "TOKEN_TEST_PLUGIN_FIRST"
        assert saved.schedule_time == "09:30"
        assert saved.auto_resign is False
        plugin.scheduler.stop()

    run(scenario())


def test_settings_commands_update_and_render_without_sensitive_fields() -> None:
    async def scenario() -> None:
        plugin = MemoryWendaoPlugin([FakeApiClient({"signInStatus": 2})])
        await plugin.initialize()
        await command(plugin, "bind", BASE_CURL)

        assert "时间格式" in await command(plugin, "schedule_time", "25:00")
        assert "09:45" in await command(plugin, "schedule_time", "09:45")
        assert "已关闭" in await command(plugin, "auto_signin", "关")
        assert "已开启" in await command(plugin, "auto_resign", "开")
        assert "已关闭" in await command(plugin, "auto_milestone", "关")
        assert "已关闭" in await command(plugin, "auto_weekly_report", "关")
        settings = await command(plugin, "settings")

        assert "09:45" in settings
        assert "自动签到：关" in settings
        assert "自动补签：开" in settings
        assert "自动里程碑：关" in settings
        assert "自动周报：关（每周一 09:00）" in settings
        assert "TOKEN_TEST_PLUGIN_FIRST" not in settings
        assert "DEVICE_TEST" not in settings
        plugin.scheduler.stop()

    run(scenario())


def test_manual_weekly_report_command_uses_shared_service() -> None:
    class FakeWeeklyReportService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        async def execute(
            self,
            bot_uuid: str,
            sender_id: str,
            *,
            expected_period: str = "",
        ) -> WeeklyReportOutcome:
            self.calls.append((bot_uuid, sender_id, expected_period))
            return WeeklyReportOutcome(
                report_period="2026年07月06日-2026年07月12日",
                message="问道周报\n测试内容",
            )

    async def scenario() -> None:
        plugin = MemoryWendaoPlugin()
        await plugin.initialize()
        service = FakeWeeklyReportService()
        plugin.weekly_report = service

        reply = await command(plugin, "weekly_report")

        assert reply == "问道周报\n测试内容"
        assert service.calls == [("bot-test-1", "sender-test-1", "")]
        plugin.scheduler.stop()

    run(scenario())


def test_query_uses_shared_workflow_and_unbind_removes_account() -> None:
    async def scenario() -> None:
        bind_client = FakeApiClient({"signInStatus": 2})
        query_client = FakeApiClient(
            {
                "signInStatus": 2,
                "signNum": 7,
                "reSignInStatus": 4,
                "reSignNum": 0,
                "reSignNumLimit": 1,
                "milestoneData": {"state": 1},
            }
        )
        plugin = MemoryWendaoPlugin([bind_client, query_client])
        await plugin.initialize()
        await command(plugin, "bind", BASE_CURL)

        reply = await command(plugin, "query")
        unbound = await command(plugin, "unbind")

        assert "今日已签到" in reply
        assert "7 天" in reply
        assert "解绑成功" in unbound
        assert await plugin.account_store.get("bot-test-1", "sender-test-1") is None
        plugin.scheduler.stop()

    run(scenario())


def test_help_group_policy_and_unbound_errors_are_clear() -> None:
    async def scenario() -> None:
        plugin = MemoryWendaoPlugin()
        await plugin.initialize()

        help_text = await command(plugin, "help")
        query_text = await command(plugin, "query")
        group_text = await plugin.handle_wendao_command(
            bot_uuid="bot-test-1",
            sender_id="sender-test-1",
            target_id="group-test-1",
            is_group=True,
            command=ParsedCommand(kind="signin"),
        )

        assert "问道登录 <手机号>" in help_text
        assert "问道绑定 <登录响应>" not in help_text
        assert "问道验证 <randstr> <ticket>" not in help_text
        assert "问道周报" in help_text
        assert "问道自动周报 开/关" in help_text
        assert "自动刷新" in help_text
        assert query_text == (
            "尚未绑定问道账号，请发送“问道登录 11位手机号”开始登录。"
        )
        assert "问道绑定 <登录响应>" not in query_text
        assert "私聊" in group_text
        plugin.scheduler.stop()

    run(scenario())


def test_scheduler_notification_uses_saved_private_route() -> None:
    async def scenario() -> None:
        plugin = MemoryWendaoPlugin([FakeApiClient({"signInStatus": 2})])
        await plugin.initialize()
        await command(plugin, "bind", BASE_CURL)
        account = await plugin.account_store.get("bot-test-1", "sender-test-1")

        await plugin._notify_account(account, "自动签到完成")

        [sent] = plugin.sent_messages
        assert sent["bot_uuid"] == "bot-test-1"
        assert sent["target_type"] == "person"
        assert sent["target_id"] == "person-target-1"
        chain = sent["message_chain"]
        assert isinstance(chain, MessageChain)
        assert isinstance(chain[0], Plain)
        assert chain[0].text == "自动签到完成"
        plugin.scheduler.stop()

    run(scenario())


def test_unbind_waits_for_running_workflow_and_account_stays_deleted() -> None:
    async def scenario() -> None:
        query_client = BlockingQueryClient()
        plugin = MemoryWendaoPlugin(
            [FakeApiClient({"signInStatus": 2}), query_client]
        )
        await plugin.initialize()
        await command(plugin, "bind", BASE_CURL)

        query_task = asyncio.create_task(command(plugin, "query"))
        await query_client.entered.wait()
        unbind_task = asyncio.create_task(command(plugin, "unbind"))
        await asyncio.sleep(0)

        assert unbind_task.done() is False
        query_client.release.set()
        await query_task
        assert "解绑成功" in await unbind_task
        assert await plugin.account_store.get("bot-test-1", "sender-test-1") is None
        plugin.scheduler.stop()

    run(scenario())

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langbot_plugin.api.entities import events
import langbot_plugin.api.entities.builtin.platform.message as platform_message

from components.command_parser import ParsedCommand
from components.event_listeners.wendao_signin_listener import WendaoSigninListener


class DummyPlugin:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.waiting_for_code = False

    async def is_waiting_for_login_code(self, bot_uuid: str, sender_id: str) -> bool:
        return self.waiting_for_code

    async def handle_wendao_command(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return f"已处理 {kwargs['command'].kind}"


class DummyEvent:
    def __init__(self, text: str, *, launcher_type: str = "person", normal: bool = True) -> None:
        self.launcher_type = launcher_type
        self.launcher_id = "launcher-test-1"
        self.sender_id = "sender-test-1"
        self.query = SimpleNamespace(bot_uuid="bot-test-1")
        self.message_chain = platform_message.MessageChain([platform_message.Plain(text=text)])
        if normal:
            self.reply_message_chain: platform_message.MessageChain | None = None
            self.model_fields = {"reply_message_chain": object()}
        else:
            self.model_fields = {}


class DummyContext:
    def __init__(self, event: DummyEvent, *, query_id: int | None) -> None:
        self.event = event
        self.query_id = query_id
        self.default_prevented = False
        self.postorder_prevented = False
        self.direct_replies: list[platform_message.MessageChain] = []

    def prevent_default(self) -> None:
        self.default_prevented = True

    def prevent_postorder(self) -> None:
        self.postorder_prevented = True

    async def get_bot_uuid(self) -> str:
        return "bot-test-1"

    async def reply(self, message_chain: platform_message.MessageChain) -> None:
        self.direct_replies.append(message_chain)


def run(coro):
    return asyncio.run(coro)


def reply_text(context: DummyContext) -> str:
    chain = getattr(context.event, "reply_message_chain", None)
    if chain is None and context.direct_replies:
        chain = context.direct_replies[0]
    if chain and isinstance(chain[0], platform_message.Plain):
        return chain[0].text
    return ""


def build_listener() -> tuple[WendaoSigninListener, DummyPlugin]:
    listener = WendaoSigninListener()
    plugin = DummyPlugin()
    listener.plugin = plugin
    return listener, plugin


def test_initialize_registers_all_four_message_events() -> None:
    listener, _ = build_listener()

    run(listener.initialize())

    assert set(listener.registered_handlers) == {
        events.PersonMessageReceived,
        events.GroupMessageReceived,
        events.PersonNormalMessageReceived,
        events.GroupNormalMessageReceived,
    }


def test_private_keyword_prevents_default_but_keeps_postorder_plugins() -> None:
    listener, plugin = build_listener()
    context = DummyContext(DummyEvent("问道查询"), query_id=100)

    run(listener._handle_message(context))

    assert context.default_prevented is True
    assert context.postorder_prevented is False
    assert plugin.calls[0]["bot_uuid"] == "bot-test-1"
    assert plugin.calls[0]["sender_id"] == "sender-test-1"
    assert plugin.calls[0]["target_id"] == "launcher-test-1"
    assert plugin.calls[0]["is_group"] is False
    assert plugin.calls[0]["request_id"] == "100"
    assert reply_text(context) == "已处理 query"


def test_missing_query_id_passes_empty_request_id() -> None:
    listener, plugin = build_listener()
    context = DummyContext(DummyEvent("问道积分"), query_id=None)

    run(listener._handle_message(context))

    assert plugin.calls[0]["request_id"] == ""


def test_group_command_except_help_redirects_to_private_chat() -> None:
    listener, plugin = build_listener()
    context = DummyContext(DummyEvent("问道绑定 curl test", launcher_type="group"), query_id=101)

    run(listener._handle_message(context))

    assert context.default_prevented is True
    assert context.postorder_prevented is False
    assert plugin.calls == []
    assert "私聊" in reply_text(context)


def test_group_phone_login_is_rejected_before_sensitive_input_dispatch() -> None:
    listener, plugin = build_listener()
    context = DummyContext(
        DummyEvent("问道登录 13800138000", launcher_type="group"),
        query_id=106,
    )

    run(listener._handle_message(context))

    assert context.default_prevented is True
    assert plugin.calls == []
    assert "私聊" in reply_text(context)
    assert "13800138000" not in reply_text(context)


def test_group_help_is_allowed() -> None:
    listener, plugin = build_listener()
    context = DummyContext(DummyEvent("问道帮助", launcher_type="group"), query_id=102)

    run(listener._handle_message(context))

    assert len(plugin.calls) == 1
    assert plugin.calls[0]["command"].kind == "help"
    assert plugin.calls[0]["is_group"] is True


def test_duplicate_query_id_only_dispatches_once_but_always_prevents_default() -> None:
    listener, plugin = build_listener()
    first = DummyContext(DummyEvent("问道签到"), query_id=103)
    duplicate = DummyContext(DummyEvent("问道签到"), query_id=103)

    async def scenario() -> None:
        await listener._handle_message(first)
        await listener._handle_message(duplicate)

    run(scenario())

    assert len(plugin.calls) == 1
    assert first.default_prevented is True
    assert duplicate.default_prevented is True


def test_unrelated_message_is_ignored() -> None:
    listener, plugin = build_listener()
    context = DummyContext(DummyEvent("今天玩什么游戏"), query_id=104)

    run(listener._handle_message(context))

    assert context.default_prevented is False
    assert plugin.calls == []
    assert reply_text(context) == ""


def test_plain_sms_code_is_only_handled_during_pending_private_login() -> None:
    listener, plugin = build_listener()
    before_login = DummyContext(DummyEvent("873157"), query_id=107)
    pending_login = DummyContext(DummyEvent("873157"), query_id=108)

    async def scenario() -> None:
        await listener._handle_message(before_login)
        plugin.waiting_for_code = True
        await listener._handle_message(pending_login)

    run(scenario())

    assert before_login.default_prevented is False
    assert pending_login.default_prevented is True
    assert len(plugin.calls) == 1
    assert plugin.calls[0]["command"] == ParsedCommand("login_code", "873157")


def test_plain_sms_code_is_not_accepted_in_group_chat() -> None:
    listener, plugin = build_listener()
    plugin.waiting_for_code = True
    context = DummyContext(DummyEvent("873157", launcher_type="group"), query_id=109)

    run(listener._handle_message(context))

    assert context.default_prevented is False
    assert plugin.calls == []


def test_raw_message_event_uses_context_reply_route() -> None:
    listener, _ = build_listener()
    context = DummyContext(DummyEvent("问道设置", normal=False), query_id=105)

    run(listener._handle_message(context))

    assert len(context.direct_replies) == 1
    assert reply_text(context) == "已处理 settings"

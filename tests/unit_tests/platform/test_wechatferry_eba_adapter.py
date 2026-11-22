"""Unit and protocol-loopback tests for the WeChatFerry EBA adapter."""

from __future__ import annotations

import asyncio
import datetime
import json
import pathlib
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
import pytest
import websockets
import yaml

from langbot.pkg.platform.adapters.wechatferry.adapter import WeChatFerryAdapter
from langbot.pkg.platform.adapters.wechatferry.errors import NotSupportedError
from langbot.pkg.platform.adapters.wechatferry.event_converter import WeChatFerryEventConverter
from langbot.pkg.platform.adapters.wechatferry.message_converter import WeChatFerryMessageConverter
from langbot.pkg.platform.adapters.wechatferry.platform_api import PLATFORM_API_MAP
from langbot.pkg.platform.adapters.wechatferry.types import WeChatFerryMessageDeduplicator
from langbot_plugin.api.definition.abstract.platform.event_logger import AbstractEventLogger
from langbot_plugin.api.entities.builtin.platform import entities as platform_entities
from langbot_plugin.api.entities.builtin.platform import events as platform_events
from langbot_plugin.api.entities.builtin.platform import message as platform_message

ADAPTER_DIR = pathlib.Path(__file__).parents[3] / 'src/langbot/pkg/platform/adapters/wechatferry'


class DummyLogger(AbstractEventLogger):
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    async def info(self, text, images=None, message_session_id=None, no_throw=True):
        self.records.append(('info', str(text)))

    async def debug(self, text, images=None, message_session_id=None, no_throw=True):
        self.records.append(('debug', str(text)))

    async def warning(self, text, images=None, message_session_id=None, no_throw=True):
        self.records.append(('warning', str(text)))

    async def error(self, text, images=None, message_session_id=None, no_throw=True):
        self.records.append(('error', str(text)))


def _make_adapter(config: dict | None = None) -> WeChatFerryAdapter:
    cfg = {
        'api_url': 'http://127.0.0.1:18080',
        'ws_url': 'ws://127.0.0.1:18080/ws',
        'bot_wxid': 'wxid_bot_self',
        **(config or {}),
    }
    return WeChatFerryAdapter(cfg, DummyLogger())


def test_manifest_matches_adapter_capabilities_and_platform_api_map():
    manifest = yaml.safe_load((ADAPTER_DIR / 'manifest.yaml').read_text(encoding='utf-8'))
    adapter = _make_adapter()

    assert manifest['metadata']['name'] == adapter.name == 'wechatferry'
    assert (ADAPTER_DIR / manifest['metadata']['icon']).is_file()
    assert manifest['spec']['supported_events'] == adapter.get_supported_events()

    declared_apis = (
        manifest['spec']['supported_apis']['required'] + manifest['spec']['supported_apis']['optional']
    )
    assert declared_apis == adapter.get_supported_apis()

    manifest_actions = [item['action'] for item in manifest['spec']['platform_specific_apis']]
    assert manifest_actions == list(PLATFORM_API_MAP.keys())


def test_deduplicator_evicts_oldest_and_ignores_empty_ids():
    dedup = WeChatFerryMessageDeduplicator(max_size=2)
    assert dedup.is_duplicate('') is False
    assert dedup.is_duplicate(None) is False
    assert dedup.is_duplicate('m1') is False
    assert dedup.is_duplicate('m1') is True
    assert dedup.is_duplicate('m2') is False
    assert dedup.is_duplicate('m3') is False
    # m1 evicted after capacity=2
    assert dedup.is_duplicate('m1') is False


@pytest.mark.asyncio
async def test_message_converter_inbound_components():
    # Text + Source + At + AtAll + Quote
    chain = await WeChatFerryMessageConverter.target2yiri(
        {
            'id': '1001',
            'ts': 1760000000,
            'type': 1,
            'is_at': True,
            'content': '@所有人 你好 world',
            'quote': {'id': '999', 'sender': 'wxid_origin', 'content': 'quoted line'},
        },
        bot_account_id='wxid_bot_self',
    )
    assert any(isinstance(c, platform_message.Source) and c.id == '1001' for c in chain)
    assert any(isinstance(c, platform_message.At) and c.target == 'wxid_bot_self' for c in chain)
    assert any(isinstance(c, platform_message.AtAll) for c in chain)
    assert any(isinstance(c, platform_message.Quote) and c.id == '999' for c in chain)
    assert any(isinstance(c, platform_message.Plain) and c.text == '@所有人 你好 world' for c in chain)

    # Inline Base64 image from <img_b64>
    img_chain = await WeChatFerryMessageConverter.target2yiri(
        {'id': '1002', 'type': 3, 'content': '<img_b64>AAAA</img_b64>', 'extra': 'C:\\Windows\\img.jpg'},
        bot_account_id='wxid_bot_self',
    )
    images = [c for c in img_chain if isinstance(c, platform_message.Image)]
    assert len(images) == 1
    assert images[0].base64 == 'data:image/jpeg;base64,AAAA'

    # Cross-host Windows drive path without Base64 falls back to Plain instead of invalid Image(path=...)
    win_chain = await WeChatFerryMessageConverter.target2yiri(
        {'id': '1003', 'type': 3, 'content': '', 'extra': 'C:\\Users\\foo\\a.jpg'},
        bot_account_id='wxid_bot_self',
    )
    assert not any(isinstance(c, platform_message.Image) for c in win_chain)
    assert any(isinstance(c, platform_message.Plain) and c.text == '[图片]' for c in win_chain)

    # Voice, File, and Unknown
    voice_chain = await WeChatFerryMessageConverter.target2yiri(
        {'id': '1004', 'type': 34, 'voice_b64': 'data:audio/amr;base64,BBBB', 'voice_length': 5}
    )
    assert any(isinstance(c, platform_message.Voice) and c.length == 5 for c in voice_chain)

    file_chain = await WeChatFerryMessageConverter.target2yiri(
        {'id': '1005', 'type': 49, 'file_name': 'report.pdf', 'file_url': 'https://example.com/report.pdf', 'file_size': 1234}
    )
    assert any(isinstance(c, platform_message.File) and c.name == 'report.pdf' and c.size == 1234 for c in file_chain)

    unknown_chain = await WeChatFerryMessageConverter.target2yiri({'id': '1006', 'type': 9999, 'content': ''})
    assert any(isinstance(c, platform_message.Unknown) for c in unknown_chain)


@pytest.mark.asyncio
async def test_message_converter_outbound_mixed_chain_preserves_order():
    chain = platform_message.MessageChain(
        [
            platform_message.Source(id='skip-me', time=datetime.datetime(2026, 11, 22, 12, 0, 0)),
            platform_message.At(target='wxid_alice'),
            platform_message.Plain(text='hello '),
            platform_message.Face(face_id='1', face_name='[smile]'),
            platform_message.Image(base64='data:image/png;base64,AAAA'),
            platform_message.File(id='f1', name='doc.txt', url='https://example.com/doc.txt'),
            platform_message.Voice(url='https://example.com/v.mp3'),
            platform_message.AtAll(),
            platform_message.Plain(text='tail'),
        ]
    )
    ops = await WeChatFerryMessageConverter.yiri2target(chain)
    assert [op['kind'] for op in ops] == ['text', 'image', 'file', 'voice', 'text']
    assert ops[0] == {'kind': 'text', 'text': 'hello [smile]', 'ats': ['wxid_alice']}
    assert ops[1] == {'kind': 'image', 'base64': 'data:image/png;base64,AAAA'}
    assert ops[2] == {'kind': 'file', 'url': 'https://example.com/doc.txt', 'filename': 'doc.txt'}
    assert ops[3] == {'kind': 'voice', 'url': 'https://example.com/v.mp3'}
    assert ops[4] == {'kind': 'text', 'text': 'tail', 'ats': ['notify@all']}


@pytest.mark.asyncio
async def test_event_converter_maps_group_private_platform_specific_and_filters_self():
    converter = WeChatFerryEventConverter(bot_account_id='wxid_bot_self')

    # Self and system accounts filtered
    assert await converter.target2yiri({'sender': 'wxid_bot_self', 'content': 'echo'}) is None
    assert await converter.target2yiri({'sender': 'gh_official', 'content': 'push'}) is None
    assert await converter.target2yiri({'sender': 'weixin', 'content': 'sys'}) is None

    # Group message -> MessageReceivedEvent + legacy GroupMessage
    group_event = await converter.target2yiri(
        {
            'id': '2001',
            'ts': 1760000100,
            'type': 1,
            'sender': 'wxid_alice',
            'sender_name': 'Alice',
            'roomid': '12345@chatroom',
            'room_name': 'Engineering',
            'is_group': True,
            'content': 'hi team',
        }
    )
    assert isinstance(group_event, platform_events.MessageReceivedEvent)
    assert group_event.chat_type == platform_entities.ChatType.GROUP
    assert group_event.chat_id == '12345@chatroom'
    assert group_event.group is not None and group_event.group.name == 'Engineering'
    assert group_event.sender.id == 'wxid_alice' and group_event.sender.nickname == 'Alice'
    legacy = group_event.to_legacy_event()
    assert isinstance(legacy, platform_events.GroupMessage)
    assert legacy.sender.member_name == 'Alice'
    assert legacy.sender.group.name == 'Engineering'

    # Private message -> MessageReceivedEvent + legacy FriendMessage
    private_event = await converter.target2yiri(
        {
            'id': '2002',
            'ts': 1760000200,
            'type': 1,
            'sender': 'wxid_bob',
            'sender_name': 'Bob',
            'content': 'hello dm',
        }
    )
    assert isinstance(private_event, platform_events.MessageReceivedEvent)
    assert private_event.chat_type == platform_entities.ChatType.PRIVATE
    assert private_event.chat_id == 'wxid_bob'
    assert private_event.group is None
    assert isinstance(private_event.to_legacy_event(), platform_events.FriendMessage)

    # Non-message gateway event -> PlatformSpecificEvent
    spec_event = await converter.target2yiri(
        {'event_type': 'friend_request', 'request_id': 'req_1', 'wxid': 'wxid_new', 'ts': 1760000300}
    )
    assert isinstance(spec_event, platform_events.PlatformSpecificEvent)
    assert spec_event.action == 'wechatferry.friend_request'
    assert spec_event.data['request_id'] == 'req_1'


@pytest.mark.asyncio
async def test_dispatcher_prefers_most_specific_eba_listener_and_keeps_legacy_compat():
    adapter = _make_adapter()
    adapter.set_bot_uuid('bot-uuid-1')
    received_specific: list[platform_events.Event] = []
    received_generic: list[platform_events.Event] = []
    received_legacy: list[platform_events.Event] = []

    async def on_specific(ev, _ad):
        received_specific.append(ev)

    async def on_generic(ev, _ad):
        received_generic.append(ev)

    async def on_legacy(ev, _ad):
        received_legacy.append(ev)

    adapter.register_listener(platform_events.MessageReceivedEvent, on_specific)
    adapter.register_listener(platform_events.EBAEvent, on_generic)
    adapter.register_listener(platform_events.FriendMessage, on_legacy)

    await adapter._handle_inbound_data(
        {'id': '3001', 'type': 1, 'sender': 'wxid_bob', 'sender_name': 'Bob', 'content': 'ping'}
    )
    # Duplicate is ignored
    await adapter._handle_inbound_data(
        {'id': '3001', 'type': 1, 'sender': 'wxid_bob', 'sender_name': 'Bob', 'content': 'ping'}
    )

    assert len(received_specific) == 1
    assert received_specific[0].bot_uuid == 'bot-uuid-1'
    assert len(received_generic) == 0
    assert len(received_legacy) == 1


@pytest.mark.asyncio
async def test_unsupported_common_apis_raise_not_supported_error():
    adapter = _make_adapter()
    with pytest.raises(NotSupportedError):
        await adapter.edit_message('private', 'u1', 'm1', platform_message.MessageChain([]))
    with pytest.raises(NotSupportedError):
        await adapter.delete_message('private', 'u1', 'm1')
    with pytest.raises(NotSupportedError):
        await adapter.forward_message('private', 'u1', 'm1', 'group', 'g1')
    with pytest.raises(NotSupportedError):
        await adapter.leave_group('g1')
    with pytest.raises(NotSupportedError):
        await adapter.upload_file(b'abc', 'a.txt')
    with pytest.raises(NotSupportedError):
        await adapter.get_file_url('f1')
    with pytest.raises(NotSupportedError):
        await adapter.call_platform_api('nonexistent_action', {})


@pytest.mark.asyncio
async def test_protocol_loopback_ws_inbound_and_http_apis():
    """Exercise the real WeChatFerryAdapter against a live local HTTP+WS mock gateway."""
    outbound_calls: list[tuple[str, dict]] = []

    async def handle_status(_req):
        return web.json_response({'is_login': True, 'self_wxid': 'wxid_bot_live', 'user_info': {'name': 'LiveBot'}})

    async def handle_contacts(_req):
        return web.json_response(
            {
                'contacts': [
                    {'wxid': 'wxid_alice', 'name': 'Alice', 'remark': 'A'},
                    {'wxid': '888@chatroom', 'name': 'DevGroup'},
                ]
            }
        )

    async def handle_user_name(req):
        return web.json_response({'wxid': req.query['wxid'], 'name': 'Alice'})

    async def handle_members(req):
        return web.json_response({'roomid': req.query['roomid'], 'members': {'wxid_alice': 'Alice'}})

    async def handle_friend_requests(_req):
        return web.json_response({'requests': [{'request_id': 'req_42', 'wxid': 'wxid_new'}]})

    async def handle_post(req):
        body = await req.json()
        outbound_calls.append((req.path, body))
        return web.json_response({'status': 0, 'message_id': f'out_{len(outbound_calls)}'})

    async def handle_ws(req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        await ws.send_json(
            {
                'id': 'ws_msg_1',
                'ts': 1760000500,
                'type': 1,
                'sender': 'wxid_alice',
                'sender_name': 'Alice',
                'roomid': '888@chatroom',
                'room_name': 'DevGroup',
                'is_group': True,
                'is_at': True,
                'content': 'hello loopback',
            }
        )
        await asyncio.sleep(0.3)
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get('/api/status', handle_status)
    app.router.add_get('/api/contacts', handle_contacts)
    app.router.add_get('/api/user_name', handle_user_name)
    app.router.add_get('/api/chatroom_members', handle_members)
    app.router.add_get('/api/friend_requests', handle_friend_requests)
    app.router.add_post('/api/send', handle_post)
    app.router.add_post('/api/send_image', handle_post)
    app.router.add_post('/api/send_file', handle_post)
    app.router.add_post('/api/send_voice', handle_post)
    app.router.add_post('/api/accept_friend', handle_post)
    app.router.add_post('/api/send_rich_text', handle_post)
    app.router.add_get('/ws', handle_ws)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    adapter = _make_adapter(
        {'api_url': f'http://127.0.0.1:{port}', 'ws_url': f'ws://127.0.0.1:{port}/ws', 'bot_wxid': ''}
    )
    got_event = asyncio.Event()
    captured: list[platform_events.MessageReceivedEvent] = []

    async def on_msg(ev: platform_events.MessageReceivedEvent, _ad):
        captured.append(ev)
        got_event.set()

    adapter.register_listener(platform_events.MessageReceivedEvent, on_msg)
    run_task = asyncio.create_task(adapter.run_async())

    try:
        await asyncio.wait_for(got_event.wait(), timeout=5.0)
        assert adapter.bot_account_id == 'wxid_bot_live'
        assert len(captured) == 1
        ev = captured[0]
        assert ev.chat_id == '888@chatroom'
        assert ev.sender.nickname == 'Alice'

        # Reply via EBA event
        reply_res = await adapter.reply_message(
            ev,
            platform_message.MessageChain(
                [
                    platform_message.Plain(text='reply ok'),
                    platform_message.Image(url='https://example.com/a.png'),
                    platform_message.File(id='f1', name='a.pdf', url='https://example.com/a.pdf'),
                    platform_message.Voice(url='https://example.com/a.amr'),
                ]
            ),
        )
        assert isinstance(reply_res, platform_events.MessageResult)
        assert reply_res.raw['sent'] == 4
        assert [p for p, _ in outbound_calls[:4]] == ['/api/send', '/api/send_image', '/api/send_file', '/api/send_voice']

        # Common lookup APIs
        user = await adapter.get_user_info('wxid_alice')
        assert user.nickname == 'Alice'
        friends = await adapter.get_friend_list()
        assert [f.id for f in friends] == ['wxid_alice']
        groups = await adapter.get_group_list()
        assert [g.id for g in groups] == ['888@chatroom']
        group = await adapter.get_group_info('888@chatroom')
        assert group.name == 'DevGroup'
        members = await adapter.get_group_member_list('888@chatroom')
        assert [m.user.id for m in members] == ['wxid_alice']

        # Platform-specific APIs
        status = await adapter.call_platform_api('get_status', {})
        assert status['is_login'] is True
        reqs = await adapter.call_platform_api('get_friend_requests', {})
        assert len(reqs['requests']) == 1
        await adapter.call_platform_api('accept_friend', {'request_id': 'req_42'})
        await adapter.call_platform_api(
            'send_rich_text', {'receiver': '888@chatroom', 'title': 'Doc', 'url': 'https://example.com'}
        )
    finally:
        await adapter.kill()
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        await runner.cleanup()

import asyncio
import importlib
import sys
import threading
import types
from functools import cache
from unittest.mock import AsyncMock, patch

from langbot.pkg.platform.sources import wechatpad_message_guard
from langbot.pkg.platform.sources.wechatpad_message_guard import (
    WeChatPadMessageDeduplicator,
    is_wechatpad_message,
)


@cache
def _load_wechatpad_adapter():
    module_name = 'langbot.pkg.platform.sources.wechatpad'
    if module_name not in sys.modules:
        logger_module = types.ModuleType('langbot.pkg.platform.logger')
        logger_module.EventLogger = object
        with patch.dict(sys.modules, {'langbot.pkg.platform.logger': logger_module}):
            module = importlib.import_module(module_name)
        return module.WeChatPadAdapter

    return sys.modules[module_name].WeChatPadAdapter


def _build_adapter():
    adapter_class = _load_wechatpad_adapter()
    config = {
        'wechatpad_url': 'http://wechatpad.invalid',
        'wechatpad_ws': 'ws://wechatpad.invalid',
        'admin_key': '',
        'token': 'test-token',
        'wxid': 'wxid_bot',
    }
    return adapter_class(config, AsyncMock())


def test_same_new_msg_id_is_seen_only_once():
    deduplicator = WeChatPadMessageDeduplicator()
    event = {'new_msg_id': 123, 'from_user_name': {'str': 'wxid_sender'}}

    assert deduplicator.is_duplicate(event) is False
    assert deduplicator.is_duplicate(event) is True


def test_same_content_with_different_message_ids_is_not_deduplicated():
    deduplicator = WeChatPadMessageDeduplicator()

    first = {'new_msg_id': 123, 'content': {'str': '相同内容'}}
    second = {'new_msg_id': 456, 'content': {'str': '相同内容'}}

    assert deduplicator.is_duplicate(first) is False
    assert deduplicator.is_duplicate(second) is False


def test_non_message_event_is_ignored():
    assert is_wechatpad_message({'Type': 10000, 'loginState': 1}) is False
    assert is_wechatpad_message({'from_user_name': {'str': 'wxid_sender'}}) is True


def test_only_text_messages_are_accepted():
    event = {'from_user_name': {'str': 'wxid_sender'}}

    assert wechatpad_message_guard.is_wechatpad_text_message({**event, 'msg_type': 1}) is True
    assert wechatpad_message_guard.is_wechatpad_text_message({**event, 'msg_type': 3}) is False
    assert wechatpad_message_guard.is_wechatpad_text_message({**event, 'msg_type': 34}) is False
    assert wechatpad_message_guard.is_wechatpad_text_message({**event, 'msg_type': 49}) is False
    assert wechatpad_message_guard.is_wechatpad_text_message(event) is False


def test_non_text_messages_skip_conversion():
    async def scenario():
        adapter = _build_adapter()
        adapter.event_converter.target2yiri = AsyncMock(return_value=None)

        for index, msg_type in enumerate((3, 34, 49), start=1):
            result = await adapter.ws_message(
                {
                    'from_user_name': {'str': 'wxid_sender'},
                    'msg_type': msg_type,
                    'new_msg_id': index,
                }
            )
            assert result == 'ok'

        adapter.event_converter.target2yiri.assert_not_awaited()

    asyncio.run(scenario())


def test_text_message_is_converted_and_dispatched_once():
    async def scenario():
        adapter = _build_adapter()

        class TextEvent:
            pass

        event = TextEvent()
        listener = AsyncMock()
        adapter.event_converter.target2yiri = AsyncMock(return_value=event)
        adapter.listeners = {TextEvent: listener}
        payload = {
            'from_user_name': {'str': 'wxid_sender'},
            'msg_type': 1,
            'new_msg_id': 123,
        }

        assert await adapter.ws_message(payload) == 'ok'
        assert await adapter.ws_message(payload) == 'ok'

        adapter.event_converter.target2yiri.assert_awaited_once()
        listener.assert_awaited_once_with(event, adapter)

    asyncio.run(scenario())


def test_worker_thread_submits_message_to_main_event_loop_without_waiting():
    async def scenario():
        adapter = _build_adapter()
        main_loop = asyncio.get_running_loop()
        main_thread_id = threading.get_ident()
        listener_started = asyncio.Event()
        listener_release = asyncio.Event()
        listener_finished = asyncio.Event()
        execution_context = {}

        async def fake_ws_message(self, data):
            execution_context['loop'] = asyncio.get_running_loop()
            execution_context['thread_id'] = threading.get_ident()
            listener_started.set()
            await listener_release.wait()
            listener_finished.set()

        object.__setattr__(adapter, 'ws_message', types.MethodType(fake_ws_message, adapter))

        worker = threading.Thread(
            target=adapter._submit_ws_message,
            args=(main_loop, {'msg_type': 1}),
        )
        worker.start()
        worker.join(timeout=0.2)

        assert worker.is_alive() is False
        await asyncio.wait_for(listener_started.wait(), timeout=1)
        assert execution_context == {
            'loop': main_loop,
            'thread_id': main_thread_id,
        }

        listener_release.set()
        await asyncio.wait_for(listener_finished.wait(), timeout=1)

    asyncio.run(scenario())


def test_main_loop_follow_up_task_is_not_cancelled_after_dispatch():
    async def scenario():
        adapter = _build_adapter()
        main_loop = asyncio.get_running_loop()
        follow_up_finished = asyncio.Event()

        async def fake_ws_message(self, data):
            async def follow_up():
                await asyncio.sleep(0.01)
                follow_up_finished.set()

            asyncio.create_task(follow_up())

        object.__setattr__(adapter, 'ws_message', types.MethodType(fake_ws_message, adapter))

        worker = threading.Thread(
            target=adapter._submit_ws_message,
            args=(main_loop, {'msg_type': 1}),
        )
        worker.start()
        worker.join(timeout=0.2)

        assert worker.is_alive() is False
        await asyncio.wait_for(follow_up_finished.wait(), timeout=1)

    asyncio.run(scenario())

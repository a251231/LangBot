"""Auxiliary live adapter probe for the WeChatFerry EBA adapter.

Usage:
    WCF_API_URL=http://127.0.0.1:18080 WCF_WS_URL=ws://127.0.0.1:18080/ws \\
        uv run python tests/e2e/live_wechatferry_eba_probe.py --wait-seconds 15

Pass ``--send-target <wxid_or_chatroom>`` to also exercise outbound send_message.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import time

from langbot.pkg.platform.adapters.wechatferry.adapter import WeChatFerryAdapter
from langbot_plugin.api.definition.abstract.platform.event_logger import AbstractEventLogger
from langbot_plugin.api.entities.builtin.platform import events as platform_events
from langbot_plugin.api.entities.builtin.platform import message as platform_message


class ProbeLogger(AbstractEventLogger):
    async def info(self, text, images=None, message_session_id=None, no_throw=True):
        print(f'[INFO] {text}')

    async def debug(self, text, images=None, message_session_id=None, no_throw=True):
        pass

    async def warning(self, text, images=None, message_session_id=None, no_throw=True):
        print(f'[WARN] {text}')

    async def error(self, text, images=None, message_session_id=None, no_throw=True):
        print(f'[ERROR] {text}')


def _write_record(log_path: pathlib.Path, record: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a', encoding='utf-8') as f:
        f.write(json.dumps({'ts': time.time(), **record}, ensure_ascii=False) + '\n')


async def main() -> None:
    parser = argparse.ArgumentParser(description='Live WeChatFerry EBA adapter probe')
    parser.add_argument('--api-url', default=os.environ.get('WCF_API_URL', 'http://127.0.0.1:18080'))
    parser.add_argument('--ws-url', default=os.environ.get('WCF_WS_URL', 'ws://127.0.0.1:18080/ws'))
    parser.add_argument('--bot-wxid', default=os.environ.get('WCF_BOT_WXID', ''))
    parser.add_argument('--wait-seconds', type=int, default=15)
    parser.add_argument('--send-target', default=os.environ.get('WCF_SEND_TARGET', ''))
    parser.add_argument('--log', default='data/temp/wechatferry_eba_live_probe.jsonl')
    args = parser.parse_args()

    log_path = pathlib.Path(args.log)
    adapter = WeChatFerryAdapter(
        {'api_url': args.api_url, 'ws_url': args.ws_url, 'bot_wxid': args.bot_wxid},
        ProbeLogger(),
    )

    passed: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    observed_events: list[str] = []

    async def on_any(event: platform_events.EBAEvent, _ad):
        observed_events.append(event.type)
        summary = {
            'kind': 'inbound_event',
            'event_class': type(event).__name__,
            'event_type': event.type,
            'adapter_name': event.adapter_name,
            'bot_uuid': event.bot_uuid,
        }
        if isinstance(event, platform_events.MessageReceivedEvent):
            summary.update(
                {
                    'chat_type': event.chat_type.value,
                    'chat_id': event.chat_id,
                    'sender_id': event.sender.id,
                    'components': [type(c).__name__ for c in event.message_chain],
                }
            )
        _write_record(log_path, summary)

    adapter.register_listener(platform_events.EBAEvent, on_any)

    try:
        status = await adapter.call_platform_api('get_status', {})
        passed.append('call_platform_api:get_status')
        _write_record(log_path, {'kind': 'api', 'action': 'get_status', 'ok': True, 'is_login': status.get('is_login')})
    except Exception as exc:
        failed.append(f'call_platform_api:get_status ({exc})')
        status = {}

    if status.get('is_login'):
        for api_name, coro in [
            ('get_friend_list', adapter.get_friend_list()),
            ('get_group_list', adapter.get_group_list()),
            ('call_platform_api:get_friend_requests', adapter.call_platform_api('get_friend_requests', {})),
        ]:
            try:
                res = await coro
                passed.append(api_name)
                _write_record(
                    log_path,
                    {'kind': 'api', 'action': api_name, 'ok': True, 'count': len(res) if isinstance(res, list) else None},
                )
            except Exception as exc:
                failed.append(f'{api_name} ({exc})')
                _write_record(log_path, {'kind': 'api', 'action': api_name, 'ok': False, 'error': str(exc)})

        if args.send_target:
            target_type = 'group' if '@chatroom' in args.send_target else 'person'
            try:
                res = await adapter.send_message(
                    target_type,
                    args.send_target,
                    platform_message.MessageChain([platform_message.Plain(text='[LangBot EBA probe] ping')]),
                )
                passed.append('send_message:Plain')
                _write_record(log_path, {'kind': 'api', 'action': 'send_message', 'ok': True, 'raw': res.raw})
            except Exception as exc:
                failed.append(f'send_message:Plain ({exc})')
        else:
            skipped.append('send_message (pass --send-target to enable)')
    else:
        skipped.append('logged-in APIs and outbound send (gateway is_login=false)')

    if args.wait_seconds > 0:
        task = asyncio.create_task(adapter.run_async())
        try:
            await asyncio.sleep(args.wait_seconds)
        finally:
            await adapter.kill()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    print(
        json.dumps(
            {'passed': passed, 'failed': failed, 'skipped': skipped, 'observed_events': observed_events},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == '__main__':
    asyncio.run(main())

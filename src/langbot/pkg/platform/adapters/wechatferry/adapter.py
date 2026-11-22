from __future__ import annotations

import asyncio
import json
import traceback
import typing

import aiohttp
import pydantic
import websockets

import langbot_plugin.api.definition.abstract.platform.adapter as abstract_platform_adapter
import langbot_plugin.api.definition.abstract.platform.event_logger as abstract_platform_logger
from langbot_plugin.api.entities.builtin.platform import events as platform_events
from langbot_plugin.api.entities.builtin.platform import message as platform_message

from langbot.pkg.platform.adapters.wechatferry.api_impl import WeChatFerryAPIMixin
from langbot.pkg.platform.adapters.wechatferry.errors import NotSupportedError
from langbot.pkg.platform.adapters.wechatferry.event_converter import WeChatFerryEventConverter
from langbot.pkg.platform.adapters.wechatferry.message_converter import WeChatFerryMessageConverter
from langbot.pkg.platform.adapters.wechatferry.platform_api import PLATFORM_API_MAP
from langbot.pkg.platform.adapters.wechatferry.types import (
    ADAPTER_NAME,
    DEFAULT_API_URL,
    DEFAULT_WS_MAX_SIZE_MB,
    DEFAULT_WS_URL,
    MIN_WS_MAX_SIZE_MB,
    WeChatFerryMessageDeduplicator,
)


class WeChatFerryAdapter(WeChatFerryAPIMixin, abstract_platform_adapter.AbstractPlatformAdapter):
    """WeChatFerry / WeChat 4.x Helper Native Bridge EBA platform adapter."""

    model_config = pydantic.ConfigDict(extra='allow', arbitrary_types_allowed=True)

    name: str = ADAPTER_NAME
    bot_account_id: str = ''
    bot_uuid: str | None = None
    config: dict = pydantic.Field(default_factory=dict)
    logger: typing.Any = None
    message_converter: WeChatFerryMessageConverter = pydantic.Field(default_factory=WeChatFerryMessageConverter)
    event_converter: WeChatFerryEventConverter = pydantic.Field(default_factory=WeChatFerryEventConverter)
    listeners: dict[
        typing.Type[platform_events.Event],
        typing.Callable[[platform_events.Event, abstract_platform_adapter.AbstractMessagePlatformAdapter], None],
    ] = pydantic.Field(default_factory=dict)

    _deduplicator: WeChatFerryMessageDeduplicator = pydantic.PrivateAttr(default_factory=WeChatFerryMessageDeduplicator)
    _running: bool = pydantic.PrivateAttr(default=False)
    _ws_task: typing.Optional[asyncio.Task] = pydantic.PrivateAttr(default=None)
    _inbound_tasks: set[asyncio.Task] = pydantic.PrivateAttr(default_factory=set)

    def __init__(self, config: dict, logger: abstract_platform_logger.AbstractEventLogger, **kwargs):
        bot_wxid = str(config.get('bot_wxid') or '')
        message_converter = WeChatFerryMessageConverter()
        event_converter = WeChatFerryEventConverter(bot_account_id=bot_wxid)
        super().__init__(
            config=config,
            logger=logger,
            bot_account_id=bot_wxid,
            message_converter=message_converter,
            event_converter=event_converter,
            listeners={},
            name=ADAPTER_NAME,
            **kwargs,
        )
        self.message_converter = message_converter
        self.event_converter = event_converter
        self.listeners = {}

    def set_bot_uuid(self, bot_uuid: str) -> None:
        self.bot_uuid = bot_uuid

    def _api_base_url(self) -> str:
        return str(self.config.get('api_url') or DEFAULT_API_URL).rstrip('/')

    def get_supported_events(self) -> list[str]:
        return [
            'message.received',
            'platform.specific',
        ]

    def get_supported_apis(self) -> list[str]:
        return [
            'send_message',
            'reply_message',
            'get_user_info',
            'get_friend_list',
            'get_group_list',
            'get_group_info',
            'get_group_member_list',
            'call_platform_api',
        ]

    async def _http_get(
        self,
        path: str,
        params: dict[str, typing.Any] | None = None,
        timeout_seconds: float = 10.0,
    ) -> typing.Any:
        url = f'{self._api_base_url()}{path}'
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as resp:
                if resp.status >= 400:
                    detail = await resp.text()
                    raise RuntimeError(f'WeChatFerry GET {path} failed ({resp.status}): {detail[:300]}')
                return await resp.json()

    async def _http_post(
        self,
        path: str,
        payload: dict[str, typing.Any],
        timeout_seconds: float = 20.0,
    ) -> typing.Any:
        url = f'{self._api_base_url()}{path}'
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as resp:
                if resp.status >= 400:
                    detail = await resp.text()
                    raise RuntimeError(f'WeChatFerry POST {path} failed ({resp.status}): {detail[:300]}')
                return await resp.json()

    async def send_message(
        self,
        target_type: str,
        target_id: str,
        message: platform_message.MessageChain,
    ) -> platform_events.MessageResult:
        """Send an ordered MessageChain to a group chat or private WeChat user."""
        operations = await self.message_converter.yiri2target(message)
        results: list[dict[str, typing.Any]] = []
        last_message_id = ''

        for op in operations:
            kind = op['kind']
            if kind == 'text':
                payload = {
                    'receiver': str(target_id),
                    'msg': op.get('text', ''),
                    'aters': ','.join(op.get('ats') or []),
                }
                res = await self._http_post('/api/send', payload, timeout_seconds=10.0)
            elif kind == 'image':
                payload = {
                    'receiver': str(target_id),
                    'path': op.get('path', ''),
                    'url': op.get('url', ''),
                    'base64': op.get('base64', ''),
                }
                res = await self._http_post('/api/send_image', payload, timeout_seconds=20.0)
            elif kind == 'file':
                payload = {
                    'receiver': str(target_id),
                    'path': op.get('path', ''),
                    'url': op.get('url', ''),
                    'base64': op.get('base64', ''),
                    'filename': op.get('filename', ''),
                }
                res = await self._http_post('/api/send_file', payload, timeout_seconds=30.0)
            elif kind == 'voice':
                payload = {
                    'receiver': str(target_id),
                    'path': op.get('path', ''),
                    'url': op.get('url', ''),
                    'base64': op.get('base64', ''),
                    'filename': op.get('filename', ''),
                }
                res = await self._http_post('/api/send_voice', payload, timeout_seconds=20.0)
            else:
                continue

            if isinstance(res, dict) and res.get('status', 0) != 0:
                await self.logger.error(f'WeChatFerry outbound {kind} failed: {res}')
                raise RuntimeError(f'WeChatFerry outbound {kind} failed: {res}')
            if isinstance(res, dict):
                mid = str(res.get('message_id') or res.get('msg_id') or res.get('id') or '')
                if mid:
                    last_message_id = mid
            results.append({'kind': kind, 'response': res})

        return platform_events.MessageResult(
            message_id=last_message_id,
            raw={'target_id': str(target_id), 'sent': len(results), 'results': results},
        )

    async def reply_message(
        self,
        message_source: platform_events.MessageEvent | platform_events.EBAEvent,
        message: platform_message.MessageChain,
        quote_origin: bool = False,
    ) -> platform_events.MessageResult:
        """Reply to a legacy MessageEvent or unified EBA MessageReceivedEvent."""
        if isinstance(message_source, platform_events.MessageReceivedEvent):
            target_type = 'group' if message_source.chat_type.value == 'group' else 'person'
            target_id = str(message_source.chat_id)
            return await self.send_message(target_type, target_id, message)
        if isinstance(message_source, platform_events.GroupMessage):
            return await self.send_message('group', str(message_source.sender.group.id), message)
        if isinstance(message_source, platform_events.FriendMessage):
            return await self.send_message('person', str(message_source.sender.id), message)

        raw_obj = getattr(message_source, 'source_platform_object', None)
        if isinstance(raw_obj, dict):
            roomid = str(raw_obj.get('roomid') or '')
            sender = str(raw_obj.get('sender') or '')
            if roomid:
                return await self.send_message('group', roomid, message)
            if sender:
                return await self.send_message('person', sender, message)

        raise NotSupportedError('reply_message:wechatferry_missing_target')

    async def call_platform_api(self, action: str, params: dict = {}) -> dict:
        handler = PLATFORM_API_MAP.get(action)
        if handler is None:
            raise NotSupportedError(f'call_platform_api:{action}')
        return await handler(self, dict(params or {}))

    def register_listener(
        self,
        event_type: typing.Type[platform_events.Event],
        callback: typing.Callable[
            [platform_events.Event, abstract_platform_adapter.AbstractMessagePlatformAdapter],
            None,
        ],
    ):
        self.listeners[event_type] = callback

    def unregister_listener(
        self,
        event_type: typing.Type[platform_events.Event],
        callback: typing.Callable[
            [platform_events.Event, abstract_platform_adapter.AbstractMessagePlatformAdapter],
            None,
        ],
    ):
        registered = self.listeners.get(event_type)
        if registered is callback:
            self.listeners.pop(event_type, None)

    async def _dispatch_eba_event(self, event: platform_events.EBAEvent) -> None:
        """Deliver to the most specific registered EBA listener."""
        for event_type in (type(event), platform_events.EBAEvent, platform_events.Event):
            callback = self.listeners.get(event_type)
            if callback:
                await callback(event, self)
                return

    async def _handle_inbound_data(self, data: dict[str, typing.Any]) -> None:
        """Deduplicate, convert, and dispatch a single inbound gateway payload."""
        try:
            msg_id = data.get('id') or data.get('msg_id')
            if msg_id and self._deduplicator.is_duplicate(msg_id):
                return

            eba_event = await self.event_converter.target2yiri(data, bot_account_id=self.bot_account_id)
            if eba_event is None:
                return

            if self.bot_uuid and not eba_event.bot_uuid:
                eba_event.bot_uuid = self.bot_uuid

            if isinstance(eba_event, platform_events.MessageReceivedEvent):
                legacy_event = eba_event.to_legacy_event()
                legacy_listener = self.listeners.get(type(legacy_event))
                if legacy_listener:
                    await legacy_listener(legacy_event, self)

            await self._dispatch_eba_event(eba_event)
        except Exception as ex:
            await self.logger.error(f'WeChatFerry inbound dispatch error: {ex}\n{traceback.format_exc()}')

    async def run_async(self):
        """Connect to the WeChatFerry WebSocket gateway and stream events."""
        self._running = True
        ws_url = str(self.config.get('ws_url') or DEFAULT_WS_URL)
        api_url = self._api_base_url()

        await self.logger.info(f'WeChatFerry EBA adapter starting (api_url={api_url}, ws_url={ws_url})')

        try:
            status_data = await self._http_get('/api/status', timeout_seconds=5.0)
            if isinstance(status_data, dict) and status_data.get('is_login'):
                resolved_wxid = str(status_data.get('self_wxid') or self.bot_account_id or '')
                if resolved_wxid:
                    self.bot_account_id = resolved_wxid
                    self.event_converter.bot_account_id = resolved_wxid
                    self.config['bot_wxid'] = resolved_wxid
                user_info = status_data.get('user_info') or {}
                await self.logger.info(
                    f'WeChatFerry account online: {user_info.get("name", "WeChat")} ({self.bot_account_id})'
                )
        except Exception as exc:
            await self.logger.warning(f'WeChatFerry startup status probe failed (non-fatal): {exc}')

        self._ws_task = asyncio.create_task(self._ws_loop(ws_url))
        try:
            await self._ws_task
        except asyncio.CancelledError:
            pass

    async def _ws_loop(self, ws_url: str) -> None:
        """Reconnectable WebSocket loop supporting 64MiB frames for inline Base64 media."""
        retry_delay = 2.0
        max_retry_delay = 30.0
        ws_max_size_mb = max(
            MIN_WS_MAX_SIZE_MB,
            int(self.config.get('ws_max_size_mb', DEFAULT_WS_MAX_SIZE_MB) or DEFAULT_WS_MAX_SIZE_MB),
        )
        ws_max_size = ws_max_size_mb * 1024 * 1024

        while self._running:
            try:
                await self.logger.info(
                    f'Connecting to WeChatFerry WebSocket: {ws_url} (max_size={ws_max_size_mb}MiB)'
                )
                async with websockets.connect(
                    ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                    max_size=ws_max_size,
                ) as ws:
                    await self.logger.info('WeChatFerry WebSocket connected.')
                    retry_delay = 2.0

                    async for raw_message in ws:
                        if not self._running:
                            break
                        try:
                            if isinstance(raw_message, bytes):
                                raw_message = raw_message.decode('utf-8', errors='ignore')
                            data = json.loads(raw_message)
                            if not isinstance(data, dict):
                                continue
                            task = asyncio.create_task(self._handle_inbound_data(data))
                            self._inbound_tasks.add(task)
                            task.add_done_callback(self._inbound_tasks.discard)
                        except json.JSONDecodeError:
                            await self.logger.warning(f'WeChatFerry ignored non-JSON frame: {str(raw_message)[:100]}')
                        except Exception as ex:
                            await self.logger.error(f'WeChatFerry WS frame error: {ex}\n{traceback.format_exc()}')
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._running:
                    break
                await self.logger.error(f'WeChatFerry WebSocket disconnected ({exc}); retrying in {retry_delay:.1f}s')
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, max_retry_delay)

    async def is_muted(self, group_id: int | None = None) -> bool:
        return False

    async def kill(self) -> bool:
        self._running = False
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
        for task in list(self._inbound_tasks):
            if not task.done():
                task.cancel()
        self._inbound_tasks.clear()
        return True

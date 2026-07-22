from __future__ import annotations

import asyncio
from collections import deque
import re
from typing import Any

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import context, events
import langbot_plugin.api.entities.builtin.platform.message as platform_message

from components.command_parser import ParsedCommand, parse_keyword


MAX_SEEN_QUERY_IDS = 2048
PLAIN_SMS_CODE_RE = re.compile(r'\d{4,8}')


class WendaoSigninListener(EventListener):
    def __init__(self) -> None:
        super().__init__()
        self._seen_query_ids: set[str] = set()
        self._seen_query_order: deque[str] = deque()
        self._seen_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await super().initialize()

        @self.handler(events.PersonMessageReceived)
        async def _on_person_message(event_ctx: context.EventContext) -> None:
            await self._handle_message(event_ctx)

        @self.handler(events.GroupMessageReceived)
        async def _on_group_message(event_ctx: context.EventContext) -> None:
            await self._handle_message(event_ctx)

        @self.handler(events.PersonNormalMessageReceived)
        async def _on_person_normal_message(event_ctx: context.EventContext) -> None:
            await self._handle_message(event_ctx)

        @self.handler(events.GroupNormalMessageReceived)
        async def _on_group_normal_message(event_ctx: context.EventContext) -> None:
            await self._handle_message(event_ctx)

    @staticmethod
    def _extract_plain_text(message_chain: platform_message.MessageChain) -> str:
        parts = [
            str(component.text)
            for component in message_chain
            if isinstance(component, platform_message.Plain) and str(component.text).strip()
        ]
        return '\n'.join(parts).strip()

    async def _is_duplicate(self, query_id: Any) -> bool:
        if query_id is None:
            return False
        key = str(query_id)
        async with self._seen_lock:
            if key in self._seen_query_ids:
                return True
            if len(self._seen_query_order) >= MAX_SEEN_QUERY_IDS:
                oldest = self._seen_query_order.popleft()
                self._seen_query_ids.discard(oldest)
            self._seen_query_ids.add(key)
            self._seen_query_order.append(key)
            return False

    async def _resolve_bot_uuid(self, event_ctx: context.EventContext) -> str:
        try:
            bot_uuid = await event_ctx.get_bot_uuid()
            if bot_uuid:
                return str(bot_uuid)
        except Exception:
            pass
        query = getattr(event_ctx.event, 'query', None)
        return str(getattr(query, 'bot_uuid', '') or '')

    @staticmethod
    def _supports_reply_chain(event: Any) -> bool:
        model_fields = getattr(type(event), 'model_fields', None)
        if not isinstance(model_fields, dict):
            model_fields = getattr(event, 'model_fields', {})
        return isinstance(model_fields, dict) and 'reply_message_chain' in model_fields

    async def _reply_text(self, event_ctx: context.EventContext, text: str) -> None:
        chain = platform_message.MessageChain([platform_message.Plain(text=text)])
        if self._supports_reply_chain(event_ctx.event):
            event_ctx.event.reply_message_chain = chain
            return
        await event_ctx.reply(chain)

    async def _dispatch(
        self,
        event_ctx: context.EventContext,
        command: ParsedCommand,
        *,
        is_group: bool,
    ) -> str:
        bot_uuid = await self._resolve_bot_uuid(event_ctx)
        event = event_ctx.event
        return await self.plugin.handle_wendao_command(  # type: ignore[attr-defined]
            bot_uuid=bot_uuid,
            sender_id=str(getattr(event, 'sender_id', '')),
            target_id=str(getattr(event, 'launcher_id', '')),
            is_group=is_group,
            command=command,
            request_id=str(getattr(event_ctx, 'query_id', None) or ''),
        )

    async def _handle_message(self, event_ctx: context.EventContext) -> None:
        text = self._extract_plain_text(event_ctx.event.message_chain)
        command = parse_keyword(text)
        launcher_type = str(getattr(event_ctx.event, 'launcher_type', '')).lower()
        is_group = launcher_type == 'group'
        if command is None and PLAIN_SMS_CODE_RE.fullmatch(text):
            bot_uuid = await self._resolve_bot_uuid(event_ctx)
            try:
                waiting = await self.plugin.should_accept_plain_login_code(  # type: ignore[attr-defined]
                    bot_uuid=bot_uuid,
                    sender_id=str(getattr(event_ctx.event, 'sender_id', '')),
                    target_id=str(getattr(event_ctx.event, 'launcher_id', '')),
                    is_group=is_group,
                )
            except Exception:
                waiting = False
            if waiting:
                command = ParsedCommand('login_code', text)
        if command is None:
            return

        event_ctx.prevent_default()
        if await self._is_duplicate(getattr(event_ctx, 'query_id', None)):
            return

        try:
            reply = await self._dispatch(event_ctx, command, is_group=is_group)
        except Exception:
            reply = '问道签到助手执行失败，请稍后重试。'
        if reply:
            await self._reply_text(event_ctx, reply)

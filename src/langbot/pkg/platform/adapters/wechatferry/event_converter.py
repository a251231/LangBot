from __future__ import annotations

import time
import typing

import langbot_plugin.api.definition.abstract.platform.adapter as abstract_platform_adapter
from langbot_plugin.api.entities.builtin.platform import entities as platform_entities
from langbot_plugin.api.entities.builtin.platform import events as platform_events

from langbot.pkg.platform.adapters.wechatferry.message_converter import WeChatFerryMessageConverter
from langbot.pkg.platform.adapters.wechatferry.types import ADAPTER_NAME, SYSTEM_SENDERS


def _event_timestamp(raw_ts: typing.Any) -> float:
    try:
        ts = float(raw_ts)
        if ts > 1e12:
            ts /= 1000.0
        if ts > 0:
            return ts
    except (TypeError, ValueError):
        pass
    return time.time()


class WeChatFerryEventConverter(abstract_platform_adapter.AbstractEventConverter):
    """Convert WeChatFerry / Helper V4-V7 gateway payloads into unified EBA events."""

    def __init__(self, bot_account_id: str = ''):
        self.bot_account_id = bot_account_id

    @staticmethod
    async def yiri2target(event: platform_events.Event) -> typing.Any:
        return getattr(event, 'source_platform_object', None)

    async def target2yiri(
        self,
        event: dict[str, typing.Any],
        bot_account_id: str | None = None,
    ) -> typing.Optional[platform_events.EBAEvent]:
        """Convert one inbound gateway JSON object into an EBA event, or None if ignored."""
        if not isinstance(event, dict):
            return None

        effective_bot_id = self.bot_account_id if bot_account_id is None else bot_account_id
        timestamp = _event_timestamp(event.get('ts') or event.get('timestamp'))

        # Non-message gateway notifications stay visible via PlatformSpecificEvent.
        event_kind = str(event.get('event_type') or '').strip()
        if event_kind and event_kind.lower() != 'message':
            return platform_events.PlatformSpecificEvent(
                adapter_name=ADAPTER_NAME,
                timestamp=timestamp,
                action=f'wechatferry.{event_kind}',
                data=dict(event),
                source_platform_object=event,
            )

        sender = str(event.get('sender') or '').strip()
        roomid = str(event.get('roomid') or '').strip()

        if not sender:
            return None
        if effective_bot_id and sender == effective_bot_id:
            return None
        if sender.startswith('gh_') or sender in SYSTEM_SENDERS:
            return None

        message_chain = await WeChatFerryMessageConverter.target2yiri(event, bot_account_id=effective_bot_id)
        if not message_chain:
            return None

        msg_id = str(event.get('id') or event.get('msg_id') or '')
        sender_name = str(event.get('sender_name') or sender)
        room_name = str(event.get('room_name') or roomid)
        is_group = bool(event.get('is_group', False)) or ('@chatroom' in roomid)

        sender_user = platform_entities.User(
            id=sender,
            nickname=sender_name,
        )

        if is_group:
            chat_id = roomid or sender
            group = platform_entities.UserGroup(
                id=chat_id,
                name=room_name or chat_id,
            )
            sender_member = platform_entities.UserGroupMember(
                user=sender_user,
                group_id=chat_id,
                role=platform_entities.MemberRole.MEMBER,
                display_name=sender_name,
            )
            return platform_events.MessageReceivedEvent(
                adapter_name=ADAPTER_NAME,
                timestamp=timestamp,
                message_id=msg_id,
                message_chain=message_chain,
                sender=sender_user,
                sender_member=sender_member,
                chat_type=platform_entities.ChatType.GROUP,
                chat_id=chat_id,
                group=group,
                source_platform_object=event,
            )

        return platform_events.MessageReceivedEvent(
            adapter_name=ADAPTER_NAME,
            timestamp=timestamp,
            message_id=msg_id,
            message_chain=message_chain,
            sender=sender_user,
            chat_type=platform_entities.ChatType.PRIVATE,
            chat_id=sender,
            group=None,
            source_platform_object=event,
        )

    async def target2legacy(
        self,
        event: dict[str, typing.Any],
        bot_account_id: str | None = None,
    ) -> platform_events.FriendMessage | platform_events.GroupMessage | None:
        """Build the legacy FriendMessage/GroupMessage event for pipeline bindings."""
        eba_event = await self.target2yiri(event, bot_account_id=bot_account_id)
        if not isinstance(eba_event, platform_events.MessageReceivedEvent):
            return None
        return eba_event.to_legacy_event()

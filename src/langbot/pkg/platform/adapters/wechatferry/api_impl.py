from __future__ import annotations

import typing

from langbot_plugin.api.entities.builtin.platform import entities as platform_entities
from langbot_plugin.api.entities.builtin.platform import events as platform_events
from langbot_plugin.api.entities.builtin.platform import message as platform_message

from langbot.pkg.platform.adapters.wechatferry.errors import NotSupportedError


def _extract_items(payload: typing.Any) -> list[dict[str, typing.Any]]:
    """Normalize gateway list responses (bare list or wrapped under data/contacts/members)."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ('data', 'contacts', 'members', 'items', 'list'):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


class WeChatFerryAPIMixin:
    """Common EBA API implementations backed by the WeChatFerry / Helper V4+ HTTP gateway."""

    async def get_user_info(self, user_id: typing.Union[int, str]) -> platform_entities.User:
        wxid = str(user_id)
        data = await self._http_get('/api/user_name', params={'wxid': wxid})
        name = ''
        if isinstance(data, dict):
            name = str(data.get('name') or data.get('nickname') or data.get('user_name') or '')
            if not name and isinstance(data.get('data'), dict):
                inner = data['data']
                name = str(inner.get('name') or inner.get('nickname') or '')
        return platform_entities.User(
            id=wxid,
            nickname=name or wxid,
            remark=str(data.get('remark') or '') if isinstance(data, dict) else '',
        )

    async def get_friend_list(self) -> list[platform_entities.User]:
        data = await self._http_get('/api/contacts')
        friends: list[platform_entities.User] = []
        for item in _extract_items(data):
            wxid = str(item.get('wxid') or item.get('id') or '')
            if not wxid or '@chatroom' in wxid or wxid.startswith('gh_'):
                continue
            friends.append(
                platform_entities.User(
                    id=wxid,
                    nickname=str(item.get('name') or item.get('nickname') or wxid),
                    remark=str(item.get('remark') or ''),
                    avatar_url=str(item.get('avatar') or item.get('avatar_url') or ''),
                )
            )
        return friends

    async def get_group_list(self) -> list[platform_entities.UserGroup]:
        data = await self._http_get('/api/contacts')
        groups: list[platform_entities.UserGroup] = []
        for item in _extract_items(data):
            wxid = str(item.get('wxid') or item.get('id') or '')
            if '@chatroom' not in wxid:
                continue
            groups.append(
                platform_entities.UserGroup(
                    id=wxid,
                    name=str(item.get('name') or item.get('nickname') or wxid),
                    avatar_url=str(item.get('avatar') or item.get('avatar_url') or ''),
                )
            )
        return groups

    async def get_group_info(self, group_id: typing.Union[int, str]) -> platform_entities.UserGroup:
        target = str(group_id)
        for group in await self.get_group_list():
            if group.id == target:
                return group
        raise NotSupportedError('get_group_info:group_not_found')

    async def get_group_member_list(
        self,
        group_id: typing.Union[int, str],
    ) -> list[platform_entities.UserGroupMember]:
        roomid = str(group_id)
        data = await self._http_get('/api/chatroom_members', params={'roomid': roomid})
        members: list[platform_entities.UserGroupMember] = []
        if isinstance(data, dict) and isinstance(data.get('members'), dict):
            for wxid, display_name in data['members'].items():
                name = str(display_name or wxid)
                members.append(
                    platform_entities.UserGroupMember(
                        user=platform_entities.User(id=str(wxid), nickname=name),
                        group_id=roomid,
                        role=platform_entities.MemberRole.MEMBER,
                        display_name=name,
                    )
                )
            return members

        for item in _extract_items(data):
            wxid = str(item.get('wxid') or item.get('id') or '')
            if not wxid:
                continue
            name = str(item.get('name') or item.get('display_name') or item.get('nickname') or wxid)
            members.append(
                platform_entities.UserGroupMember(
                    user=platform_entities.User(id=wxid, nickname=name),
                    group_id=roomid,
                    role=platform_entities.MemberRole.MEMBER,
                    display_name=name,
                )
            )
        return members

    async def edit_message(
        self,
        chat_type: str,
        chat_id: typing.Union[int, str],
        message_id: typing.Union[int, str],
        new_content: platform_message.MessageChain,
    ) -> None:
        raise NotSupportedError('edit_message')

    async def delete_message(
        self,
        chat_type: str,
        chat_id: typing.Union[int, str],
        message_id: typing.Union[int, str],
    ) -> None:
        raise NotSupportedError('delete_message')

    async def forward_message(
        self,
        from_chat_type: str,
        from_chat_id: typing.Union[int, str],
        message_id: typing.Union[int, str],
        to_chat_type: str,
        to_chat_id: typing.Union[int, str],
    ) -> platform_events.MessageResult:
        raise NotSupportedError('forward_message')

    async def leave_group(self, group_id: typing.Union[int, str]) -> None:
        raise NotSupportedError('leave_group')

    async def upload_file(self, file_data: bytes, filename: str) -> str:
        raise NotSupportedError('upload_file')

    async def get_file_url(self, file_id: str) -> str:
        raise NotSupportedError('get_file_url')

    async def kick_group_member(self, group_id: typing.Union[int, str], user_id: typing.Union[int, str]) -> None:
        raise NotSupportedError('kick_group_member')

    async def mute_group_member(
        self,
        group_id: typing.Union[int, str],
        user_id: typing.Union[int, str],
        duration: int = 0,
    ) -> None:
        raise NotSupportedError('mute_group_member')

    async def unmute_group_member(self, group_id: typing.Union[int, str], user_id: typing.Union[int, str]) -> None:
        raise NotSupportedError('unmute_group_member')

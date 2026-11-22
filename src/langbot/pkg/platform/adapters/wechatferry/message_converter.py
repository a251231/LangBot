from __future__ import annotations

import datetime
import re
import typing

import langbot_plugin.api.definition.abstract.platform.adapter as abstract_platform_adapter
from langbot_plugin.api.entities.builtin.platform import message as platform_message

_IMG_B64_RE = re.compile(r'<img_b64>(.*?)</img_b64>', re.DOTALL)


def _message_datetime(raw_ts: typing.Any) -> datetime.datetime:
    try:
        ts = float(raw_ts)
        if ts > 1e12:
            ts /= 1000.0
        if ts > 0:
            return datetime.datetime.fromtimestamp(ts)
    except (TypeError, ValueError, OSError):
        pass
    return datetime.datetime.now()


def _is_windows_drive_path(path: str) -> bool:
    """Detect Windows drive-letter paths (e.g. C:\\...) unusable on remote Linux hosts."""
    return len(path) >= 2 and path[1] == ':' and path[0].isalpha()


class WeChatFerryMessageConverter(abstract_platform_adapter.AbstractMessageConverter):
    """Convert between LangBot MessageChain and WeChatFerry / Helper V4-V7 payloads."""

    @staticmethod
    async def yiri2target(message_chain: platform_message.MessageChain) -> list[dict[str, typing.Any]]:
        """Convert MessageChain into an ordered list of gateway outbound operations.

        Adjacent text/mention segments are coalesced into one ``text`` operation while
        media segments (``image``, ``file``, ``voice``) preserve their relative order.
        """
        operations: list[dict[str, typing.Any]] = []
        text_parts: list[str] = []
        ats: list[str] = []

        def _flush_text() -> None:
            if text_parts or ats:
                operations.append(
                    {
                        'kind': 'text',
                        'text': ''.join(text_parts),
                        'ats': list(ats),
                    }
                )
                text_parts.clear()
                ats.clear()

        for component in message_chain:
            if isinstance(component, platform_message.Source):
                continue
            elif isinstance(component, platform_message.AtAll):
                ats.append('notify@all')
            elif isinstance(component, platform_message.At):
                if component.target:
                    ats.append(str(component.target))
            elif isinstance(component, platform_message.Plain):
                if component.text:
                    text_parts.append(str(component.text))
            elif isinstance(component, platform_message.Face):
                face_text = getattr(component, 'face_name', None) or f'[face:{getattr(component, "face_id", "")}]'
                text_parts.append(str(face_text))
            elif isinstance(component, platform_message.Quote):
                origin_text = ''
                origin = getattr(component, 'origin', None)
                if origin:
                    origin_text = ''.join(
                        str(c.text) for c in origin if isinstance(c, platform_message.Plain) and c.text
                    )
                if origin_text:
                    text_parts.append(f'> {origin_text}\n')
            elif isinstance(component, platform_message.Forward):
                node_list = getattr(component, 'node_list', None) or []
                lines: list[str] = []
                for node in node_list:
                    sender = getattr(node, 'sender_name', None) or getattr(node, 'sender_id', '') or 'user'
                    node_chain = getattr(node, 'message_chain', None) or []
                    body = ''.join(str(c.text) for c in node_chain if isinstance(c, platform_message.Plain) and c.text)
                    if body:
                        lines.append(f'{sender}: {body}')
                if lines:
                    text_parts.append('\n'.join(lines))
            elif isinstance(component, platform_message.Image):
                img_payload: dict[str, str] = {}
                if component.url:
                    img_payload['url'] = str(component.url)
                elif component.base64:
                    img_payload['base64'] = str(component.base64)
                elif component.path:
                    img_payload['path'] = str(component.path)
                if img_payload:
                    _flush_text()
                    operations.append({'kind': 'image', **img_payload})
            elif isinstance(component, platform_message.File):
                file_payload: dict[str, str] = {}
                if getattr(component, 'url', None):
                    file_payload['url'] = str(component.url)
                if getattr(component, 'path', None):
                    file_payload['path'] = str(component.path)
                if getattr(component, 'base64', None):
                    file_payload['base64'] = str(component.base64)
                if getattr(component, 'name', None):
                    file_payload['filename'] = str(component.name)
                if file_payload:
                    _flush_text()
                    operations.append({'kind': 'file', **file_payload})
            elif isinstance(component, platform_message.Voice):
                voice_payload: dict[str, str] = {}
                if getattr(component, 'url', None):
                    voice_payload['url'] = str(component.url)
                if getattr(component, 'path', None):
                    voice_payload['path'] = str(component.path)
                if getattr(component, 'base64', None):
                    voice_payload['base64'] = str(component.base64)
                if voice_payload:
                    _flush_text()
                    operations.append({'kind': 'voice', **voice_payload})
            elif isinstance(component, platform_message.Unknown):
                if getattr(component, 'text', None):
                    text_parts.append(str(component.text))

        _flush_text()
        return operations

    @staticmethod
    async def target2yiri(
        data: dict[str, typing.Any],
        bot_account_id: str = '',
    ) -> platform_message.MessageChain:
        """Convert a WeChatFerry / Helper V4-V7 inbound message dict into MessageChain."""
        components: list[platform_message.MessageComponent] = []

        msg_id = data.get('id') or data.get('msg_id')
        if msg_id is not None and str(msg_id):
            components.append(
                platform_message.Source(
                    id=str(msg_id),
                    time=_message_datetime(data.get('ts')),
                )
            )

        content = str(data.get('content') or '')
        is_at = bool(data.get('is_at', False))
        msg_type = data.get('type', 1)

        if is_at and bot_account_id:
            components.append(platform_message.At(target=bot_account_id))

        if '@所有人' in content:
            components.append(platform_message.AtAll())

        quote_data = data.get('quote') or data.get('refer')
        if isinstance(quote_data, dict):
            quote_id = str(quote_data.get('id') or quote_data.get('msg_id') or '')
            quote_sender = str(quote_data.get('sender') or '')
            quote_text = str(quote_data.get('content') or '')
            origin_chain = [platform_message.Plain(text=quote_text)] if quote_text else []
            components.append(
                platform_message.Quote(
                    id=quote_id,
                    sender_id=quote_sender,
                    origin=origin_chain,
                )
            )

        if msg_type == 1:
            if content:
                components.append(platform_message.Plain(text=content))
        elif msg_type == 3:
            b64_match = _IMG_B64_RE.search(content)
            img_b64 = (b64_match.group(1).strip() if b64_match else '') or str(
                data.get('img_b64') or data.get('base64') or ''
            ).strip()
            img_url = str(data.get('image_url') or data.get('url') or '').strip()
            img_path = str(data.get('extra') or data.get('image_path') or '').strip()
            if img_b64:
                if not img_b64.startswith('data:'):
                    img_b64 = f'data:image/jpeg;base64,{img_b64}'
                components.append(platform_message.Image(base64=img_b64))
            elif img_url.startswith(('http://', 'https://')):
                components.append(platform_message.Image(url=img_url))
            elif img_path and not _is_windows_drive_path(img_path):
                components.append(platform_message.Image(path=img_path))
            else:
                components.append(platform_message.Plain(text='[图片]'))
        elif msg_type == 34:
            voice_b64 = str(data.get('voice_b64') or data.get('base64') or '').strip()
            voice_url = str(data.get('voice_url') or data.get('url') or '').strip()
            voice_path = str(data.get('voice_path') or data.get('extra') or '').strip()
            voice_len = int(data.get('voice_length') or data.get('length') or 0)
            if voice_b64:
                components.append(platform_message.Voice(base64=voice_b64, length=voice_len))
            elif voice_url.startswith(('http://', 'https://')):
                components.append(platform_message.Voice(url=voice_url, length=voice_len))
            elif voice_path and not _is_windows_drive_path(voice_path):
                components.append(platform_message.Voice(path=voice_path, length=voice_len))
            else:
                components.append(platform_message.Unknown(text='[语音]'))
        elif msg_type == 47:
            img_url = str(data.get('url') or data.get('cdnurl') or '').strip()
            if img_url.startswith(('http://', 'https://')):
                components.append(platform_message.Image(url=img_url))
            else:
                components.append(platform_message.Unknown(text='[表情]'))
        elif msg_type == 49:
            file_name = str(data.get('file_name') or data.get('filename') or '').strip()
            file_url = str(data.get('file_url') or data.get('url') or '').strip()
            file_size = int(data.get('file_size') or data.get('size') or 0)
            if file_name or file_url:
                components.append(
                    platform_message.File(
                        id=str(msg_id or ''),
                        name=file_name or 'file',
                        url=file_url or None,
                        size=file_size,
                    )
                )
            elif content:
                components.append(platform_message.Plain(text=content))
            else:
                components.append(platform_message.Unknown(text='[应用消息]'))
        else:
            if content:
                components.append(platform_message.Plain(text=content))
            else:
                components.append(platform_message.Unknown(text=f'[未知类型: {msg_type}]'))

        return platform_message.MessageChain(components)

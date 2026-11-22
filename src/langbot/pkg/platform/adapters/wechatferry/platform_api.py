from __future__ import annotations

import typing


async def get_status(adapter, params: dict) -> dict:
    """Return gateway login status, WeChat version, and capabilities."""
    data = await adapter._http_get('/api/status')
    return data if isinstance(data, dict) else {'data': data}


async def get_friend_requests(adapter, params: dict) -> dict:
    """List pending WeChat friend requests exposed by the Helper gateway."""
    data = await adapter._http_get('/api/friend_requests')
    return data if isinstance(data, dict) else {'requests': data}


async def accept_friend(adapter, params: dict) -> dict:
    """Accept a pending friend request by request_id."""
    request_id = str(params.get('request_id') or '').strip()
    if not request_id:
        raise ValueError('accept_friend requires request_id')
    data = await adapter._http_post('/api/accept_friend', {'request_id': request_id})
    return data if isinstance(data, dict) else {'result': data}


async def send_rich_text(adapter, params: dict) -> dict:
    """Send a WeChat link/card rich-text message via /api/send_rich_text."""
    receiver = str(params.get('receiver') or '').strip()
    title = str(params.get('title') or '').strip()
    url = str(params.get('url') or '').strip()
    if not receiver or not title or not url:
        raise ValueError('send_rich_text requires receiver, title, and url')
    payload = {
        'receiver': receiver,
        'name': str(params.get('name') or ''),
        'account': str(params.get('account') or ''),
        'title': title,
        'digest': str(params.get('digest') or ''),
        'url': url,
        'thumburl': str(params.get('thumburl') or ''),
    }
    data = await adapter._http_post('/api/send_rich_text', payload)
    return data if isinstance(data, dict) else {'result': data}


PLATFORM_API_MAP: dict[str, typing.Callable[[typing.Any, dict], typing.Awaitable[dict]]] = {
    'get_status': get_status,
    'get_friend_requests': get_friend_requests,
    'accept_friend': accept_friend,
    'send_rich_text': send_rich_text,
}

from __future__ import annotations

import typing
from collections import deque

ADAPTER_NAME = 'wechatferry'

DEFAULT_API_URL = 'http://127.0.0.1:18080'
DEFAULT_WS_URL = 'ws://127.0.0.1:18080/ws'
DEFAULT_WS_MAX_SIZE_MB = 64
MIN_WS_MAX_SIZE_MB = 16

SYSTEM_SENDERS = frozenset({'weixin', 'newsapp', 'fmessage', 'medianote', 'qmessage'})


class WeChatFerryMessageDeduplicator:
    """In-memory bounded FIFO deduplicator for incoming WeChatFerry message IDs."""

    def __init__(self, max_size: int = 4096) -> None:
        self.max_size = max_size
        self._seen_ids: set[str] = set()
        self._order: deque[str] = deque()

    def is_duplicate(self, msg_id: typing.Any) -> bool:
        if not msg_id:
            return False
        key = str(msg_id)
        if key in self._seen_ids:
            return True
        if len(self._order) >= self.max_size:
            oldest = self._order.popleft()
            self._seen_ids.discard(oldest)
        self._seen_ids.add(key)
        self._order.append(key)
        return False

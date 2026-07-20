from langbot.pkg.platform.sources.wechatpad_message_guard import (
    WeChatPadMessageDeduplicator,
    is_wechatpad_message,
)


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

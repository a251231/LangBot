from __future__ import annotations

import pytest

from components.command_parser import (
    CurlParseError,
    ParsedCommand,
    parse_binding_curl,
    parse_binding_input,
    parse_keyword,
)


BASE_CURL = r"""
curl -X GET 'https://vwdservice.roguelike.com/v2/api/wd_app/outer/user_signin/list' \
-H 'Token: TOKEN_TEST_1234567890' \
-H 'DEVICE: 2211133C/Android/16' \
-H 'Version: 2.26.1' \
-H 'versionCode: 260604' \
-H 'guestId: 1030000000000000' \
-H 'clientType: wd_android'
"""

LOGIN_RESPONSE = """{
  "code": 2000,
  "data": {
    "roleId": 12345678,
    "ltUid": "311900000000000000",
    "token": {
      "accessToken": "TOKEN_LOGIN_TEST_1234567890",
      "accessTokenValidTime": 1784219114121,
      "refreshToken": "REFRESH_LOGIN_TEST_1234567890"
    },
    "bindServer": {
      "name": "测试角色",
      "serverName": "测试区服"
    }
  },
  "timestamp": 1784215516171
}"""


def test_parse_binding_curl_extracts_required_credentials() -> None:
    credentials = parse_binding_curl(BASE_CURL)

    assert credentials.token == 'TOKEN_TEST_1234567890'
    assert credentials.device == '2211133C/Android/16'
    assert credentials.version == '2.26.1'
    assert credentials.version_code == '260604'
    assert credentials.guest_id == '1030000000000000'
    assert credentials.client_type == 'wd_android'


@pytest.mark.parametrize('continuation', ['\\', '^', '`'])
def test_parse_binding_curl_accepts_common_line_continuations(continuation: str) -> None:
    raw = BASE_CURL.replace('\\\n', continuation + '\n')

    assert parse_binding_curl(raw).token == 'TOKEN_TEST_1234567890'


def test_parse_binding_curl_rejects_untrusted_host() -> None:
    raw = BASE_CURL.replace('vwdservice.roguelike.com', 'example.test')

    with pytest.raises(CurlParseError, match='域名'):
        parse_binding_curl(raw)


def test_parse_binding_curl_lists_missing_headers_without_echoing_token() -> None:
    raw = BASE_CURL.replace("-H 'guestId: 1030000000000000'", '')

    with pytest.raises(CurlParseError, match='guestid') as exc_info:
        parse_binding_curl(raw)

    assert 'TOKEN_TEST_1234567890' not in str(exc_info.value)


@pytest.mark.parametrize(
    "raw",
    [
        LOGIN_RESPONSE,
        "HTTP/2 200\r\ncontent-type: application/json; charset=utf-8\r\n\r\n"
        + LOGIN_RESPONSE,
    ],
)
def test_parse_binding_input_accepts_login_response_with_fixed_device_headers(raw: str) -> None:
    binding = parse_binding_input(raw)

    assert binding.credentials.token == "TOKEN_LOGIN_TEST_1234567890"
    assert binding.credentials.refresh_token == "REFRESH_LOGIN_TEST_1234567890"
    assert binding.credentials.access_token_valid_time == 1784219114121
    assert binding.credentials.device == "2211133C/Android/16"
    assert binding.credentials.version == "2.26.1"
    assert binding.credentials.version_code == "260604"
    assert binding.credentials.guest_id == "1030620167932793"
    assert binding.credentials.client_type == "wd_android"
    assert binding.nickname == "测试角色"
    assert binding.user_identifier == "311900000000000000"


def test_parse_binding_input_keeps_legacy_curl_compatible() -> None:
    binding = parse_binding_input(BASE_CURL)

    assert binding.credentials == parse_binding_curl(BASE_CURL)
    assert binding.nickname == ""
    assert binding.user_identifier == ""


def test_parse_binding_input_rejects_incomplete_login_response_without_echoing_token() -> None:
    raw = LOGIN_RESPONSE.replace(
        ',\n      "refreshToken": "REFRESH_LOGIN_TEST_1234567890"',
        '',
    )

    with pytest.raises(ValueError, match="refreshToken") as exc_info:
        parse_binding_input(raw)

    assert "TOKEN_LOGIN_TEST_1234567890" not in str(exc_info.value)


@pytest.mark.parametrize(
    ('text', 'kind', 'argument'),
    [
        ('问道查询', 'query', ''),
        ('问道登录 13800138000', 'login_start', '13800138000'),
        (
            '问道验证 @TEST TICKET_TEST_*',
            'login_captcha',
            '@TEST TICKET_TEST_*',
        ),
        ('问道验证码 873157', 'login_code', '873157'),
        ('问道签到', 'signin', ''),
        ('问道补签', 'resign', ''),
        ('问道时间 09:30', 'schedule_time', '09:30'),
        ('问道自动 开', 'auto_signin', '开'),
        ('问道自动补签 关', 'auto_resign', '关'),
        ('问道自动里程碑 开', 'auto_milestone', '开'),
        ('问道自动周报 关', 'auto_weekly_report', '关'),
        ('问道周报', 'weekly_report', ''),
        ('问道设置', 'settings', ''),
        ('问道推广', 'promotion', ''),
        ('问道邀请 ABCD2345', 'referral_bind', 'ABCD2345'),
        ('问道积分', 'points', ''),
        ('问道商城', 'shop', ''),
        ('问道兑换 P000001', 'redeem', 'P000001'),
        ('问道兑换记录', 'redemptions', ''),
        ('问道激活 WD-TEST', 'activate', 'WD-TEST'),
        ('问道权益', 'entitlement', ''),
        ('问道管理 商品列表', 'admin', '商品列表'),
        ('问道解绑', 'unbind', ''),
        ('问道帮助', 'help', ''),
    ],
)
def test_parse_keyword_matches_exact_commands(text: str, kind: str, argument: str) -> None:
    command = parse_keyword(text)

    assert command is not None
    assert command.kind == kind
    assert command.argument == argument


def test_parse_keyword_uses_longest_auto_command_match() -> None:
    command = parse_keyword('问道自动补签 开')

    assert command is not None
    assert command.kind == 'auto_resign'


def test_parse_keyword_does_not_treat_auto_weekly_report_as_auto_signin() -> None:
    command = parse_keyword('问道自动周报 开')

    assert command is not None
    assert command.kind == 'auto_weekly_report'


def test_parse_keyword_uses_longest_redeem_command_match() -> None:
    command = parse_keyword('问道兑换记录')

    assert command is not None
    assert command.kind == 'redemptions'
    assert command.argument == ''


def test_parse_keyword_preserves_multiline_curl_after_bind_prefix() -> None:
    command = parse_keyword('问道绑定\n' + BASE_CURL)

    assert command is not None
    assert command.kind == 'bind'
    assert command.argument.startswith('curl -X GET')


def test_parse_keyword_distinguishes_login_verification_commands() -> None:
    captcha = parse_keyword('问道验证 @RAND TICKET_VALUE_*')
    sms_code = parse_keyword('问道验证码 123456')

    assert captcha == ParsedCommand('login_captcha', '@RAND TICKET_VALUE_*')
    assert sms_code == ParsedCommand('login_code', '123456')


@pytest.mark.parametrize(
    ('text', 'operation'),
    (
        ('群聊回复 开始', 'on'),
        ('群聊回复 开', 'on'),
        ('群聊回复 开启', 'on'),
        ('群聊回复 关闭', 'off'),
        ('群聊回复 关', 'off'),
        ('群聊回复 状态', 'status'),
    ),
)
def test_parse_keyword_matches_standalone_group_reply_control(
    text: str,
    operation: str,
) -> None:
    assert parse_keyword(text) == ParsedCommand('group_reply_control', operation)


@pytest.mark.parametrize(
    'text',
    (
        '群聊回复',
        '群聊回复 暂停',
        '群聊回复 开始 现在',
        '请群聊回复 开始',
        '群聊回复开始',
    ),
)
def test_parse_keyword_rejects_invalid_group_reply_control(text: str) -> None:
    assert parse_keyword(text) is None


def test_parse_keyword_ignores_unrelated_messages() -> None:
    assert parse_keyword('今天问道游戏好玩吗') is None

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from urllib.parse import urlparse

from components.models import Credentials


ALLOWED_HOST = 'vwdservice.roguelike.com'
DEFAULT_DEVICE = '2211133C/Android/16'
DEFAULT_VERSION = '2.26.1'
DEFAULT_VERSION_CODE = '260604'
DEFAULT_GUEST_ID = '1030620167932793'
DEFAULT_CLIENT_TYPE = 'wd_android'
_CONTINUATION_RE = re.compile(r'[\\^`]\s*\r?\n')


class BindingParseError(ValueError):
    pass


class CurlParseError(BindingParseError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    kind: str
    argument: str = ''


@dataclass(frozen=True, slots=True)
class BindingInput:
    credentials: Credentials
    nickname: str = ''
    user_identifier: str = ''


def _tokenize_curl(raw: str) -> list[str]:
    normalized = _CONTINUATION_RE.sub(' ', raw.strip())
    try:
        return shlex.split(normalized, posix=True)
    except ValueError as exc:
        raise CurlParseError('curl 格式不完整，请检查引号。') from exc


def parse_binding_curl(raw: str) -> Credentials:
    tokens = _tokenize_curl(raw)
    if not tokens or tokens[0].lower() not in {'curl', 'curl.exe'}:
        raise CurlParseError('绑定内容必须是完整 curl 命令。')

    urls = [token for token in tokens[1:] if token.lower().startswith(('http://', 'https://'))]
    if not urls:
        raise CurlParseError('curl 中缺少请求 URL。')
    parsed_url = urlparse(urls[0])
    if parsed_url.scheme.lower() != 'https' or (parsed_url.hostname or '').lower() != ALLOWED_HOST:
        raise CurlParseError(f'只接受 {ALLOWED_HOST} 域名的 HTTPS 请求。')

    headers: dict[str, str] = {}
    index = 1
    while index < len(tokens):
        token = tokens[index]
        header = ''
        if token in {'-H', '--header'} and index + 1 < len(tokens):
            index += 1
            header = tokens[index]
        elif token.startswith('-H') and len(token) > 2:
            header = token[2:]
        if ':' in header:
            name, value = header.split(':', 1)
            headers[name.strip().lower()] = value.strip()
        index += 1

    required = ('token', 'device', 'version', 'versioncode', 'guestid', 'clienttype')
    missing = [name for name in required if not headers.get(name)]
    if missing:
        raise CurlParseError('curl 缺少请求头：' + ', '.join(missing))

    return Credentials(
        token=headers['token'],
        device=headers['device'],
        version=headers['version'],
        version_code=headers['versioncode'],
        guest_id=headers['guestid'],
        client_type=headers['clienttype'],
    )


def _parse_login_response(raw: str) -> BindingInput:
    start = raw.find('{')
    if start < 0:
        raise BindingParseError('绑定内容需要包含完整登录响应 JSON。')
    try:
        payload, _ = json.JSONDecoder().raw_decode(raw[start:])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BindingParseError('登录响应 JSON 格式不完整。') from exc
    if not isinstance(payload, dict):
        raise BindingParseError('登录响应必须是 JSON 对象。')
    try:
        code = int(payload.get('code', 0))
    except (TypeError, ValueError):
        code = 0
    if code != 2000:
        raise BindingParseError(f'登录响应业务码不是 2000（当前为 {code}）。')

    data = payload.get('data')
    data = data if isinstance(data, dict) else {}
    token_data = data.get('token')
    token_data = token_data if isinstance(token_data, dict) else {}
    access_token = str(token_data.get('accessToken') or '').strip()
    refresh_token = str(token_data.get('refreshToken') or '').strip()
    try:
        valid_time = int(token_data.get('accessTokenValidTime') or 0)
    except (TypeError, ValueError):
        valid_time = 0

    missing: list[str] = []
    if not access_token:
        missing.append('accessToken')
    if not refresh_token:
        missing.append('refreshToken')
    if valid_time <= 0:
        missing.append('accessTokenValidTime')
    if missing:
        raise BindingParseError('登录响应缺少字段：' + ', '.join(missing))

    bind_server = data.get('bindServer')
    bind_server = bind_server if isinstance(bind_server, dict) else {}
    return BindingInput(
        credentials=Credentials(
            token=access_token,
            device=DEFAULT_DEVICE,
            version=DEFAULT_VERSION,
            version_code=DEFAULT_VERSION_CODE,
            guest_id=DEFAULT_GUEST_ID,
            client_type=DEFAULT_CLIENT_TYPE,
            refresh_token=refresh_token,
            access_token_valid_time=valid_time,
        ),
        nickname=str(bind_server.get('name') or '').strip(),
        user_identifier=str(data.get('ltUid') or '').strip(),
    )


def parse_binding_input(raw: str) -> BindingInput:
    text = raw.strip()
    if text.lower().startswith(('curl ', 'curl.exe ')):
        return BindingInput(credentials=parse_binding_curl(text))
    return _parse_login_response(text)


def _prefixed_argument(text: str, prefix: str) -> str | None:
    if text == prefix:
        return ''
    if text.startswith(prefix) and len(text) > len(prefix) and text[len(prefix)].isspace():
        return text[len(prefix) :].strip()
    return None


def parse_keyword(raw: str) -> ParsedCommand | None:
    text = raw.strip()
    bind_argument = _prefixed_argument(text, '问道绑定')
    if bind_argument is not None:
        return ParsedCommand('bind', bind_argument)

    parameter_commands = (
        ('问道自动里程碑', 'auto_milestone'),
        ('问道自动周报', 'auto_weekly_report'),
        ('问道自动补签', 'auto_resign'),
        ('问道管理', 'admin'),
        ('问道验证码', 'login_code'),
        ('问道验证', 'login_captcha'),
        ('问道登录', 'login_start'),
        ('问道邀请', 'referral_bind'),
        ('问道兑换', 'redeem'),
        ('问道激活', 'activate'),
        ('问道自动', 'auto_signin'),
        ('问道时间', 'schedule_time'),
    )
    for prefix, kind in parameter_commands:
        argument = _prefixed_argument(text, prefix)
        if argument is not None:
            return ParsedCommand(kind, argument)

    exact_commands = {
        '问道查询': 'query',
        '问道签到': 'signin',
        '问道补签': 'resign',
        '问道周报': 'weekly_report',
        '问道设置': 'settings',
        '问道推广': 'promotion',
        '问道积分': 'points',
        '问道商城': 'shop',
        '问道兑换记录': 'redemptions',
        '问道权益': 'entitlement',
        '问道解绑': 'unbind',
        '问道帮助': 'help',
    }
    kind = exact_commands.get(text)
    return ParsedCommand(kind) if kind else None

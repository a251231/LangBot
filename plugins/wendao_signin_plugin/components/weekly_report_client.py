from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from components.api_client import WendaoBusinessError, WendaoRetryableError


ACTIVITY_BASE_URL = 'https://actscpapi01.leiting.com'
ACTIVITY_API_PREFIX = '/php/ltproject/public/index.php/api/wd/p202404xd'
ACTIVITY_USER_DATA_PATH = f'{ACTIVITY_API_PREFIX}/userData'
ACTIVITY_PAGE_STATUS_PATH = f'{ACTIVITY_API_PREFIX}/page_status'
ACTIVITY_PATH = f'{ACTIVITY_API_PREFIX}/index'
ACTIVITY_PAGE_HOST = 'actscp01.leiting.com'
ACTIVITY_PAGE_PATH = '/wd/act/202405/hand/'
ACTIVITY_SUCCESS_CODE = 1
ACTIVITY_USER_DATA_FIELDS = (
    'sid',
    'rid',
    'role_name',
    'zone_id',
    'zone_name',
    'channel_no',
    'polar',
    'gender',
    'identify',
    'is_sub',
    'token',
)


@dataclass(frozen=True, slots=True)
class WeeklyReportTicket:
    sid: str
    rid: str
    token: str
    user_data_items: tuple[tuple[str, str], ...]

    @property
    def user_data_params(self) -> dict[str, str]:
        return dict(self.user_data_items)

    @property
    def form_data(self) -> dict[str, str]:
        return {
            'sid': self.sid,
            'rid': self.rid,
            'token': self.token,
        }


class WeeklyReportTokenError(WendaoBusinessError):
    pass


class WeeklyReportNotReadyError(WendaoRetryableError):
    pass


def parse_weekly_report_ticket(action_url: str) -> WeeklyReportTicket:
    parsed = urlsplit(str(action_url).strip())
    if (
        parsed.scheme.lower() != 'https'
        or (parsed.hostname or '').lower() != ACTIVITY_PAGE_HOST
        or parsed.path != ACTIVITY_PAGE_PATH
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise WendaoBusinessError('问道周报地址格式错误。', retryable=False)

    values = parse_qs(parsed.query, keep_blank_values=True)
    required: dict[str, str] = {}
    for name in ACTIVITY_USER_DATA_FIELDS:
        items = values.get(name, [])
        if len(items) != 1 or not items[0].strip():
            raise WendaoBusinessError('问道周报地址缺少必要凭据。', retryable=False)
        required[name] = items[0].strip()
    return WeeklyReportTicket(
        sid=required['sid'],
        rid=required['rid'],
        token=required['token'],
        user_data_items=tuple(
            (name, required[name]) for name in ACTIVITY_USER_DATA_FIELDS
        ),
    )


class WeeklyReportClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=ACTIVITY_BASE_URL,
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=transport,
            headers={
                'accept': 'application/json, text/plain, */*',
                'origin': f'https://{ACTIVITY_PAGE_HOST}',
                'referer': f'https://{ACTIVITY_PAGE_HOST}/',
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        ticket: WeeklyReportTicket,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                data=data,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            raise WendaoRetryableError(
                '问道周报网络请求失败。',
                retryable=True,
            ) from None

        if response.status_code >= 500:
            raise WendaoRetryableError(
                f'问道周报服务暂时不可用（HTTP {response.status_code}）。',
                retryable=True,
            )
        if response.status_code != 200:
            raise WendaoBusinessError(
                f'问道周报服务返回 HTTP {response.status_code}。',
                retryable=False,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise WendaoBusinessError(
                '问道周报服务返回了无效 JSON。',
                retryable=False,
            ) from exc
        if not isinstance(payload, dict):
            raise WendaoBusinessError('问道周报响应格式错误。', retryable=False)

        try:
            code = int(payload.get('code', 0))
        except (TypeError, ValueError):
            code = 0
        message = str(payload.get('msg') or payload.get('message') or '').strip()
        redacted_message = message.replace(ticket.token, '[已脱敏]')
        if code == 0:
            suffix = f'：{redacted_message}' if redacted_message else ''
            raise WeeklyReportTokenError(
                f'问道周报活动凭据校验失败{suffix}',
                retryable=False,
                code=code,
            )
        if code != ACTIVITY_SUCCESS_CODE:
            suffix = f'：{redacted_message}' if redacted_message else ''
            raise WendaoBusinessError(
                f'问道周报业务错误 {code}{suffix}',
                retryable=False,
                code=code,
            )
        return payload

    async def fetch(self, ticket: WeeklyReportTicket) -> dict[str, Any]:
        await self._request(
            'GET',
            ACTIVITY_USER_DATA_PATH,
            ticket,
            params=ticket.user_data_params,
        )
        status_payload = await self._request(
            'POST',
            ACTIVITY_PAGE_STATUS_PATH,
            ticket,
            data=ticket.form_data,
        )
        status_data = status_payload.get('data')
        if not isinstance(status_data, dict):
            raise WendaoBusinessError('问道周报页面状态格式错误。', retryable=False)
        try:
            page_state = int(status_data.get('data', 0))
        except (TypeError, ValueError):
            page_state = 0
        if page_state == 1:
            raise WeeklyReportNotReadyError(
                '问道周报正在生成中。',
                retryable=True,
            )
        if page_state == 2:
            raise WendaoBusinessError(
                '问道周报上周暂无可展示数据。',
                retryable=False,
            )
        if page_state != 3:
            raise WendaoBusinessError(
                f'问道周报页面状态异常（状态 {page_state}）。',
                retryable=False,
            )

        payload = await self._request(
            'POST',
            ACTIVITY_PATH,
            ticket,
            data=ticket.form_data,
        )
        data = payload.get('data')
        if not isinstance(data, dict):
            raise WendaoBusinessError('问道周报响应格式错误。', retryable=False)
        return data

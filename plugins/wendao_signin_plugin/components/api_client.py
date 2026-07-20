from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx

from components.models import ApiResponse, Credentials


BASE_URL = 'https://vwdservice.roguelike.com'
LIST_PATH = '/v2/api/wd_app/outer/user_signin/list'
SIGNIN_PATH = '/v2/api/wd_app/outer/user_signin/signin'
MILESTONE_PATH = '/v2/api/wd_app/outer/user_signin/get_milestone_reward'
READ_REPORT_PATH = '/v2/api/wd_content/post/read_report/{source_id}'
THIRD_URL_PATH = '/v2/api/wd_content/home/get_third_url'
REFRESH_URL = 'https://wdappapi.roguelike.com/api/app/account/token/refresh'
WEEKLY_REPORT_ACTION_URL = 'https://actscp01.leiting.com/wd/act/202405/hand/'
WEEKLY_REPORT_IDENTIFY = 'xdzb'
SUCCESS_CODE = 2000


def calculate_sign(device: str, timestamp: str | int) -> str:
    source = f'{device}{timestamp}'.encode('utf-8')
    return hashlib.md5(source).hexdigest()


class WendaoApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        code: int | None = None,
        data: dict[str, Any] | None = None,
        credentials_fingerprint: str = '',
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.data = data or {}
        self.credentials_fingerprint = credentials_fingerprint


class WendaoAuthError(WendaoApiError):
    pass


class WendaoRetryableError(WendaoApiError):
    pass


class WendaoBusinessError(WendaoApiError):
    pass


class WendaoApiClient:
    def __init__(
        self,
        credentials: Credentials,
        *,
        timeout_seconds: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.credentials = credentials
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_signin(self) -> ApiResponse:
        return await self._request('GET', LIST_PATH)

    async def signin(self, signin_type: int) -> ApiResponse:
        if signin_type not in {1, 2}:
            raise ValueError('签到类型仅支持 1 或 2。')
        return await self._request('POST', SIGNIN_PATH, json_body={'type': signin_type})

    async def claim_milestone(self) -> ApiResponse:
        return await self._request('POST', MILESTONE_PATH)

    async def report_post_read(self, source_id: str) -> ApiResponse:
        normalized = str(source_id).strip()
        if not normalized.isascii() or not normalized.isdigit():
            raise ValueError('文章 ID 格式错误。')
        path = READ_REPORT_PATH.format(source_id=normalized)
        return await self._request(
            'GET',
            path,
            query_params={'postType': 1},
        )

    async def get_third_url(self) -> ApiResponse:
        return await self._request(
            'POST',
            THIRD_URL_PATH,
            json_body={
                'actionUrl': WEEKLY_REPORT_ACTION_URL,
                'identify': WEEKLY_REPORT_IDENTIFY,
            },
        )

    async def refresh_access_token(self) -> Credentials:
        credentials = self.credentials
        if not credentials.refresh_token:
            raise WendaoAuthError(
                '问道刷新凭据缺失，请重新绑定。',
                retryable=False,
            )

        response = await self._request(
            'PUT',
            REFRESH_URL,
            json_body={'refreshToken': credentials.refresh_token},
        )
        access_token = response.data.get('accessToken')
        valid_time = response.data.get('accessTokenValidTime')
        refresh_token = response.data.get('refreshToken')
        if (
            not isinstance(access_token, str)
            or not access_token.strip()
            or not isinstance(valid_time, int)
            or isinstance(valid_time, bool)
            or valid_time <= 0
            or not isinstance(refresh_token, str)
            or not refresh_token.strip()
        ):
            raise WendaoBusinessError(
                '问道令牌刷新响应格式错误。',
                retryable=False,
            )

        self.credentials = replace(
            credentials,
            token=access_token,
            access_token_valid_time=valid_time,
            refresh_token=refresh_token,
        )
        return self.credentials

    def _headers(self) -> dict[str, str]:
        timestamp = str(self._clock_ms())
        credentials = self.credentials
        return {
            'token': credentials.token,
            'timestamp': timestamp,
            'device': credentials.device,
            'version': credentials.version,
            'versioncode': credentials.version_code,
            'guestid': credentials.guest_id,
            'clienttype': credentials.client_type,
            'sign': calculate_sign(credentials.device, timestamp),
            'accept-encoding': 'gzip',
            'user-agent': 'okhttp/4.4.1',
        }

    def _redact(self, message: str) -> str:
        redacted = message
        tokens = {
            self.credentials.token,
            self.credentials.refresh_token,
        }
        for token in sorted(tokens, key=len, reverse=True):
            if token:
                redacted = redacted.replace(token, '[已脱敏]')
        return redacted

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        query_params: dict[str, str | int] | None = None,
    ) -> ApiResponse:
        try:
            headers = self._headers()
            if json_body is not None:
                headers['content-type'] = 'application/json; charset=UTF-8'
            response = await self._client.request(
                method,
                path,
                headers=headers,
                json=json_body,
                params=query_params,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            raise WendaoRetryableError(
                '问道接口网络请求失败。',
                retryable=True,
            ) from None

        if response.status_code in {401, 403}:
            raise WendaoAuthError(
                f'问道账号凭据已失效（HTTP {response.status_code}）。',
                retryable=False,
            )
        if response.status_code >= 500:
            raise WendaoRetryableError(
                f'问道接口暂时不可用（HTTP {response.status_code}）。',
                retryable=True,
            )
        if response.status_code != 200:
            raise WendaoBusinessError(
                f'问道接口返回 HTTP {response.status_code}。',
                retryable=False,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise WendaoBusinessError('问道接口返回了无效 JSON。', retryable=False) from exc
        if not isinstance(payload, dict):
            raise WendaoBusinessError('问道接口响应格式错误。', retryable=False)

        try:
            code = int(payload.get('code', 0))
        except (TypeError, ValueError):
            code = 0
        data = payload.get('data')
        data = data if isinstance(data, dict) else {}
        message = str(payload.get('msg') or payload.get('message') or '').strip()
        if code != SUCCESS_CODE:
            detail = self._redact(message)
            suffix = f'：{detail}' if detail else ''
            raise WendaoBusinessError(
                f'问道接口业务错误 {code}{suffix}',
                retryable=False,
                code=code,
                data=data,
            )

        timestamp_raw = payload.get('timestamp')
        try:
            timestamp = int(timestamp_raw) if timestamp_raw is not None else None
        except (TypeError, ValueError):
            timestamp = None
        return ApiResponse(code=code, data=data, message=message, timestamp=timestamp)

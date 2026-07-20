from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
import json
import re
import secrets
import time
from typing import Protocol
import uuid
import weakref
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
import httpx

from components.api_client import (
    BASE_URL,
    SUCCESS_CODE,
    WendaoBusinessError,
    WendaoRetryableError,
    calculate_sign,
)
from components.command_parser import (
    DEFAULT_CLIENT_TYPE,
    DEFAULT_DEVICE,
    DEFAULT_VERSION,
    DEFAULT_VERSION_CODE,
    BindingInput,
)
from components.models import Credentials


SMS_PATH = '/v2/api/app/sms/leiting/send'
PHONE_LOGIN_PATH = '/v2/api/app/account/login/phone_code'
CAPTCHA_CONFIG_PATH = '/v2/api/app/account/login/switch'
AES_KEY = b'LT#AESKey3fswnXu'
AES_IV = b'LT#AESIV9ipyfsTi'
GUEST_ID_EPOCH_MS = int(
    datetime(2023, 1, 1, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1000
)
PHONE_RE = re.compile(r'1[3-9]\d{9}\Z')
SMS_CODE_RE = re.compile(r'\d{4,8}\Z')


class LoginSessionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LoginSessionData:
    phone_number: str
    guest_id: str
    serial_uuid: str
    android_id: str
    oaid: str
    created_at_ms: int
    expires_at_ms: int
    sms_sent: bool = False
    binding: BindingInput | None = None


def aes_cbc_encode(value: str) -> str:
    padder = PKCS7(128).padder()
    padded = padder.update(value.encode('utf-8')) + padder.finalize()
    encryptor = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_IV)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode('ascii')


def generate_guest_id(now_ms: int, *, random_suffix: int) -> str:
    suffix = int(random_suffix)
    if not 0 <= suffix <= 9999:
        raise ValueError('guestId 随机尾号必须位于 0..9999。')
    elapsed = max(0, int(now_ms) - GUEST_ID_EPOCH_MS)
    return str(elapsed * 10_000 + suffix)


class LoginClient(Protocol):
    async def get_captcha_app_id(self, session: LoginSessionData) -> str: ...

    async def send_sms(
        self,
        session: LoginSessionData,
        *,
        randstr: str,
        ticket: str,
    ) -> None: ...

    async def login_with_code(
        self,
        session: LoginSessionData,
        *,
        verification_code: str,
    ) -> BindingInput: ...

    async def aclose(self) -> None: ...


class WendaoLoginClient:
    def __init__(
        self,
        *,
        device: str = DEFAULT_DEVICE,
        version: str = DEFAULT_VERSION,
        version_code: str = DEFAULT_VERSION_CODE,
        client_type: str = DEFAULT_CLIENT_TYPE,
        timeout_seconds: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.device = device
        self.version = version
        self.version_code = version_code
        self.client_type = client_type
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_captcha_app_id(self, session: LoginSessionData) -> str:
        data = await self._request(
            CAPTCHA_CONFIG_PATH,
            guest_id=session.guest_id,
            method='GET',
        )
        config = data.get('verificationCodeCaptchaConfig')
        config = config if isinstance(config, dict) else {}
        try:
            is_close = int(config.get('isClose') or 0)
        except (TypeError, ValueError):
            is_close = 0
        app_id = str(config.get('appId') or '').strip()
        if is_close != 0:
            raise WendaoBusinessError(
                '腾讯人机验证当前已关闭。',
                retryable=False,
            )
        if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', app_id) is None:
            raise WendaoBusinessError(
                '问道配置响应缺少腾讯验证码 App ID。',
                retryable=False,
            )
        return app_id

    def _headers(self, guest_id: str) -> dict[str, str]:
        timestamp = str(self._clock_ms())
        return {
            'timestamp': timestamp,
            'device': self.device,
            'version': self.version,
            'versioncode': self.version_code,
            'guestid': guest_id,
            'clienttype': self.client_type,
            'sign': calculate_sign(self.device, timestamp),
            'content-type': 'application/json; charset=UTF-8',
            'accept-encoding': 'gzip',
            'user-agent': 'okhttp/4.4.1',
        }

    async def send_sms(
        self,
        session: LoginSessionData,
        *,
        randstr: str,
        ticket: str,
    ) -> None:
        data = await self._request(
            SMS_PATH,
            guest_id=session.guest_id,
            json_body={
                'phoneNo': aes_cbc_encode(session.phone_number),
                'scene': 'LOGIN_LEC',
                'verificationCode': {
                    'imei': session.guest_id,
                    'macAddress': '',
                    'randStr': randstr,
                    'scene': 1,
                    'ticket': ticket,
                },
            },
        )
        try:
            status = int(data.get('status', -1))
        except (TypeError, ValueError):
            status = -1
        if status != 0:
            raise WendaoBusinessError(
                '问道短信发送未成功。',
                retryable=False,
                code=SUCCESS_CODE,
                data=data,
            )

    async def login_with_code(
        self,
        session: LoginSessionData,
        *,
        verification_code: str,
    ) -> BindingInput:
        device_model, separator, os_version = self.device.partition('/Android/')
        if not separator or not device_model or not os_version:
            raise WendaoBusinessError(
                '问道登录设备字段格式错误。',
                retryable=False,
            )
        extend_json = json.dumps(
            {
                'encode': '1',
                'androidId': session.android_id,
                'oaid': session.oaid,
            },
            ensure_ascii=False,
            separators=(',', ':'),
        )
        data = await self._request(
            PHONE_LOGIN_PATH,
            guest_id=session.guest_id,
            json_body={
                'flyData': {
                    'clientVer': self.version_code,
                    'extend': aes_cbc_encode(extend_json),
                    'media': '0',
                    'osVer': os_version,
                    'terminInfo': device_model,
                },
                'phoneNo': aes_cbc_encode(session.phone_number),
                'serial': aes_cbc_encode(session.serial_uuid),
                'verificationCode': verification_code,
            },
        )
        token_data = data.get('token')
        token_data = token_data if isinstance(token_data, dict) else {}
        access_token = str(token_data.get('accessToken') or '').strip()
        refresh_token = str(token_data.get('refreshToken') or '').strip()
        try:
            valid_time = int(token_data.get('accessTokenValidTime') or 0)
        except (TypeError, ValueError):
            valid_time = 0
        if not access_token or not refresh_token or valid_time <= 0:
            raise WendaoBusinessError(
                '问道登录响应缺少访问凭据。',
                retryable=False,
            )
        bind_server = data.get('bindServer')
        bind_server = bind_server if isinstance(bind_server, dict) else {}
        return BindingInput(
            credentials=Credentials(
                token=access_token,
                device=self.device,
                version=self.version,
                version_code=self.version_code,
                guest_id=session.guest_id,
                client_type=self.client_type,
                refresh_token=refresh_token,
                access_token_valid_time=valid_time,
            ),
            nickname=str(bind_server.get('name') or '').strip(),
            user_identifier=str(data.get('ltUid') or '').strip(),
        )

    async def _request(
        self,
        path: str,
        *,
        guest_id: str,
        json_body: dict[str, object] | None = None,
        method: str = 'POST',
    ) -> dict[str, object]:
        try:
            response = await self._client.request(
                method,
                path,
                headers=self._headers(guest_id),
                json=json_body,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            raise WendaoRetryableError(
                '问道登录接口网络请求失败。',
                retryable=True,
            ) from None
        if response.status_code >= 500:
            raise WendaoRetryableError(
                f'问道登录接口暂时不可用（HTTP {response.status_code}）。',
                retryable=True,
            )
        if response.status_code != 200:
            raise WendaoBusinessError(
                f'问道登录接口返回 HTTP {response.status_code}。',
                retryable=False,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise WendaoBusinessError(
                '问道登录接口返回了无效 JSON。',
                retryable=False,
            ) from exc
        if not isinstance(payload, dict):
            raise WendaoBusinessError('问道登录接口响应格式错误。', retryable=False)
        try:
            code = int(payload.get('code', 0))
        except (TypeError, ValueError):
            code = 0
        data = payload.get('data')
        data = data if isinstance(data, dict) else {}
        if code != SUCCESS_CODE:
            raise WendaoBusinessError(
                f'问道登录接口业务错误 {code}。',
                retryable=False,
                code=code,
                data=data,
            )
        return data


class WendaoLoginService:
    def __init__(
        self,
        *,
        client_factory: Callable[[], LoginClient],
        ttl_seconds: int = 600,
        semaphore: asyncio.Semaphore | None = None,
        clock_ms: Callable[[], int] | None = None,
        random_suffix: Callable[[], int] | None = None,
        uuid_factory: Callable[[], str] | None = None,
        token_hex: Callable[[int], str] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._ttl_ms = max(60, min(1800, int(ttl_seconds))) * 1000
        self._semaphore = semaphore
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._random_suffix = random_suffix or (lambda: secrets.randbelow(10_000))
        self._uuid_factory = uuid_factory or (lambda: str(uuid.uuid4()))
        self._token_hex = token_hex or secrets.token_hex
        self._sessions: dict[tuple[str, str], LoginSessionData] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._expiry_handles: dict[tuple[str, str], asyncio.TimerHandle] = {}

    def _key(self, bot_uuid: str, sender_id: str) -> tuple[str, str]:
        return str(bot_uuid), str(sender_id)

    def _lock(self, key: tuple[str, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _remove_session_locked(self, key: tuple[str, str]) -> None:
        self._sessions.pop(key, None)
        handle = self._expiry_handles.pop(key, None)
        if handle is not None:
            handle.cancel()

    def _schedule_expiry(
        self,
        key: tuple[str, str],
        expires_at_ms: int,
    ) -> None:
        previous = self._expiry_handles.pop(key, None)
        if previous is not None:
            previous.cancel()
        service_ref = weakref.ref(self)

        def expire_callback() -> None:
            service = service_ref()
            if service is None:
                return
            service._expiry_handles.pop(key, None)
            asyncio.create_task(service._expire_session(key, expires_at_ms))

        delay_seconds = max(0.0, (expires_at_ms - self._clock_ms()) / 1000)
        self._expiry_handles[key] = asyncio.get_running_loop().call_later(
            delay_seconds,
            expire_callback,
        )

    async def _expire_session(
        self,
        key: tuple[str, str],
        expires_at_ms: int,
    ) -> None:
        async with self._lock(key):
            session = self._sessions.get(key)
            if (
                session is not None
                and session.expires_at_ms == expires_at_ms
                and self._clock_ms() >= expires_at_ms
            ):
                self._remove_session_locked(key)

    async def purge_expired(self) -> None:
        now_ms = self._clock_ms()
        for key in list(self._sessions):
            async with self._lock(key):
                session = self._sessions.get(key)
                if session is not None and now_ms >= session.expires_at_ms:
                    self._remove_session_locked(key)

    def close(self) -> None:
        for handle in self._expiry_handles.values():
            handle.cancel()
        self._expiry_handles.clear()
        self._sessions.clear()

    async def begin(
        self,
        bot_uuid: str,
        sender_id: str,
        phone_number: str,
    ) -> LoginSessionData:
        phone = str(phone_number).strip()
        if PHONE_RE.fullmatch(phone) is None:
            raise LoginSessionError('手机号格式错误，请输入 11 位中国大陆手机号。')
        key = self._key(bot_uuid, sender_id)
        async with self._lock(key):
            now_ms = self._clock_ms()
            session = LoginSessionData(
                phone_number=phone,
                guest_id=generate_guest_id(
                    now_ms,
                    random_suffix=self._random_suffix(),
                ),
                serial_uuid=self._uuid_factory(),
                android_id=self._token_hex(8),
                oaid=self._token_hex(8),
                created_at_ms=now_ms,
                expires_at_ms=now_ms + self._ttl_ms,
            )
            self._sessions[key] = session
            self._schedule_expiry(key, session.expires_at_ms)
            return session

    def _require_session(self, key: tuple[str, str]) -> LoginSessionData:
        session = self._sessions.get(key)
        if session is None:
            raise LoginSessionError('尚未创建问道登录会话，请先发送“问道登录 <手机号>”。')
        if self._clock_ms() >= session.expires_at_ms:
            self._remove_session_locked(key)
            raise LoginSessionError('问道登录会话已过期，请重新发送“问道登录 <手机号>”。')
        return session

    async def _send_sms_request(
        self,
        client: LoginClient,
        session: LoginSessionData,
        randstr: str,
        ticket: str,
    ) -> None:
        if self._semaphore is None:
            await client.send_sms(session, randstr=randstr, ticket=ticket)
            return
        async with self._semaphore:
            await client.send_sms(session, randstr=randstr, ticket=ticket)

    async def submit_captcha(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        randstr: str,
        ticket: str,
    ) -> None:
        normalized_randstr = str(randstr).strip()
        normalized_ticket = str(ticket).strip()
        if not normalized_randstr or not normalized_ticket:
            raise LoginSessionError('腾讯人机验证票据格式错误。')
        if len(normalized_randstr) > 128 or len(normalized_ticket) > 4096:
            raise LoginSessionError('腾讯人机验证票据格式错误。')
        key = self._key(bot_uuid, sender_id)
        async with self._lock(key):
            session = self._require_session(key)
            if session.sms_sent:
                return
            client = self._client_factory()
            try:
                await self._send_sms_request(
                    client,
                    session,
                    normalized_randstr,
                    normalized_ticket,
                )
            finally:
                await client.aclose()
            self._sessions[key] = replace(session, sms_sent=True)

    async def get_captcha_app_id(self, bot_uuid: str, sender_id: str) -> str:
        key = self._key(bot_uuid, sender_id)
        async with self._lock(key):
            session = self._require_session(key)
            client = self._client_factory()
            try:
                if self._semaphore is None:
                    return await client.get_captcha_app_id(session)
                async with self._semaphore:
                    return await client.get_captcha_app_id(session)
            finally:
                await client.aclose()

    async def is_waiting_for_code(self, bot_uuid: str, sender_id: str) -> bool:
        key = self._key(bot_uuid, sender_id)
        async with self._lock(key):
            session = self._sessions.get(key)
            if session is None:
                return False
            if self._clock_ms() >= session.expires_at_ms:
                self._remove_session_locked(key)
                return False
            return session.sms_sent

    async def _login_request(
        self,
        client: LoginClient,
        session: LoginSessionData,
        verification_code: str,
    ) -> BindingInput:
        if self._semaphore is None:
            return await client.login_with_code(
                session,
                verification_code=verification_code,
            )
        async with self._semaphore:
            return await client.login_with_code(
                session,
                verification_code=verification_code,
            )

    async def submit_code(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        verification_code: str,
    ) -> BindingInput:
        code = str(verification_code).strip()
        if SMS_CODE_RE.fullmatch(code) is None:
            raise LoginSessionError('短信验证码格式错误，请输入 4 至 8 位数字。')
        key = self._key(bot_uuid, sender_id)
        async with self._lock(key):
            session = self._require_session(key)
            if not session.sms_sent:
                raise LoginSessionError('请先完成人机验证并发送短信验证码。')
            if session.binding is not None:
                return session.binding
            client = self._client_factory()
            try:
                binding = await self._login_request(client, session, code)
            finally:
                await client.aclose()
            self._sessions[key] = replace(session, binding=binding)
            return binding

    async def complete(self, bot_uuid: str, sender_id: str) -> None:
        key = self._key(bot_uuid, sender_id)
        async with self._lock(key):
            self._remove_session_locked(key)

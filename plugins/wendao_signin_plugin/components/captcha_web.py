from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
import re
import secrets
import time
from typing import Protocol
from urllib.parse import urlsplit

from aiohttp import web

from components.api_client import WendaoBusinessError, WendaoRetryableError
from components.login import LoginSessionError


CAPTCHA_PATH = '/wendao/captcha'
NONCE_RE = re.compile(r'[A-Za-z0-9_-]{16,128}\Z')
APP_ID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
MAX_CALLBACK_BODY_BYTES = 8 * 1024
MAX_RANDSTR_LENGTH = 128
MAX_TICKET_LENGTH = 4096


class LoginCaptchaService(Protocol):
    async def submit_captcha(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        randstr: str,
        ticket: str,
    ) -> None: ...


CaptchaNotifier = Callable[[str, str, str, str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CaptchaChallenge:
    bot_uuid: str
    sender_id: str
    target_type: str
    target_id: str
    captcha_app_id: str
    expires_at_ms: int


class CaptchaWebServer:
    def __init__(
        self,
        *,
        login_service: LoginCaptchaService,
        public_base_url: str,
        bind_host: str = '0.0.0.0',
        bind_port: int = 8788,
        clock_ms: Callable[[], int] | None = None,
        token_urlsafe: Callable[[int], str] | None = None,
        notify: CaptchaNotifier | None = None,
    ) -> None:
        parsed = urlsplit(str(public_base_url).strip())
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
            raise ValueError('验证码公开地址必须是完整的 http 或 https 地址。')
        if parsed.query or parsed.fragment:
            raise ValueError('验证码公开地址不可包含查询参数或片段。')
        port = int(bind_port)
        if not 1 <= port <= 65535:
            raise ValueError('验证码监听端口必须位于 1..65535。')

        self._login_service = login_service
        self._public_base_url = str(public_base_url).strip().rstrip('/')
        self._bind_host = str(bind_host).strip() or '0.0.0.0'
        self._bind_port = port
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._token_urlsafe = token_urlsafe or secrets.token_urlsafe
        self._notify = notify
        self._route_prefix = parsed.path.rstrip('/')
        self._route_path = f'{self._route_prefix}{CAPTCHA_PATH}/{{nonce}}'
        self._challenges: dict[str, CaptchaChallenge] = {}
        self._identity_nonces: dict[tuple[str, str], str] = {}
        self._consumed_nonces: dict[str, int] = {}
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._cleanup_task = None

        self.app = web.Application(client_max_size=MAX_CALLBACK_BODY_BYTES)
        self.app.router.add_get(self._route_path, self._handle_page)
        self.app.router.add_post(self._route_path, self._handle_callback)

    async def start(self) -> None:
        if self._runner is not None:
            return
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(
                runner,
                host=self._bind_host,
                port=self._bind_port,
            )
            await site.start()
        except Exception:
            await runner.cleanup()
            raise
        self._runner = runner
        self._site = site

    async def aclose(self) -> None:
        runner = self._runner
        self._runner = None
        self._site = None
        self._challenges.clear()
        self._identity_nonces.clear()
        self._consumed_nonces.clear()
        if runner is not None:
            await runner.cleanup()

    def stop(self) -> None:
        if self._runner is None:
            return
        try:
            import asyncio

            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._cleanup_task = loop.create_task(self.aclose())

    def _purge_consumed(self) -> None:
        now_ms = self._clock_ms()
        for nonce, expires_at_ms in list(self._consumed_nonces.items()):
            if now_ms > expires_at_ms:
                self._consumed_nonces.pop(nonce, None)

    def _consume_nonce(self, nonce: str, challenge: CaptchaChallenge) -> None:
        self._challenges.pop(nonce, None)
        identity = (challenge.bot_uuid, challenge.sender_id)
        if self._identity_nonces.get(identity) == nonce:
            self._identity_nonces.pop(identity, None)
        self._consumed_nonces[nonce] = challenge.expires_at_ms

    def discard_identity(self, bot_uuid: str, sender_id: str) -> None:
        identity = (str(bot_uuid), str(sender_id))
        nonce = self._identity_nonces.pop(identity, None)
        if nonce is None:
            return
        challenge = self._challenges.pop(nonce, None)
        if challenge is not None:
            self._consumed_nonces[nonce] = challenge.expires_at_ms

    def create_challenge(
        self,
        *,
        bot_uuid: str,
        sender_id: str,
        target_type: str,
        target_id: str,
        captcha_app_id: str,
        expires_at_ms: int,
    ) -> str:
        app_id = str(captcha_app_id).strip()
        if APP_ID_RE.fullmatch(app_id) is None:
            raise ValueError('腾讯验证码 App ID 格式错误。')
        expiry = int(expires_at_ms)
        if expiry <= self._clock_ms():
            raise ValueError('登录会话已过期。')
        normalized_target_type = str(target_type).strip().lower()
        if normalized_target_type not in {'person', 'group'}:
            raise ValueError('验证码回复目标类型错误。')

        identity = (str(bot_uuid), str(sender_id))
        self.discard_identity(*identity)
        self._purge_consumed()
        for _ in range(8):
            nonce = self._token_urlsafe(32)
            if NONCE_RE.fullmatch(nonce) is None:
                continue
            if nonce not in self._challenges and nonce not in self._consumed_nonces:
                break
        else:
            raise RuntimeError('生成验证码链接失败。')

        self._challenges[nonce] = CaptchaChallenge(
            bot_uuid=identity[0],
            sender_id=identity[1],
            target_type=normalized_target_type,
            target_id=str(target_id),
            captcha_app_id=app_id,
            expires_at_ms=expiry,
        )
        self._identity_nonces[identity] = nonce
        return f'{self._public_base_url}{CAPTCHA_PATH}/{nonce}'

    def _find_challenge(
        self,
        nonce: str,
    ) -> tuple[CaptchaChallenge | None, int]:
        self._purge_consumed()
        if NONCE_RE.fullmatch(nonce) is None:
            return None, 404
        challenge = self._challenges.get(nonce)
        if challenge is None:
            return None, 410 if nonce in self._consumed_nonces else 404
        if self._clock_ms() >= challenge.expires_at_ms:
            self._consume_nonce(nonce, challenge)
            return None, 410
        return challenge, 200

    @staticmethod
    def _response_headers() -> dict[str, str]:
        return {
            'Cache-Control': 'no-store',
            'Pragma': 'no-cache',
            'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY',
        }

    def _render_page(self, challenge: CaptchaChallenge, callback_path: str) -> str:
        app_id = json.dumps(challenge.captcha_app_id, ensure_ascii=True)
        callback_url = json.dumps(callback_path, ensure_ascii=True)
        return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>问道登录验证</title>
  <style>
    body {{ margin: 0; font-family: sans-serif; background: #f5f7fa; color: #17202a; }}
    main {{ max-width: 30rem; margin: 12vh auto; padding: 1.5rem; text-align: center; }}
    button {{ min-height: 2.75rem; padding: 0 1.25rem; border: 0; border-radius: 6px;
      background: #1769aa; color: white; font-size: 1rem; cursor: pointer; }}
    #status {{ min-height: 3rem; margin-top: 1rem; line-height: 1.6; }}
  </style>
  <script src="https://turing.captcha.qcloud.com/TCaptcha.js"></script>
</head>
<body>
  <main>
    <h1>问道登录验证</h1>
    <button id="verify" type="button">开始验证</button>
    <p id="status">请完成人机验证。</p>
  </main>
  <script>
    const appId = {app_id};
    const callbackUrl = {callback_url};
    const statusNode = document.getElementById('status');
    async function onCaptcha(result) {{
      if (!result || result.ret !== 0) {{
        statusNode.textContent = '验证未完成，请重新操作。';
        return;
      }}
      statusNode.textContent = '正在发送短信验证码...';
      try {{
        const response = await fetch(callbackUrl, {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{ticket: result.ticket, randstr: result.randstr}})
        }});
        const payload = await response.json();
        statusNode.textContent = payload.message || '处理完成。';
        if (response.ok) document.getElementById('verify').disabled = true;
      }} catch (_) {{
        statusNode.textContent = '网络请求失败，请稍后重新验证。';
      }}
    }}
    function showCaptcha() {{
      if (typeof TencentCaptcha !== 'function') {{
        statusNode.textContent = '验证码组件加载失败，请刷新页面。';
        return;
      }}
      const captcha = new TencentCaptcha(appId, onCaptcha, {{userLanguage: 'zh-cn'}});
      captcha.show();
    }}
    document.getElementById('verify').addEventListener('click', showCaptcha);
    window.addEventListener('load', showCaptcha);
  </script>
</body>
</html>'''

    async def _handle_page(self, request: web.Request) -> web.Response:
        nonce = request.match_info['nonce']
        challenge, status = self._find_challenge(nonce)
        if challenge is None:
            message = '验证码链接已失效。' if status == 410 else '验证码链接不存在。'
            return web.Response(
                status=status,
                text=message,
                content_type='text/plain',
                charset='utf-8',
                headers=self._response_headers(),
            )
        return web.Response(
            text=self._render_page(challenge, request.path),
            content_type='text/html',
            charset='utf-8',
            headers=self._response_headers(),
        )

    @staticmethod
    def _json_response(status: int, ok: bool, message: str) -> web.Response:
        return web.json_response(
            {'ok': ok, 'message': message},
            status=status,
            headers=CaptchaWebServer._response_headers(),
        )

    async def _handle_callback(self, request: web.Request) -> web.Response:
        nonce = request.match_info['nonce']
        challenge, status = self._find_challenge(nonce)
        if challenge is None:
            message = '验证码链接已失效。' if status == 410 else '验证码链接不存在。'
            return self._json_response(status, False, message)
        try:
            payload = await request.json()
        except (ValueError, web.HTTPException):
            return self._json_response(400, False, '验证码回调格式错误。')
        if not isinstance(payload, dict):
            return self._json_response(400, False, '验证码回调格式错误。')
        randstr = str(payload.get('randstr') or '').strip()
        ticket = str(payload.get('ticket') or '').strip()
        if (
            not randstr
            or not ticket
            or len(randstr) > MAX_RANDSTR_LENGTH
            or len(ticket) > MAX_TICKET_LENGTH
        ):
            return self._json_response(400, False, '验证码回调格式错误。')

        try:
            await self._login_service.submit_captcha(
                challenge.bot_uuid,
                challenge.sender_id,
                randstr=randstr,
                ticket=ticket,
            )
        except LoginSessionError:
            self._consume_nonce(nonce, challenge)
            return self._json_response(410, False, '登录会话已过期，请重新发起登录。')
        except WendaoRetryableError:
            return self._json_response(502, False, '短信接口暂时不可用，请重新验证。')
        except WendaoBusinessError as exc:
            code_text = f'（业务码 {exc.code}）' if exc.code is not None else ''
            return self._json_response(422, False, f'短信发送失败{code_text}。')
        except Exception:
            return self._json_response(500, False, '短信发送异常，请重新验证。')

        self._consume_nonce(nonce, challenge)
        if self._notify is not None:
            try:
                await self._notify(
                    challenge.bot_uuid,
                    challenge.sender_id,
                    challenge.target_type,
                    challenge.target_id,
                    '验证码已发送，请直接回复短信中的验证码，例如：123456。',
                )
            except Exception:
                pass
        return self._json_response(
            200,
            True,
            '验证码已发送，请返回机器人并直接回复短信中的验证码。',
        )

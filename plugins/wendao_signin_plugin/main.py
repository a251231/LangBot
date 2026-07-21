from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from typing import Any
import weakref
from zoneinfo import ZoneInfo

from langbot_plugin.api.definition.plugin import BasePlugin
import langbot_plugin.api.entities.builtin.platform.message as platform_message

from components.account_session import ACCOUNT_NOT_BOUND_MESSAGE, AuthenticatedAccountSession
from components.account_store import AccountStore
from components.api_client import (
    WendaoApiClient,
    WendaoAuthError,
    WendaoBusinessError,
    WendaoRetryableError,
)
from components.captcha_web import CaptchaWebServer
from components.command_parser import (
    BindingInput,
    BindingParseError,
    ParsedCommand,
    parse_binding_input,
)
from components.entitlement import EntitlementService
from components.growth_service import GrowthService
from components.growth_store import GrowthStore
from components.login import LoginSessionError, WendaoLoginClient, WendaoLoginService
from components.models import AccountRecord, Credentials
from components.scheduler import SigninScheduler
from components.weekly_report import WeeklyReportService
from components.weekly_report_client import WeeklyReportClient
from components.workflow import (
    AccountNeedsRebindError,
    AccountNotBoundError,
    SigninWorkflow,
)


HELP_TEXT = '''问道签到助手命令：
问道登录 <手机号>
问道验证码 <短信码>
问道查询
问道签到
问道补签
问道时间 HH:MM
问道自动 开/关
问道自动补签 开/关
问道自动里程碑 开/关
问道周报
问道自动周报 开/关
问道设置
问道推广
问道邀请 <邀请码>
问道积分
问道商城
问道兑换 <商品ID>
问道兑换记录
问道激活 <卡密>
问道权益
问道解绑
问道帮助
访问凭据到期前会自动刷新。'''


class WendaoSigninPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__()
        self.account_store: AccountStore | None = None
        self.growth_store: GrowthStore | None = None
        self.growth_service: GrowthService | None = None
        self.entitlement_service: EntitlementService | None = None
        self.login_service: WendaoLoginService | None = None
        self.captcha_server: CaptchaWebServer | None = None
        self.account_session: AuthenticatedAccountSession[WendaoApiClient] | None = None
        self.workflow: SigninWorkflow | None = None
        self.weekly_report: WeeklyReportService | None = None
        self.scheduler: SigninScheduler | None = None
        self._request_semaphore: asyncio.Semaphore | None = None
        self._request_timeout_seconds = 15.0
        self._login_session_ttl_seconds = 600
        self._timezone = ZoneInfo('Asia/Shanghai')
        self._admin_user_ids: set[str] = set()

    def _config_value(self, name: str, default: Any) -> Any:
        return self.get_config().get(name, default)

    def _int_config(self, name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self._config_value(name, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _float_config(self, name: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(self._config_value(name, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _bool_config(self, name: str, default: bool) -> bool:
        value = self._config_value(name, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {'1', 'true', 'yes', 'on'}
        return bool(value)

    def _schedule_config(self) -> str:
        value = str(self._config_value('default_schedule_time', '08:00')).strip()
        return value if self._valid_schedule_time(value) else '08:00'

    async def initialize(self) -> None:
        timezone_name = str(self._config_value('timezone', 'Asia/Shanghai')).strip()
        try:
            self._timezone = ZoneInfo(timezone_name)
        except Exception:
            self._timezone = ZoneInfo('Asia/Shanghai')
        self._request_timeout_seconds = self._float_config(
            'request_timeout_seconds', 15, 1, 120
        )
        self._login_session_ttl_seconds = self._int_config(
            'login_session_ttl_seconds', 600, 60, 1800
        )
        max_concurrency = self._int_config('max_concurrency', 3, 1, 20)
        self._request_semaphore = asyncio.Semaphore(max_concurrency)
        self._admin_user_ids = {
            item.strip()
            for item in str(self._config_value('admin_user_ids', '')).split(',')
            if item.strip()
        }
        plugin_ref = weakref.ref(self)

        def create_client(account: AccountRecord) -> WendaoApiClient:
            plugin = plugin_ref()
            if plugin is None:
                raise RuntimeError('插件实例已释放。')
            return plugin._create_api_client(account.credentials)

        def create_login_client() -> WendaoLoginClient:
            plugin = plugin_ref()
            if plugin is None:
                raise RuntimeError('插件实例已释放。')
            return plugin._create_login_client()

        def create_weekly_report_client() -> WeeklyReportClient:
            plugin = plugin_ref()
            if plugin is None:
                raise RuntimeError('插件实例已释放。')
            return plugin._create_weekly_report_client()

        async def notify(account: AccountRecord, text: str) -> None:
            plugin = plugin_ref()
            if plugin is not None:
                await plugin._notify_account(account, text)

        async def notify_login(
            bot_uuid: str,
            sender_id: str,
            target_id: str,
            text: str,
        ) -> None:
            plugin = plugin_ref()
            if plugin is not None:
                await plugin._notify_login(bot_uuid, target_id, text)

        self.account_store = AccountStore(weakref.proxy(self))
        self.growth_store = GrowthStore(weakref.proxy(self))
        self.growth_service = GrowthService(
            self.growth_store,
            self.account_store,
            trial_days=self._int_config('growth_trial_days', 30, 0, 1_000_000),
            promoter_reward_points=self._int_config(
                'promoter_reward_points',
                100,
                0,
                1_000_000,
            ),
            invitee_reward_points=self._int_config(
                'invitee_reward_points',
                20,
                0,
                1_000_000,
            ),
        )
        self.entitlement_service = self.growth_service.entitlement_service
        await self.growth_service.initialize()
        self.login_service = WendaoLoginService(
            client_factory=create_login_client,
            ttl_seconds=self._login_session_ttl_seconds,
            semaphore=self._request_semaphore,
        )
        captcha_public_base_url = str(
            self._config_value('captcha_public_base_url', '')
        ).strip()
        if captcha_public_base_url:
            self.captcha_server = CaptchaWebServer(
                login_service=self.login_service,
                public_base_url=captcha_public_base_url,
                bind_host=str(
                    self._config_value('captcha_bind_host', '0.0.0.0')
                ).strip(),
                bind_port=self._int_config(
                    'captcha_bind_port',
                    8788,
                    1,
                    65535,
                ),
                notify=notify_login,
            )
            await self.captcha_server.start()
        self.account_session = AuthenticatedAccountSession(
            self.account_store,
            client_factory=create_client,
            max_concurrency=max_concurrency,
            semaphore=self._request_semaphore,
            timezone=self._timezone,
        )
        self.workflow = SigninWorkflow(
            self.account_store,
            session=self.account_session,
            timezone=self._timezone,
        )
        self.weekly_report = WeeklyReportService(
            self.account_store,
            session=self.account_session,
            activity_client_factory=create_weekly_report_client,
            timezone=self._timezone,
        )
        self.scheduler = SigninScheduler(
            self.account_store,
            self.workflow,
            weekly_report=self.weekly_report,
            notify=notify,
            poll_seconds=self._int_config('scheduler_poll_seconds', 30, 5, 3600),
            timezone=self._timezone,
        )
        self.scheduler.start()

    def __del__(self) -> None:
        scheduler = getattr(self, 'scheduler', None)
        if scheduler is not None:
            scheduler.stop()
        captcha_server = getattr(self, 'captcha_server', None)
        if captcha_server is not None:
            captcha_server.stop()
        login_service = getattr(self, 'login_service', None)
        if login_service is not None:
            login_service.close()

    def _create_api_client(self, credentials: Credentials) -> WendaoApiClient:
        return WendaoApiClient(
            credentials,
            timeout_seconds=self._request_timeout_seconds,
        )

    def _create_login_client(self) -> WendaoLoginClient:
        return WendaoLoginClient(timeout_seconds=self._request_timeout_seconds)

    def _create_weekly_report_client(self) -> WeeklyReportClient:
        return WeeklyReportClient(timeout_seconds=self._request_timeout_seconds)

    async def _notify_account(self, account: AccountRecord, text: str) -> None:
        chain = platform_message.MessageChain([platform_message.Plain(text=text)])
        await self.send_message(
            bot_uuid=account.bot_uuid,
            target_type=account.target_type,
            target_id=account.target_id,
            message_chain=chain,
        )

    async def _notify_login(
        self,
        bot_uuid: str,
        target_id: str,
        text: str,
    ) -> None:
        chain = platform_message.MessageChain([platform_message.Plain(text=text)])
        await self.send_message(
            bot_uuid=bot_uuid,
            target_type='person',
            target_id=target_id,
            message_chain=chain,
        )

    @staticmethod
    def _valid_schedule_time(value: str) -> bool:
        try:
            return datetime.strptime(value, '%H:%M').strftime('%H:%M') == value
        except ValueError:
            return False

    @staticmethod
    def _profile_from_status(data: dict[str, Any]) -> tuple[str, str]:
        containers = [data]
        for key in ('userInfo', 'user', 'userData'):
            nested = data.get(key)
            if isinstance(nested, dict):
                containers.append(nested)

        nickname = ''
        user_identifier = ''
        for container in containers:
            if not nickname:
                for key in ('nickname', 'nickName', 'name'):
                    value = container.get(key)
                    if value:
                        nickname = str(value).strip()
                        break
            if not user_identifier:
                for key in ('ltUid', 'ltuid', 'userId', 'uid', 'sid'):
                    value = container.get(key)
                    if value:
                        user_identifier = str(value).strip()
                        break
        return nickname, user_identifier

    async def _bind_account(
        self,
        *,
        bot_uuid: str,
        sender_id: str,
        target_id: str,
        binding_text: str,
    ) -> str:
        try:
            binding = parse_binding_input(binding_text)
        except BindingParseError as exc:
            return f'问道绑定失败：{exc}'

        reply, _ = await self._verify_and_save_binding(
            bot_uuid=bot_uuid,
            sender_id=sender_id,
            target_id=target_id,
            binding=binding,
            action_label='问道绑定',
            cleanup_label='聊天记录中的绑定消息',
        )
        return reply

    async def _verify_and_save_binding(
        self,
        *,
        bot_uuid: str,
        sender_id: str,
        target_id: str,
        binding: BindingInput,
        action_label: str,
        cleanup_label: str,
    ) -> tuple[str, bool]:
        assert self.account_store is not None
        credentials = binding.credentials

        client = self._create_api_client(credentials)
        try:
            assert self._request_semaphore is not None
            async with self._request_semaphore:
                response = await client.list_signin()
        except WendaoAuthError:
            return f'{action_label}验证失败：访问凭据已失效，请重新登录。', False
        except WendaoRetryableError:
            return f'{action_label}验证失败：网络请求失败，请稍后重试。', False
        except WendaoBusinessError as exc:
            code_text = f'，业务码 {exc.code}' if exc.code is not None else ''
            return f'{action_label}验证失败{code_text}。', False
        except Exception:
            return f'{action_label}验证失败：接口响应异常。', False
        finally:
            await client.aclose()

        status_nickname, status_identifier = self._profile_from_status(response.data)
        nickname = binding.nickname or status_nickname
        user_identifier = binding.user_identifier or status_identifier
        async with self.account_store.account_lock(bot_uuid, sender_id):
            existing = await self.account_store.get(bot_uuid, sender_id)
            record = AccountRecord.create(
                bot_uuid=bot_uuid,
                sender_id=sender_id,
                credentials=credentials,
                target_id=target_id,
                schedule_time=(
                    existing.schedule_time if existing else self._schedule_config()
                ),
                auto_signin=(
                    existing.auto_signin
                    if existing
                    else self._bool_config('default_auto_signin', True)
                ),
                auto_resign=(
                    existing.auto_resign
                    if existing
                    else self._bool_config('default_auto_resign', True)
                ),
                auto_milestone=(
                    existing.auto_milestone
                    if existing
                    else self._bool_config('default_auto_milestone', True)
                ),
                auto_weekly_report=(
                    existing.auto_weekly_report
                    if existing
                    else self._bool_config('default_auto_weekly_report', True)
                ),
                nickname=nickname or (existing.nickname if existing else ''),
                user_identifier=(
                    user_identifier or (existing.user_identifier if existing else '')
                ),
            )
            await self.account_store.save(record)
        if self.growth_service is not None:
            try:
                await self.growth_service.on_account_bound(record)
            except Exception:
                pass
        profile = f'（{record.nickname}）' if record.nickname else ''
        return (
            f'问道账号绑定成功{profile}。请立即删除{cleanup_label}。',
            True,
        )

    async def _start_phone_login(
        self,
        bot_uuid: str,
        sender_id: str,
        target_id: str,
        phone_number: str,
    ) -> str:
        assert self.login_service is not None
        try:
            session = await self.login_service.begin(bot_uuid, sender_id, phone_number)
        except LoginSessionError:
            return (
                '手机号格式错误，请发送：问道登录 11位手机号\n'
                '示例：问道登录 13800138000'
            )
        ttl_minutes = max(1, (self._login_session_ttl_seconds + 59) // 60)
        if self.captcha_server is None:
            return (
                '问道登录会话已创建，但验证码网页服务尚未配置。'
                '请让管理员设置 captcha_public_base_url 后重试；'
                '也可继续使用“问道验证 <randstr> <ticket>”。'
            )
        try:
            app_id = await self.login_service.get_captcha_app_id(
                bot_uuid,
                sender_id,
            )
            url = self.captcha_server.create_challenge(
                bot_uuid=bot_uuid,
                sender_id=sender_id,
                target_id=target_id,
                captcha_app_id=app_id,
                expires_at_ms=session.expires_at_ms,
            )
        except WendaoRetryableError:
            return '获取腾讯验证码配置失败，请稍后重新发送“问道登录 <手机号>”。'
        except WendaoBusinessError as exc:
            code_text = f'（业务码 {exc.code}）' if exc.code is not None else ''
            return f'获取腾讯验证码配置失败{code_text}。'
        except Exception:
            return '创建腾讯验证码链接失败，请稍后重试。'
        return (
            f'请在 {ttl_minutes} 分钟内打开腾讯验证码链接并完成人机验证：\n'
            f'{url}\n'
            '验证通过后会自动发送短信验证码。'
        )

    async def _submit_login_captcha(
        self,
        bot_uuid: str,
        sender_id: str,
        argument: str,
    ) -> str:
        parts = argument.split(maxsplit=1)
        if len(parts) != 2:
            return '问道验证格式错误，请发送“问道验证 <randstr> <ticket>”。'
        assert self.login_service is not None
        try:
            await self.login_service.submit_captcha(
                bot_uuid,
                sender_id,
                randstr=parts[0],
                ticket=parts[1],
            )
        except LoginSessionError as exc:
            return f'问道验证失败：{exc}'
        except WendaoRetryableError:
            return '问道短信接口网络请求失败，请稍后重试。'
        except WendaoBusinessError as exc:
            code_text = f'（业务码 {exc.code}）' if exc.code is not None else ''
            return f'问道短信发送失败{code_text}。'
        except Exception:
            return '问道短信发送异常，请稍后重试。'
        if self.captcha_server is not None:
            self.captcha_server.discard_identity(bot_uuid, sender_id)
        return '验证码已发送，请直接回复短信中的验证码，例如：123456。'

    async def is_waiting_for_login_code(self, bot_uuid: str, sender_id: str) -> bool:
        if self.login_service is None:
            return False
        return await self.login_service.is_waiting_for_code(bot_uuid, sender_id)

    async def _submit_login_code(
        self,
        *,
        bot_uuid: str,
        sender_id: str,
        target_id: str,
        verification_code: str,
    ) -> str:
        assert self.login_service is not None
        try:
            binding = await self.login_service.submit_code(
                bot_uuid,
                sender_id,
                verification_code=verification_code,
            )
        except LoginSessionError as exc:
            return f'问道登录失败：{exc}'
        except WendaoRetryableError:
            return '问道登录接口网络请求失败，请稍后重试。'
        except WendaoBusinessError as exc:
            code_text = f'（业务码 {exc.code}）' if exc.code is not None else ''
            return f'问道登录失败{code_text}。'
        except Exception:
            return '问道登录异常，请稍后重试。'

        reply, saved = await self._verify_and_save_binding(
            bot_uuid=bot_uuid,
            sender_id=sender_id,
            target_id=target_id,
            binding=binding,
            action_label='问道登录',
            cleanup_label='本次登录相关聊天记录',
        )
        if saved:
            await self.login_service.complete(bot_uuid, sender_id)
            if self.captcha_server is not None:
                self.captcha_server.discard_identity(bot_uuid, sender_id)
        return reply

    async def _set_schedule(self, bot_uuid: str, sender_id: str, value: str) -> str:
        if not self._valid_schedule_time(value):
            return '时间格式错误，请使用 24 小时制 HH:MM，例如 08:30。'
        assert self.account_store is not None
        async with self.account_store.account_lock(bot_uuid, sender_id):
            account = await self.account_store.get(bot_uuid, sender_id)
            if account is None:
                return ACCOUNT_NOT_BOUND_MESSAGE
            await self.account_store.save(replace(account, schedule_time=value))
        return f'问道自动执行时间已设置为 {value}（{self._timezone.key}）。'

    async def _set_toggle(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        field: str,
        argument: str,
        label: str,
    ) -> str:
        if argument not in {'开', '关'}:
            return f'参数错误，请使用“{label} 开”或“{label} 关”。'
        assert self.account_store is not None
        async with self.account_store.account_lock(bot_uuid, sender_id):
            account = await self.account_store.get(bot_uuid, sender_id)
            if account is None:
                return ACCOUNT_NOT_BOUND_MESSAGE
            await self.account_store.save(replace(account, **{field: argument == '开'}))
        return f'{label}已{"开启" if argument == "开" else "关闭"}。'

    async def _settings(self, bot_uuid: str, sender_id: str) -> str:
        assert self.account_store is not None
        async with self.account_store.account_lock(bot_uuid, sender_id):
            account = await self.account_store.get(bot_uuid, sender_id)
            if account is None:
                return ACCOUNT_NOT_BOUND_MESSAGE
        state = '需重新绑定' if account.needs_rebind else '已绑定'
        nickname = account.nickname or '未提供'
        last_result = account.last_result or '暂无'
        return '\n'.join(
            [
                f'问道账号：{state}',
                f'昵称：{nickname}',
                f'自动时间：{account.schedule_time}（{self._timezone.key}）',
                f'自动签到：{"开" if account.auto_signin else "关"}',
                f'自动补签：{"开" if account.auto_resign else "关"}',
                f'自动里程碑：{"开" if account.auto_milestone else "关"}',
                (
                    '自动周报：'
                    f'{"开" if account.auto_weekly_report else "关"}'
                    '（每周一 09:00）'
                ),
                f'最近结果：{last_result}',
            ]
        )

    async def _run_manual(self, bot_uuid: str, sender_id: str, mode: str) -> str:
        assert self.workflow is not None
        try:
            outcome = await self.workflow.execute(bot_uuid, sender_id, mode=mode)  # type: ignore[arg-type]
        except (AccountNotBoundError, AccountNeedsRebindError) as exc:
            return str(exc)
        except WendaoAuthError:
            return '问道账号凭据已失效，请重新登录并发送新的登录响应。'
        except WendaoRetryableError:
            return '问道接口网络请求失败，请稍后重试。'
        except WendaoBusinessError as exc:
            code_text = f'（业务码 {exc.code}）' if exc.code is not None else ''
            return f'问道操作失败{code_text}。'
        except Exception:
            return '问道操作执行异常，请稍后重试。'
        return outcome.message

    async def _run_weekly_report(self, bot_uuid: str, sender_id: str) -> str:
        assert self.weekly_report is not None
        try:
            outcome = await self.weekly_report.execute(bot_uuid, sender_id)
        except (AccountNotBoundError, AccountNeedsRebindError) as exc:
            return str(exc)
        except WendaoAuthError:
            return '问道账号凭据已失效，请重新登录并发送新的登录响应。'
        except WendaoRetryableError:
            return '问道周报网络请求失败，请稍后重试。'
        except WendaoBusinessError as exc:
            code_text = f'（业务码 {exc.code}）' if exc.code is not None else ''
            return f'问道周报查询失败{code_text}。'
        except Exception:
            return '问道周报查询异常，请稍后重试。'
        return outcome.message

    async def handle_wendao_command(
        self,
        *,
        bot_uuid: str,
        sender_id: str,
        target_id: str,
        is_group: bool,
        command: ParsedCommand,
        request_id: str = '',
    ) -> str:
        if is_group and command.kind != 'help':
            return '问道签到助手仅在私聊中处理账号操作，请转到机器人私聊后重试。'
        if command.kind == 'help':
            return HELP_TEXT
        if command.kind == 'login_start':
            return await self._start_phone_login(
                bot_uuid,
                sender_id,
                target_id,
                command.argument,
            )
        if command.kind == 'login_captcha':
            return await self._submit_login_captcha(
                bot_uuid,
                sender_id,
                command.argument,
            )
        if command.kind == 'login_code':
            return await self._submit_login_code(
                bot_uuid=bot_uuid,
                sender_id=sender_id,
                target_id=target_id,
                verification_code=command.argument,
            )
        if command.kind == 'bind':
            return await self._bind_account(
                bot_uuid=bot_uuid,
                sender_id=sender_id,
                target_id=target_id,
                binding_text=command.argument,
            )
        if command.kind in {'query', 'signin', 'resign'}:
            return await self._run_manual(bot_uuid, sender_id, command.kind)
        if command.kind == 'weekly_report':
            return await self._run_weekly_report(bot_uuid, sender_id)
        if command.kind == 'schedule_time':
            return await self._set_schedule(bot_uuid, sender_id, command.argument)
        if command.kind == 'auto_signin':
            return await self._set_toggle(
                bot_uuid,
                sender_id,
                field='auto_signin',
                argument=command.argument,
                label='问道自动',
            )
        if command.kind == 'auto_resign':
            return await self._set_toggle(
                bot_uuid,
                sender_id,
                field='auto_resign',
                argument=command.argument,
                label='问道自动补签',
            )
        if command.kind == 'auto_milestone':
            return await self._set_toggle(
                bot_uuid,
                sender_id,
                field='auto_milestone',
                argument=command.argument,
                label='问道自动里程碑',
            )
        if command.kind == 'auto_weekly_report':
            return await self._set_toggle(
                bot_uuid,
                sender_id,
                field='auto_weekly_report',
                argument=command.argument,
                label='问道自动周报',
            )
        if command.kind == 'settings':
            return await self._settings(bot_uuid, sender_id)
        if command.kind == 'promotion':
            assert self.growth_service is not None
            return await self.growth_service.promotion(bot_uuid, sender_id)
        if command.kind == 'referral_bind':
            assert self.growth_service is not None
            return await self.growth_service.bind_referral(
                bot_uuid,
                sender_id,
                command.argument,
            )
        if command.kind == 'points':
            assert self.growth_service is not None
            return await self.growth_service.points(bot_uuid, sender_id)
        if command.kind == 'shop':
            assert self.growth_service is not None
            return await self.growth_service.shop(bot_uuid)
        if command.kind == 'redeem':
            assert self.growth_service is not None
            return await self.growth_service.redeem(
                bot_uuid,
                sender_id,
                command.argument,
                request_id=request_id,
            )
        if command.kind == 'redemptions':
            assert self.growth_service is not None
            return await self.growth_service.redemptions(bot_uuid, sender_id)
        if command.kind == 'activate':
            assert self.growth_service is not None
            return await self.growth_service.activate(
                bot_uuid,
                sender_id,
                command.argument,
            )
        if command.kind == 'entitlement':
            assert self.growth_service is not None
            return await self.growth_service.entitlement(bot_uuid, sender_id)
        if command.kind == 'admin':
            if sender_id not in self._admin_user_ids:
                return '无权限执行问道管理命令。'
            assert self.growth_service is not None
            return await self.growth_service.admin(
                bot_uuid,
                command.argument,
                request_id=request_id,
            )
        if command.kind == 'unbind':
            assert self.account_store is not None
            assert self.login_service is not None
            await self.login_service.complete(bot_uuid, sender_id)
            if self.captcha_server is not None:
                self.captcha_server.discard_identity(bot_uuid, sender_id)
            async with self.account_store.account_lock(bot_uuid, sender_id):
                deleted = await self.account_store.delete(bot_uuid, sender_id)
            return '问道账号解绑成功。' if deleted else '当前未绑定问道账号。'
        return '未知问道命令，请执行“问道帮助”查看用法。'

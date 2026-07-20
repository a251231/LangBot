from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from components.account_session import (
    AccountNeedsRebindError as AccountNeedsRebindError,
    AccountNotBoundError as AccountNotBoundError,
    AuthenticatedAccountSession,
)
from components.account_store import AccountStore
from components.api_client import WendaoBusinessError
from components.models import (
    AccountRecord,
    ApiResponse,
    Credentials,
    credentials_fingerprint,
)


WorkflowMode = Literal['query', 'signin', 'resign', 'auto']


class WorkflowClient(Protocol):
    async def list_signin(self) -> ApiResponse: ...

    async def signin(self, signin_type: int) -> ApiResponse: ...

    async def claim_milestone(self) -> ApiResponse: ...

    async def report_post_read(self, source_id: str) -> ApiResponse: ...

    async def refresh_access_token(self) -> Credentials: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkflowOutcome:
    status: dict[str, Any]
    actions: tuple[str, ...]
    message: str
    run_date: str = ''
    credentials_fingerprint: str = ''


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _milestone_state(status: dict[str, Any]) -> int:
    milestone = status.get('milestoneData')
    if not isinstance(milestone, dict):
        return 0
    return _as_int(milestone.get('state'))


def _resign_task_message(
    data: dict[str, Any],
    *,
    read_reported: bool = False,
) -> str:
    task = data.get('reSignData')
    task = task if isinstance(task, dict) else {}
    task_type = _as_int(task.get('type'))
    if task_type == 1:
        return '补签前需先在问道社区 App 内发布动态，完成后请再次执行“问道补签”。'
    if task_type == 2:
        if read_reported:
            return '已上报阅读指定文章，但服务端仍提示不满足补签条件，请稍后在 App 内确认后再次执行“问道补签”。'
        return '补签前需先在问道社区 App 内阅读指定文章，完成后请再次执行“问道补签”。'
    return '补签前需先在问道社区 App 内完成指定任务，完成后请再次执行“问道补签”。'


def _read_task_source_id(data: dict[str, Any]) -> str:
    task = data.get('reSignData')
    if not isinstance(task, dict) or _as_int(task.get('type')) != 2:
        return ''
    source_id = str(task.get('sourceId') or '').strip()
    if not source_id.isascii() or not source_id.isdigit():
        return ''
    return source_id


def _format_outcome(
    status: dict[str, Any],
    actions: list[str],
    notes: list[str],
) -> str:
    signed = _as_int(status.get('signInStatus')) == 2
    sign_num = _as_int(status.get('signNum'))
    lines = [f'问道签到状态：{"今日已签到" if signed else "今日未签到"}，本月已签 {sign_num} 天。']
    action_text = {
        'signin': '今日签到成功。',
        'resign': '补签成功。',
        'resign_task': '本次补签需要先完成社区任务。',
        'milestone': '签到里程碑奖励已领取。',
    }
    lines.extend(action_text[action] for action in actions if action in action_text)
    lines.extend(notes)
    return '\n'.join(lines)


class SigninWorkflow:
    def __init__(
        self,
        store: AccountStore,
        *,
        client_factory: Callable[[AccountRecord], WorkflowClient] | None = None,
        session: AuthenticatedAccountSession[WorkflowClient] | None = None,
        max_concurrency: int = 3,
        semaphore: asyncio.Semaphore | None = None,
        timezone: ZoneInfo | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._timezone = timezone or ZoneInfo('Asia/Shanghai')
        timezone_value = self._timezone
        self._now = now or (lambda: datetime.now(timezone_value))
        if session is None:
            if client_factory is None:
                raise ValueError('必须提供账号会话或客户端工厂。')
            session = AuthenticatedAccountSession(
                store,
                client_factory=client_factory,
                max_concurrency=max_concurrency,
                semaphore=semaphore,
                timezone=self._timezone,
                now=self._now,
            )
        self._session = session

    def _current_time(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=self._timezone)
        return value.astimezone(self._timezone)

    async def execute(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        mode: WorkflowMode,
    ) -> WorkflowOutcome:
        if mode not in {'query', 'signin', 'resign', 'auto'}:
            raise ValueError(f'不支持的工作流模式：{mode}')
        return await self._session.execute(
            bot_uuid,
            sender_id,
            lambda account, client: self._run_with_client(account, client, mode),
        )

    async def _run_with_client(
        self,
        account: AccountRecord,
        client: WorkflowClient,
        mode: WorkflowMode,
    ) -> WorkflowOutcome:
        now = self._current_time()
        today = now.date().isoformat()
        status = (await client.list_signin()).data
        actions: list[str] = []
        notes: list[str] = []

        if mode == 'query':
            message = _format_outcome(status, actions, notes)
            await self._store.save(
                replace(
                    account,
                    needs_rebind=False,
                    last_run_at=now.isoformat(timespec='seconds'),
                    last_result=message,
                )
            )
            return WorkflowOutcome(
                status=status,
                actions=(),
                message=message,
                run_date=today,
                credentials_fingerprint=credentials_fingerprint(account.credentials),
            )

        allow_signin = mode == 'signin' or (mode == 'auto' and account.auto_signin)
        allow_resign = mode == 'resign' or (
            mode in {'signin', 'auto'} and account.auto_resign
        )
        allow_milestone = mode in {'signin', 'auto'} and account.auto_milestone

        if allow_signin and _as_int(status.get('signInStatus')) == 1:
            await client.signin(1)
            actions.append('signin')
            status = (await client.list_signin()).data

        resign_num = _as_int(status.get('reSignNum'))
        resign_limit = _as_int(status.get('reSignNumLimit'))
        can_resign = _as_int(status.get('reSignInStatus')) == 1
        limit_reached = resign_limit > 0 and resign_num >= resign_limit
        daily_attempted = mode == 'auto' and account.last_resign_attempt_date == today
        if allow_resign and can_resign and limit_reached:
            notes.append(f'本月补签次数已用完（{resign_num}/{resign_limit}）。')
        elif allow_resign and can_resign and not daily_attempted:
            account = replace(account, last_resign_attempt_date=today)
            await self._store.save(account)
            try:
                await client.signin(2)
            except WendaoBusinessError as exc:
                if exc.code != 4102:
                    raise
                pending_task, read_reported = (
                    await self._retry_resign_after_read_task(client, exc)
                )
                if pending_task is None:
                    actions.append('resign')
                    status = (await client.list_signin()).data
                else:
                    actions.append('resign_task')
                    notes.append(
                        _resign_task_message(
                            pending_task.data,
                            read_reported=read_reported,
                        )
                    )
            else:
                actions.append('resign')
                status = (await client.list_signin()).data

        if allow_milestone and _milestone_state(status) == 3:
            await client.claim_milestone()
            actions.append('milestone')

        status = (await client.list_signin()).data
        message = _format_outcome(status, actions, notes)
        updates = {
            'needs_rebind': False,
            'next_retry_at': '',
            'retry_count': 0,
            'retry_origin_date': '',
            'last_run_at': now.isoformat(timespec='seconds'),
            'last_result': message,
        }
        if mode == 'auto':
            updates['last_completed_date'] = today
        account = replace(account, **updates)
        await self._store.save(account)
        return WorkflowOutcome(
            status=status,
            actions=tuple(actions),
            message=message,
            run_date=today,
            credentials_fingerprint=credentials_fingerprint(account.credentials),
        )

    async def _retry_resign_after_read_task(
        self,
        client: WorkflowClient,
        error: WendaoBusinessError,
    ) -> tuple[WendaoBusinessError | None, bool]:
        source_id = _read_task_source_id(error.data)
        if not source_id:
            return error, False

        await client.report_post_read(source_id)
        try:
            await client.signin(2)
        except WendaoBusinessError as retry_error:
            if retry_error.code != 4102:
                raise
            return retry_error, True
        return None, True

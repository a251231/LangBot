from __future__ import annotations

import asyncio
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from components.account_store import AccountStore
from components.api_client import WendaoAuthError, WendaoBusinessError, WendaoRetryableError
from components.models import (
    AccountRecord,
    credentials_fingerprint as fingerprint_credentials,
)
from components.workflow import AccountNeedsRebindError, SigninWorkflow, WorkflowOutcome
from components.weekly_report import (
    WeeklyReportNotReadyError,
    WeeklyReportOutcome,
    WeeklyReportService,
    expected_weekly_report_period,
)


RETRY_DELAYS_MINUTES = (5, 15, 30)


class WorkflowRunner(Protocol):
    async def execute(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        mode: str,
    ) -> WorkflowOutcome: ...


class WeeklyReportRunner(Protocol):
    async def execute(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        expected_period: str = '',
    ) -> WeeklyReportOutcome: ...


Notifier = Callable[[AccountRecord, str], Awaitable[None]]
EntitlementChecker = Callable[[AccountRecord], Awaitable[bool]]
ENTITLEMENT_DIAGNOSTIC = '问道自动任务已跳过：权益读取失败，请稍后重试。'


def _parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_schedule_time(value: str) -> time:
    try:
        return datetime.strptime(value, '%H:%M').time()
    except ValueError:
        return time(hour=8)


async def _scheduler_loop(scheduler_ref: weakref.ReferenceType['SigninScheduler']) -> None:
    while True:
        scheduler = scheduler_ref()
        if scheduler is None:
            return
        poll_seconds = scheduler.poll_seconds
        del scheduler
        await asyncio.sleep(poll_seconds)

        scheduler = scheduler_ref()
        if scheduler is None:
            return
        try:
            await scheduler.run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        del scheduler


class SigninScheduler:
    def __init__(
        self,
        store: AccountStore,
        workflow: SigninWorkflow | WorkflowRunner,
        *,
        weekly_report: WeeklyReportService | WeeklyReportRunner | None = None,
        notify: Notifier,
        poll_seconds: int = 30,
        timezone: ZoneInfo | None = None,
        now: Callable[[], datetime] | None = None,
        is_entitled: EntitlementChecker | None = None,
    ) -> None:
        self._store = store
        self._workflow = workflow
        self._weekly_report = weekly_report
        self._notify = notify
        self._is_entitled = is_entitled
        self.poll_seconds = max(5, poll_seconds)
        self._timezone = timezone or ZoneInfo('Asia/Shanghai')
        timezone_value = self._timezone
        self._now = now or (lambda: datetime.now(timezone_value))
        self._poll_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(_scheduler_loop(weakref.ref(self)))

    def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()

    def __del__(self) -> None:
        self.stop()

    def _current_time(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=self._timezone)
        return value.astimezone(self._timezone)

    async def run_once(self) -> None:
        async with self._poll_lock:
            now = self._current_time()
            due_accounts: list[tuple[AccountRecord, bool, bool, str]] = []
            for account in await self._store.list_accounts():
                if not await self._account_is_entitled(account):
                    continue
                normalized = await self._normalize_cross_day_retry(account, now)
                if normalized is None:
                    continue
                normalized = await self._normalize_weekly_retry(normalized, now)
                if normalized is None:
                    continue
                daily_due = self._is_due(normalized, now)
                expected_period = expected_weekly_report_period(now)
                weekly_due = (
                    self._weekly_report is not None
                    and self._is_weekly_due(
                        normalized,
                        now,
                        expected_period=expected_period,
                    )
                )
                if daily_due or weekly_due:
                    due_accounts.append(
                        (normalized, daily_due, weekly_due, expected_period)
                    )
            if due_accounts:
                await asyncio.gather(
                    *(
                        self._run_due_account(
                            account,
                            daily_due=daily_due,
                            weekly_due=weekly_due,
                            expected_period=expected_period,
                        )
                        for account, daily_due, weekly_due, expected_period in due_accounts
                    )
                )

    async def _account_is_entitled(self, account: AccountRecord) -> bool:
        checker = self._is_entitled
        if checker is None:
            return True
        try:
            return bool(await checker(account))
        except Exception:
            await self._record_entitlement_diagnostic(account)
            return False

    async def _record_entitlement_diagnostic(self, account: AccountRecord) -> None:
        try:
            async with self._store.account_lock(account.bot_uuid, account.sender_id):
                latest = await self._store.get(account.bot_uuid, account.sender_id)
                if latest is None or latest.last_result == ENTITLEMENT_DIAGNOSTIC:
                    return
                await self._store.save(
                    replace(latest, last_result=ENTITLEMENT_DIAGNOSTIC)
                )
        except Exception:
            pass

    async def _run_due_account(
        self,
        account: AccountRecord,
        *,
        daily_due: bool,
        weekly_due: bool,
        expected_period: str,
    ) -> None:
        if daily_due:
            await self._run_account(account)
        if not weekly_due or self._weekly_report is None:
            return
        latest = await self._store.get(account.bot_uuid, account.sender_id)
        if latest is None or latest.needs_rebind or not latest.auto_weekly_report:
            return
        if latest.last_weekly_report_period == expected_period:
            return
        await self._run_weekly_report(latest, expected_period)

    async def _normalize_cross_day_retry(
        self,
        account: AccountRecord,
        now: datetime,
    ) -> AccountRecord | None:
        async with self._store.account_lock(account.bot_uuid, account.sender_id):
            current = await self._store.get(account.bot_uuid, account.sender_id)
            if current is None:
                return None
            account = current
            retry_at = _parse_iso_datetime(account.next_retry_at)
            if (
                account.retry_origin_date
                and account.retry_origin_date != now.date().isoformat()
            ):
                account = replace(
                    account,
                    next_retry_at='',
                    retry_count=0,
                    retry_origin_date='',
                )
                await self._store.save(account)
                return account
            if retry_at is None:
                if account.next_retry_at or account.retry_count:
                    account = replace(
                        account,
                        next_retry_at='',
                        retry_count=0,
                        retry_origin_date='',
                    )
                    await self._store.save(account)
                return account
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=self._timezone)
            if retry_at.astimezone(self._timezone).date() < now.date():
                account = replace(
                    account,
                    next_retry_at='',
                    retry_count=0,
                    retry_origin_date='',
                )
                await self._store.save(account)
            return account

    async def _normalize_weekly_retry(
        self,
        account: AccountRecord,
        now: datetime,
    ) -> AccountRecord | None:
        expected_period = expected_weekly_report_period(now)
        async with self._store.account_lock(account.bot_uuid, account.sender_id):
            current = await self._store.get(account.bot_uuid, account.sender_id)
            if current is None:
                return None
            account = current
            retry_at = _parse_iso_datetime(account.weekly_report_next_retry_at)
            wrong_period = (
                bool(account.weekly_report_retry_origin_period)
                and account.weekly_report_retry_origin_period != expected_period
            )
            incomplete_retry = bool(retry_at) != bool(
                account.weekly_report_retry_origin_period
            )
            if wrong_period or incomplete_retry:
                account = replace(
                    account,
                    weekly_report_next_retry_at='',
                    weekly_report_retry_count=0,
                    weekly_report_retry_origin_period='',
                )
                await self._store.save(account)
                return account
            if retry_at is None and (
                account.weekly_report_next_retry_at
                or account.weekly_report_retry_count
            ):
                account = replace(
                    account,
                    weekly_report_next_retry_at='',
                    weekly_report_retry_count=0,
                    weekly_report_retry_origin_period='',
                )
                await self._store.save(account)
            return account

    def _is_due(self, account: AccountRecord, now: datetime) -> bool:
        if account.needs_rebind:
            return False
        if not (account.auto_signin or account.auto_resign or account.auto_milestone):
            return False

        retry_at = _parse_iso_datetime(account.next_retry_at)
        if retry_at is not None:
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=self._timezone)
            return now >= retry_at.astimezone(self._timezone)

        if account.last_completed_date == now.date().isoformat():
            return False
        scheduled = _parse_schedule_time(account.schedule_time)
        return now.time().replace(tzinfo=None) >= scheduled

    def _is_weekly_due(
        self,
        account: AccountRecord,
        now: datetime,
        *,
        expected_period: str,
    ) -> bool:
        if account.needs_rebind or not account.auto_weekly_report:
            return False
        if account.last_weekly_report_period == expected_period:
            return False

        retry_at = _parse_iso_datetime(account.weekly_report_next_retry_at)
        if (
            retry_at is not None
            and account.weekly_report_retry_origin_period == expected_period
        ):
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=self._timezone)
            return now >= retry_at.astimezone(self._timezone)

        if now.weekday() != 0 or now.time().replace(tzinfo=None) < time(hour=9):
            return False
        return account.weekly_report_last_attempt_date != now.date().isoformat()

    async def _run_weekly_report(
        self,
        account: AccountRecord,
        expected_period: str,
    ) -> None:
        assert self._weekly_report is not None
        try:
            outcome = await self._weekly_report.execute(
                account.bot_uuid,
                account.sender_id,
                expected_period=expected_period,
            )
        except WendaoRetryableError as exc:
            await self._handle_weekly_retryable_failure(
                account,
                self._current_time(),
                expected_period=expected_period,
                credentials_fingerprint=exc.credentials_fingerprint,
                report_not_ready=isinstance(exc, WeeklyReportNotReadyError),
            )
            return
        except WendaoAuthError as exc:
            await self._finish_weekly_failure(
                account,
                self._current_time(),
                '问道周报自动推送失败：账号凭据已失效，请重新绑定。',
                needs_rebind=True,
                credentials_fingerprint=exc.credentials_fingerprint,
            )
            return
        except AccountNeedsRebindError:
            await self._finish_weekly_failure(
                account,
                self._current_time(),
                '问道周报自动推送失败：账号凭据已失效，请重新绑定。',
                needs_rebind=True,
            )
            return
        except WendaoBusinessError as exc:
            code_text = f'（业务码 {exc.code}）' if exc.code is not None else ''
            await self._finish_weekly_failure(
                account,
                self._current_time(),
                f'问道周报自动推送失败{code_text}，本周不再重试。',
                credentials_fingerprint=exc.credentials_fingerprint,
            )
            return
        except Exception:
            await self._finish_weekly_failure(
                account,
                self._current_time(),
                '问道周报自动推送最终失败：插件执行异常。',
            )
            return

        if outcome.already_completed:
            return

        now = self._current_time()
        async with self._store.account_lock(account.bot_uuid, account.sender_id):
            latest = await self._store.get(account.bot_uuid, account.sender_id)
            expected_fingerprint = (
                outcome.credentials_fingerprint
                or fingerprint_credentials(account.credentials)
            )
            if (
                latest is None
                or fingerprint_credentials(latest.credentials) != expected_fingerprint
            ):
                return
            completed = replace(
                latest,
                last_weekly_report_period=outcome.report_period,
                weekly_report_next_retry_at='',
                weekly_report_retry_count=0,
                weekly_report_retry_origin_period='',
                weekly_report_last_attempt_date=now.date().isoformat(),
                weekly_report_last_run_at=now.isoformat(timespec='seconds'),
                weekly_report_last_result=outcome.message,
            )
            await self._store.save(completed)
        await self._notify(completed, outcome.message)

    async def _handle_weekly_retryable_failure(
        self,
        account: AccountRecord,
        now: datetime,
        *,
        expected_period: str,
        credentials_fingerprint: str = '',
        report_not_ready: bool = False,
    ) -> None:
        async with self._store.account_lock(account.bot_uuid, account.sender_id):
            latest = await self._store.get(account.bot_uuid, account.sender_id)
            expected_fingerprint = (
                credentials_fingerprint
                or fingerprint_credentials(account.credentials)
            )
            if (
                latest is None
                or fingerprint_credentials(latest.credentials) != expected_fingerprint
            ):
                return
            retry_index = max(0, latest.weekly_report_retry_count)
            if retry_index < len(RETRY_DELAYS_MINUTES):
                delay = RETRY_DELAYS_MINUTES[retry_index]
                retry_at = now + timedelta(minutes=delay)
                reason = '周报尚未更新' if report_not_ready else '网络请求失败'
                updated = replace(
                    latest,
                    weekly_report_next_retry_at=retry_at.isoformat(
                        timespec='seconds'
                    ),
                    weekly_report_retry_count=retry_index + 1,
                    weekly_report_retry_origin_period=expected_period,
                    weekly_report_last_attempt_date=now.date().isoformat(),
                    weekly_report_last_run_at=now.isoformat(timespec='seconds'),
                    weekly_report_last_result=(
                        f'{reason}，已安排 {delay} 分钟后重试。'
                    ),
                )
                await self._store.save(updated)
                return
        message = (
            '问道周报自动推送最终失败：连续重试后仍未更新，本周停止推送。'
            if report_not_ready
            else '问道周报自动推送最终失败：网络请求连续失败，本周停止推送。'
        )
        await self._finish_weekly_failure(
            account,
            now,
            message,
            credentials_fingerprint=credentials_fingerprint,
        )

    async def _finish_weekly_failure(
        self,
        account: AccountRecord,
        now: datetime,
        message: str,
        *,
        needs_rebind: bool = False,
        credentials_fingerprint: str = '',
    ) -> None:
        async with self._store.account_lock(account.bot_uuid, account.sender_id):
            latest = await self._store.get(account.bot_uuid, account.sender_id)
            expected_fingerprint = (
                credentials_fingerprint
                or fingerprint_credentials(account.credentials)
            )
            if (
                latest is None
                or fingerprint_credentials(latest.credentials) != expected_fingerprint
            ):
                return
            completed = replace(
                latest,
                needs_rebind=needs_rebind or latest.needs_rebind,
                weekly_report_next_retry_at='',
                weekly_report_retry_count=0,
                weekly_report_retry_origin_period='',
                weekly_report_last_attempt_date=now.date().isoformat(),
                weekly_report_last_run_at=now.isoformat(timespec='seconds'),
                weekly_report_last_result=message,
            )
            await self._store.save(completed)
        await self._notify(completed, message)

    async def _run_account(self, account: AccountRecord) -> None:
        try:
            outcome = await self._workflow.execute(
                account.bot_uuid,
                account.sender_id,
                mode='auto',
            )
        except WendaoRetryableError as exc:
            await self._handle_retryable_failure(
                account,
                self._current_time(),
                credentials_fingerprint=exc.credentials_fingerprint,
            )
            return
        except WendaoAuthError as exc:
            await self._finish_failure(
                account,
                self._current_time(),
                '问道自动签到失败：账号凭据已失效，请重新绑定。',
                needs_rebind=True,
                credentials_fingerprint=exc.credentials_fingerprint,
            )
            return
        except AccountNeedsRebindError:
            await self._finish_failure(
                account,
                self._current_time(),
                '问道自动签到失败：账号凭据已失效，请重新绑定。',
                needs_rebind=True,
            )
            return
        except WendaoBusinessError as exc:
            code_text = f'（业务码 {exc.code}）' if exc.code is not None else ''
            await self._finish_failure(
                account,
                self._current_time(),
                f'问道自动签到失败{code_text}，本次不再重试。',
                credentials_fingerprint=exc.credentials_fingerprint,
            )
            return
        except Exception:
            await self._finish_failure(
                account,
                self._current_time(),
                '问道自动签到最终失败：插件执行异常。',
            )
            return

        now = self._current_time()
        async with self._store.account_lock(account.bot_uuid, account.sender_id):
            latest = await self._store.get(account.bot_uuid, account.sender_id)
            expected_fingerprint = (
                outcome.credentials_fingerprint
                or fingerprint_credentials(account.credentials)
            )
            if (
                latest is None
                or fingerprint_credentials(latest.credentials) != expected_fingerprint
            ):
                return
            completed = replace(
                latest,
                next_retry_at='',
                retry_count=0,
                retry_origin_date='',
                last_completed_date=outcome.run_date or now.date().isoformat(),
                last_run_at=now.isoformat(timespec='seconds'),
                last_result=outcome.message,
            )
            await self._store.save(completed)
        await self._notify(completed, outcome.message)

    async def _handle_retryable_failure(
        self,
        account: AccountRecord,
        now: datetime,
        *,
        credentials_fingerprint: str = '',
    ) -> None:
        retry_index = max(0, account.retry_count)
        if retry_index < len(RETRY_DELAYS_MINUTES):
            delay = RETRY_DELAYS_MINUTES[retry_index]
            retry_at = now + timedelta(minutes=delay)
            async with self._store.account_lock(account.bot_uuid, account.sender_id):
                latest = await self._store.get(account.bot_uuid, account.sender_id)
                expected_fingerprint = (
                    credentials_fingerprint
                    or fingerprint_credentials(account.credentials)
                )
                if (
                    latest is None
                    or fingerprint_credentials(latest.credentials) != expected_fingerprint
                ):
                    return
                updated = replace(
                    latest,
                    next_retry_at=retry_at.isoformat(timespec='seconds'),
                    retry_count=retry_index + 1,
                    retry_origin_date=(
                        latest.retry_origin_date or now.date().isoformat()
                    ),
                    last_run_at=now.isoformat(timespec='seconds'),
                    last_result=f'网络请求失败，已安排 {delay} 分钟后重试。',
                )
                await self._store.save(updated)
            return
        await self._finish_failure(
            account,
            now,
            '问道自动签到最终失败：网络请求连续失败，今日不再重试。',
        )

    async def _finish_failure(
        self,
        account: AccountRecord,
        now: datetime,
        message: str,
        *,
        needs_rebind: bool = False,
        credentials_fingerprint: str = '',
    ) -> None:
        async with self._store.account_lock(account.bot_uuid, account.sender_id):
            latest = await self._store.get(account.bot_uuid, account.sender_id)
            expected_fingerprint = (
                credentials_fingerprint
                or fingerprint_credentials(account.credentials)
            )
            if (
                latest is None
                or fingerprint_credentials(latest.credentials) != expected_fingerprint
            ):
                return
            completed = replace(
                latest,
                needs_rebind=needs_rebind or latest.needs_rebind,
                next_retry_at='',
                retry_count=0,
                retry_origin_date='',
                last_completed_date=now.date().isoformat(),
                last_run_at=now.isoformat(timespec='seconds'),
                last_result=message,
            )
            await self._store.save(completed)
        await self._notify(completed, message)

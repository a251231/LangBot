from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from components.account_store import AccountStore
from components.api_client import WendaoAuthError, WendaoBusinessError, WendaoRetryableError
from components.models import AccountRecord, Credentials, credentials_fingerprint
from components.scheduler import SigninScheduler
from components.weekly_report import (
    WeeklyReportNotReadyError,
    WeeklyReportOutcome,
)
from components.workflow import WorkflowOutcome


SHANGHAI = ZoneInfo("Asia/Shanghai")


class MemoryPluginStorage:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set_plugin_storage(self, key: str, value: bytes) -> None:
        self.values[key] = value

    async def get_plugin_storage(self, key: str) -> bytes:
        return self.values[key]

    async def get_plugin_storage_keys(self) -> list[str]:
        return list(self.values)

    async def delete_plugin_storage(self, key: str) -> None:
        del self.values[key]


class FailingDiagnosticStorage(MemoryPluginStorage):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_write = False

    async def set_plugin_storage(self, key: str, value: bytes) -> None:
        if self.fail_next_write:
            self.fail_next_write = False
            raise RuntimeError("simulated diagnostic write failure")
        await super().set_plugin_storage(key, value)


class CountingPluginStorage(MemoryPluginStorage):
    def __init__(self) -> None:
        super().__init__()
        self.key_reads = 0

    async def get_plugin_storage_keys(self) -> list[str]:
        self.key_reads += 1
        return await super().get_plugin_storage_keys()


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class RecordingWorkflow:
    def __init__(self, results: list[WorkflowOutcome | Exception]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, str, str]] = []

    async def execute(self, bot_uuid: str, sender_id: str, *, mode: str) -> WorkflowOutcome:
        self.calls.append((bot_uuid, sender_id, mode))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class RecordingWeeklyReport:
    def __init__(self, results: list[WeeklyReportOutcome | Exception]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, str, str]] = []

    async def execute(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        expected_period: str = "",
    ) -> WeeklyReportOutcome:
        self.calls.append((bot_uuid, sender_id, expected_period))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ClockAdvancingWorkflow:
    def __init__(
        self,
        clock: MutableClock,
        *,
        advance: timedelta,
        result: WorkflowOutcome | Exception,
    ) -> None:
        self.clock = clock
        self.advance = advance
        self.result = result

    async def execute(self, bot_uuid: str, sender_id: str, *, mode: str) -> WorkflowOutcome:
        self.clock.value += self.advance
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class RefreshingWorkflow:
    def __init__(self, store: AccountStore, credentials: Credentials) -> None:
        self.store = store
        self.credentials = credentials

    async def execute(self, bot_uuid: str, sender_id: str, *, mode: str) -> WorkflowOutcome:
        account = await self.store.get(bot_uuid, sender_id)
        assert account is not None
        await self.store.save(replace(account, credentials=self.credentials))
        return WorkflowOutcome(
            status={"signInStatus": 2},
            actions=("signin",),
            message="刷新后自动签到完成",
            run_date="2026-07-16",
            credentials_fingerprint=credentials_fingerprint(self.credentials),
        )


class RefreshingFailureWorkflow:
    def __init__(self, store: AccountStore, credentials: Credentials) -> None:
        self.store = store
        self.credentials = credentials

    async def execute(self, bot_uuid: str, sender_id: str, *, mode: str) -> WorkflowOutcome:
        account = await self.store.get(bot_uuid, sender_id)
        assert account is not None
        await self.store.save(replace(account, credentials=self.credentials))
        error = WendaoRetryableError("network after refresh", retryable=True)
        error.credentials_fingerprint = credentials_fingerprint(self.credentials)
        raise error


def run(coro):
    return asyncio.run(coro)


async def _append_async(values: list[str], value: str) -> None:
    values.append(value)


def make_account(**changes) -> AccountRecord:
    base = AccountRecord.create(
        bot_uuid="bot-test-1",
        sender_id="user-test-1",
        credentials=Credentials(
            token="TOKEN_TEST_SCHEDULER",
            device="DEVICE_TEST/Android/16",
            version="2.26.1",
            version_code="260604",
            guest_id="1030000000000000",
            client_type="wd_android",
        ),
        target_id="person-target-1",
        schedule_time="08:00",
        auto_signin=True,
        auto_resign=True,
        auto_milestone=True,
    )
    return replace(base, **changes)


def success(message: str = "自动签到完成") -> WorkflowOutcome:
    return WorkflowOutcome(status={"signInStatus": 2}, actions=("signin",), message=message)


def weekly_success(message: str = "问道周报\n自动推送内容") -> WeeklyReportOutcome:
    return WeeklyReportOutcome(
        report_period="2026年07月06日-2026年07月12日",
        message=message,
    )


def weekly_only_account(**changes) -> AccountRecord:
    return make_account(
        auto_signin=False,
        auto_resign=False,
        auto_milestone=False,
        auto_weekly_report=True,
        **changes,
    )


def test_expired_account_skips_daily_and_weekly_jobs() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        record = make_account(auto_weekly_report=True, last_result="previous result")
        await store.save(record)
        workflow = RecordingWorkflow([])
        weekly_report = RecordingWeeklyReport([])
        notifications: list[str] = []

        async def is_entitled(account: AccountRecord) -> bool:
            return False

        scheduler = SigninScheduler(
            store,
            workflow,
            weekly_report=weekly_report,
            notify=lambda account, text: _append_async(notifications, text),
            now=lambda: datetime(2026, 7, 13, 9, 0, tzinfo=SHANGHAI),
            is_entitled=is_entitled,
        )

        await scheduler.run_once()

        assert workflow.calls == []
        assert weekly_report.calls == []
        assert notifications == []
        assert await store.get(record.bot_uuid, record.sender_id) == record

    run(scenario())


def test_entitlement_read_error_skips_jobs_and_records_diagnostic_only() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        record = make_account(auto_weekly_report=True, last_result="previous result")
        await store.save(record)
        workflow = RecordingWorkflow([])
        weekly_report = RecordingWeeklyReport([])
        notifications: list[str] = []

        async def is_entitled(account: AccountRecord) -> bool:
            raise RuntimeError("simulated entitlement read failure")

        scheduler = SigninScheduler(
            store,
            workflow,
            weekly_report=weekly_report,
            notify=lambda account, text: _append_async(notifications, text),
            now=lambda: datetime(2026, 7, 13, 9, 0, tzinfo=SHANGHAI),
            is_entitled=is_entitled,
        )

        await scheduler.run_once()

        saved = await store.get(record.bot_uuid, record.sender_id)
        assert saved is not None
        assert workflow.calls == []
        assert weekly_report.calls == []
        assert notifications == []
        assert saved.last_result == "问道自动任务已跳过：权益读取失败，请稍后重试。"
        assert saved.last_completed_date == record.last_completed_date
        assert saved.last_run_at == record.last_run_at
        assert saved.next_retry_at == record.next_retry_at
        assert saved.weekly_report_last_attempt_date == record.weekly_report_last_attempt_date

    run(scenario())


def test_diagnostic_write_failure_does_not_block_other_accounts() -> None:
    async def scenario() -> None:
        backend = FailingDiagnosticStorage()
        store = AccountStore(backend)
        first = make_account(sender_id="user-test-1")
        second = make_account(sender_id="user-test-2")
        await store.save(first)
        await store.save(second)
        workflow = RecordingWorkflow([success()])

        async def is_entitled(account: AccountRecord) -> bool:
            if account.sender_id == first.sender_id:
                raise RuntimeError("simulated entitlement read failure")
            return True

        backend.fail_next_write = True
        scheduler = SigninScheduler(
            store,
            workflow,
            notify=lambda account, text: _append_async([], text),
            now=lambda: datetime(2026, 7, 13, 9, 0, tzinfo=SHANGHAI),
            is_entitled=is_entitled,
        )

        await scheduler.run_once()

        assert workflow.calls == [(second.bot_uuid, second.sender_id, "auto")]

    run(scenario())


def test_weekly_report_runs_monday_at_nine_and_pushes_once() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        await store.save(weekly_only_account())
        clock = MutableClock(datetime(2026, 7, 13, 8, 59, tzinfo=SHANGHAI))
        workflow = RecordingWorkflow([])
        weekly_report = RecordingWeeklyReport([weekly_success()])
        notifications: list[str] = []

        async def notify(account: AccountRecord, text: str) -> None:
            notifications.append(text)

        scheduler = SigninScheduler(
            store,
            workflow,
            weekly_report=weekly_report,
            notify=notify,
            now=clock.now,
        )
        await scheduler.run_once()
        assert weekly_report.calls == []

        clock.advance(minutes=1)
        await scheduler.run_once()
        await scheduler.run_once()

        assert weekly_report.calls == [
            (
                "bot-test-1",
                "user-test-1",
                "2026年07月06日-2026年07月12日",
            )
        ]
        assert notifications == ["问道周报\n自动推送内容"]
        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.last_weekly_report_period == "2026年07月06日-2026年07月12日"
        assert saved.weekly_report_last_attempt_date == "2026-07-13"

    run(scenario())


def test_manual_report_period_prevents_duplicate_monday_push_after_restart() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        await store.save(
            weekly_only_account(
                last_weekly_report_period="2026年07月06日-2026年07月12日",
                weekly_report_last_attempt_date="2026-07-12",
            )
        )
        weekly_report = RecordingWeeklyReport([])

        async def notify(account: AccountRecord, text: str) -> None:
            raise AssertionError("同周期不应重复推送")

        restarted = SigninScheduler(
            store,
            RecordingWorkflow([]),
            weekly_report=weekly_report,
            notify=notify,
            now=lambda: datetime(2026, 7, 13, 9, 30, tzinfo=SHANGHAI),
        )
        await restarted.run_once()

        assert weekly_report.calls == []

    run(scenario())


def test_weekly_report_retries_silently_5_15_30_then_stops_for_week() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        await store.save(weekly_only_account())
        clock = MutableClock(datetime(2026, 7, 13, 9, 0, tzinfo=SHANGHAI))
        weekly_report = RecordingWeeklyReport(
            [
                WeeklyReportNotReadyError("stale-1", retryable=True),
                WendaoRetryableError("network-2", retryable=True),
                WendaoRetryableError("network-3", retryable=True),
                WendaoRetryableError("network-final", retryable=True),
            ]
        )
        notifications: list[str] = []

        async def notify(account: AccountRecord, text: str) -> None:
            notifications.append(text)

        scheduler = SigninScheduler(
            store,
            RecordingWorkflow([]),
            weekly_report=weekly_report,
            notify=notify,
            now=clock.now,
        )

        await scheduler.run_once()
        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.weekly_report_retry_count == 1
        assert saved.weekly_report_next_retry_at.endswith("09:05:00+08:00")
        assert notifications == []

        clock.advance(minutes=5)
        await scheduler.run_once()
        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.weekly_report_retry_count == 2
        assert saved.weekly_report_next_retry_at.endswith("09:20:00+08:00")

        clock.advance(minutes=15)
        await scheduler.run_once()
        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.weekly_report_retry_count == 3
        assert saved.weekly_report_next_retry_at.endswith("09:50:00+08:00")

        clock.advance(minutes=30)
        await scheduler.run_once()
        await scheduler.run_once()
        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.weekly_report_retry_count == 0
        assert saved.weekly_report_next_retry_at == ""
        assert saved.last_weekly_report_period == ""
        assert saved.weekly_report_last_attempt_date == "2026-07-13"
        assert len(weekly_report.calls) == 4
        assert len(notifications) == 1
        assert "最终失败" in notifications[0]

    run(scenario())


def test_new_week_discards_old_weekly_retry_and_runs_new_period() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        await store.save(
            weekly_only_account(
                weekly_report_next_retry_at="2026-07-13T09:20:00+08:00",
                weekly_report_retry_count=2,
                weekly_report_retry_origin_period=(
                    "2026年07月06日-2026年07月12日"
                ),
                weekly_report_last_attempt_date="2026-07-13",
            )
        )
        weekly_report = RecordingWeeklyReport(
            [
                WeeklyReportOutcome(
                    report_period="2026年07月13日-2026年07月19日",
                    message="问道周报\n新一周",
                )
            ]
        )

        async def notify(account: AccountRecord, text: str) -> None:
            pass

        scheduler = SigninScheduler(
            store,
            RecordingWorkflow([]),
            weekly_report=weekly_report,
            notify=notify,
            now=lambda: datetime(2026, 7, 20, 9, 0, tzinfo=SHANGHAI),
        )
        await scheduler.run_once()

        assert weekly_report.calls == [
            (
                "bot-test-1",
                "user-test-1",
                "2026年07月13日-2026年07月19日",
            )
        ]
        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.weekly_report_retry_count == 0
        assert saved.last_weekly_report_period == "2026年07月13日-2026年07月19日"

    run(scenario())


def test_daily_and_weekly_due_share_one_poll_and_both_notify() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        await store.save(make_account())
        workflow = RecordingWorkflow([success()])
        weekly_report = RecordingWeeklyReport([weekly_success()])
        notifications: list[str] = []

        async def notify(account: AccountRecord, text: str) -> None:
            notifications.append(text)

        scheduler = SigninScheduler(
            store,
            workflow,
            weekly_report=weekly_report,
            notify=notify,
            now=lambda: datetime(2026, 7, 13, 9, 0, tzinfo=SHANGHAI),
        )
        await scheduler.run_once()

        assert len(workflow.calls) == 1
        assert len(weekly_report.calls) == 1
        assert notifications == ["自动签到完成", "问道周报\n自动推送内容"]

    run(scenario())


def test_weekly_auth_error_marks_rebind_and_does_not_retry() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        await store.save(weekly_only_account())
        weekly_report = RecordingWeeklyReport(
            [WendaoAuthError("expired", retryable=False)]
        )
        notifications: list[str] = []

        async def notify(account: AccountRecord, text: str) -> None:
            notifications.append(text)

        scheduler = SigninScheduler(
            store,
            RecordingWorkflow([]),
            weekly_report=weekly_report,
            notify=notify,
            now=lambda: datetime(2026, 7, 13, 9, 0, tzinfo=SHANGHAI),
        )
        await scheduler.run_once()
        await scheduler.run_once()

        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.needs_rebind is True
        assert saved.weekly_report_retry_count == 0
        assert len(weekly_report.calls) == 1
        assert len(notifications) == 1
        assert "重新绑定" in notifications[0]

    run(scenario())


def test_weekly_retry_survives_scheduler_restart() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        await store.save(
            weekly_only_account(
                weekly_report_next_retry_at="2026-07-13T09:05:00+08:00",
                weekly_report_retry_count=1,
                weekly_report_retry_origin_period=(
                    "2026年07月06日-2026年07月12日"
                ),
                weekly_report_last_attempt_date="2026-07-13",
            )
        )
        weekly_report = RecordingWeeklyReport([weekly_success("问道周报\n重试成功")])
        notifications: list[str] = []

        async def notify(account: AccountRecord, text: str) -> None:
            notifications.append(text)

        restarted = SigninScheduler(
            store,
            RecordingWorkflow([]),
            weekly_report=weekly_report,
            notify=notify,
            now=lambda: datetime(2026, 7, 13, 9, 5, tzinfo=SHANGHAI),
        )
        await restarted.run_once()

        assert len(weekly_report.calls) == 1
        assert notifications == ["问道周报\n重试成功"]
        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.weekly_report_retry_count == 0
        assert saved.weekly_report_next_retry_at == ""

    run(scenario())


def test_scheduler_obeys_custom_time_and_notifies_once_on_success() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        await store.save(make_account(schedule_time="09:15"))
        clock = MutableClock(datetime(2026, 7, 16, 9, 14, tzinfo=SHANGHAI))
        workflow = RecordingWorkflow([success()])
        notifications: list[tuple[AccountRecord, str]] = []

        async def notify(account: AccountRecord, text: str) -> None:
            notifications.append((account, text))

        scheduler = SigninScheduler(store, workflow, notify=notify, now=clock.now)
        await scheduler.run_once()
        assert workflow.calls == []

        clock.advance(minutes=1)
        await scheduler.run_once()
        await scheduler.run_once()

        assert len(workflow.calls) == 1
        assert [text for _, text in notifications] == ["自动签到完成"]
        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.last_completed_date == "2026-07-16"

    run(scenario())


def test_scheduler_accepts_credentials_refreshed_inside_workflow() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        original = make_account()
        await store.save(original)
        refreshed = replace(
            original.credentials,
            token="ACCESS_TOKEN_SCHEDULER_REFRESHED",
            refresh_token="REFRESH_TOKEN_SCHEDULER_REFRESHED",
            access_token_valid_time=1784288728233,
        )
        workflow = RefreshingWorkflow(store, refreshed)
        notifications: list[str] = []

        async def notify(account: AccountRecord, text: str) -> None:
            notifications.append(text)

        scheduler = SigninScheduler(
            store,
            workflow,
            notify=notify,
            now=lambda: datetime(2026, 7, 16, 8, 0, tzinfo=SHANGHAI),
        )

        await scheduler.run_once()

        saved = await store.get(original.bot_uuid, original.sender_id)
        assert saved is not None
        assert saved.credentials == refreshed
        assert saved.last_completed_date == "2026-07-16"
        assert notifications == ["刷新后自动签到完成"]

    run(scenario())


def test_scheduler_schedules_retry_after_workflow_refreshes_credentials() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        original = make_account()
        await store.save(original)
        refreshed = replace(
            original.credentials,
            token="ACCESS_TOKEN_SCHEDULER_RETRY_NEW",
            refresh_token="REFRESH_TOKEN_SCHEDULER_RETRY_NEW",
            access_token_valid_time=1784288728233,
        )
        workflow = RefreshingFailureWorkflow(store, refreshed)

        async def notify(account: AccountRecord, text: str) -> None:
            raise AssertionError("中间重试不应通知")

        scheduler = SigninScheduler(
            store,
            workflow,
            notify=notify,
            now=lambda: datetime(2026, 7, 16, 8, 0, tzinfo=SHANGHAI),
        )

        await scheduler.run_once()

        saved = await store.get(original.bot_uuid, original.sender_id)
        assert saved is not None
        assert saved.credentials == refreshed
        assert saved.retry_count == 1
        assert saved.next_retry_at.endswith("08:05:00+08:00")

    run(scenario())


def test_background_loop_waits_one_poll_before_first_storage_access() -> None:
    async def scenario() -> None:
        backend = CountingPluginStorage()
        store = AccountStore(backend)
        workflow = RecordingWorkflow([])

        async def notify(account: AccountRecord, text: str) -> None:
            pass

        scheduler = SigninScheduler(store, workflow, notify=notify, poll_seconds=5)
        scheduler.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert backend.key_reads == 0
        scheduler.stop()
        await asyncio.sleep(0)

    run(scenario())


def test_retryable_failures_back_off_5_15_30_minutes_then_notify_final_failure() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        await store.save(make_account())
        clock = MutableClock(datetime(2026, 7, 16, 8, 0, tzinfo=SHANGHAI))
        workflow = RecordingWorkflow(
            [
                WendaoRetryableError("network-1", retryable=True),
                WendaoRetryableError("network-2", retryable=True),
                WendaoRetryableError("network-3", retryable=True),
                WendaoRetryableError("network-final", retryable=True),
            ]
        )
        notifications: list[str] = []

        async def notify(account: AccountRecord, text: str) -> None:
            notifications.append(text)

        scheduler = SigninScheduler(store, workflow, notify=notify, now=clock.now)

        await scheduler.run_once()
        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.retry_count == 1
        assert saved.next_retry_at.endswith("08:05:00+08:00")
        assert notifications == []

        clock.advance(minutes=5)
        await scheduler.run_once()
        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.retry_count == 2
        assert saved.next_retry_at.endswith("08:20:00+08:00")

        clock.advance(minutes=15)
        await scheduler.run_once()
        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.retry_count == 3
        assert saved.next_retry_at.endswith("08:50:00+08:00")

        clock.advance(minutes=30)
        await scheduler.run_once()
        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.retry_count == 0
        assert saved.next_retry_at == ""
        assert saved.last_completed_date == "2026-07-16"
        assert len(notifications) == 1
        assert "最终失败" in notifications[0]

    run(scenario())


def test_retry_delay_uses_actual_failure_time_after_queue_wait() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        await store.save(make_account())
        clock = MutableClock(datetime(2026, 7, 16, 8, 0, tzinfo=SHANGHAI))
        workflow = ClockAdvancingWorkflow(
            clock,
            advance=timedelta(minutes=7),
            result=WendaoRetryableError("late failure", retryable=True),
        )

        async def notify(account: AccountRecord, text: str) -> None:
            pass

        scheduler = SigninScheduler(store, workflow, notify=notify, now=clock.now)
        await scheduler.run_once()

        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.next_retry_at.endswith("08:12:00+08:00")
        assert saved.last_run_at.endswith("08:07:00+08:00")

    run(scenario())


def test_completion_keeps_workflow_start_date_when_run_crosses_midnight() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        await store.save(make_account(schedule_time="23:59"))
        clock = MutableClock(datetime(2026, 7, 16, 23, 59, tzinfo=SHANGHAI))
        workflow = ClockAdvancingWorkflow(
            clock,
            advance=timedelta(minutes=2),
            result=WorkflowOutcome(
                status={"signInStatus": 2},
                actions=("signin",),
                message="自动签到完成",
                run_date="2026-07-16",
            ),
        )

        async def notify(account: AccountRecord, text: str) -> None:
            pass

        scheduler = SigninScheduler(store, workflow, notify=notify, now=clock.now)
        await scheduler.run_once()

        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.last_completed_date == "2026-07-16"
        assert saved.last_run_at.endswith("00:01:00+08:00")

    run(scenario())


def test_retry_persistence_does_not_resurrect_unbound_account() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        original = make_account()
        await store.save(original)
        workflow = RecordingWorkflow([])

        async def notify(account: AccountRecord, text: str) -> None:
            pass

        scheduler = SigninScheduler(
            store,
            workflow,
            notify=notify,
            now=lambda: datetime(2026, 7, 16, 8, 0, tzinfo=SHANGHAI),
        )
        await store.delete(original.bot_uuid, original.sender_id)

        await scheduler._handle_retryable_failure(
            original,
            datetime(2026, 7, 16, 8, 0, tzinfo=SHANGHAI),
        )

        assert await store.get(original.bot_uuid, original.sender_id) is None

    run(scenario())


def test_pending_retry_survives_scheduler_restart() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        await store.save(
            make_account(
                retry_count=1,
                next_retry_at="2026-07-16T08:05:00+08:00",
            )
        )
        clock = MutableClock(datetime(2026, 7, 16, 8, 5, tzinfo=SHANGHAI))
        workflow = RecordingWorkflow([success("重试成功")])
        notifications: list[str] = []

        async def notify(account: AccountRecord, text: str) -> None:
            notifications.append(text)

        restarted = SigninScheduler(store, workflow, notify=notify, now=clock.now)
        await restarted.run_once()

        assert len(workflow.calls) == 1
        assert notifications == ["重试成功"]
        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.retry_count == 0
        assert saved.next_retry_at == ""

    run(scenario())


def test_new_shanghai_day_discards_old_retry_and_runs_fresh_schedule() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        await store.save(
            make_account(
                retry_count=3,
                next_retry_at="2026-07-15T23:55:00+08:00",
                last_completed_date="2026-07-15",
            )
        )
        clock = MutableClock(datetime(2026, 7, 16, 8, 0, tzinfo=SHANGHAI))
        workflow = RecordingWorkflow([success()])

        async def notify(account: AccountRecord, text: str) -> None:
            pass

        scheduler = SigninScheduler(store, workflow, notify=notify, now=clock.now)
        await scheduler.run_once()

        assert len(workflow.calls) == 1
        saved = await store.get("bot-test-1", "user-test-1")
        assert saved.retry_count == 0
        assert saved.last_completed_date == "2026-07-16"

    run(scenario())


def test_retry_crossing_midnight_waits_for_new_day_schedule() -> None:
    async def scenario() -> None:
        store = AccountStore(MemoryPluginStorage())
        await store.save(
            make_account(
                retry_count=1,
                retry_origin_date="2026-07-15",
                next_retry_at="2026-07-16T00:03:00+08:00",
                last_run_at="2026-07-15T23:58:00+08:00",
            )
        )
        clock = MutableClock(datetime(2026, 7, 16, 0, 3, tzinfo=SHANGHAI))
        workflow = RecordingWorkflow([success()])

        async def notify(account: AccountRecord, text: str) -> None:
            pass

        scheduler = SigninScheduler(store, workflow, notify=notify, now=clock.now)
        await scheduler.run_once()

        assert workflow.calls == []
        reset = await store.get("bot-test-1", "user-test-1")
        assert reset.retry_count == 0
        assert reset.next_retry_at == ""

        clock.advance(hours=7, minutes=57)
        await scheduler.run_once()
        assert len(workflow.calls) == 1

    run(scenario())


def test_auth_and_business_errors_do_not_retry() -> None:
    async def scenario(error: Exception) -> tuple[AccountRecord, list[str]]:
        store = AccountStore(MemoryPluginStorage())
        await store.save(make_account())
        workflow = RecordingWorkflow([error])
        notifications: list[str] = []

        async def notify(account: AccountRecord, text: str) -> None:
            notifications.append(text)

        scheduler = SigninScheduler(
            store,
            workflow,
            notify=notify,
            now=lambda: datetime(2026, 7, 16, 8, 0, tzinfo=SHANGHAI),
        )
        await scheduler.run_once()
        return await store.get("bot-test-1", "user-test-1"), notifications

    auth_saved, auth_notifications = run(
        scenario(WendaoAuthError("expired", retryable=False))
    )
    assert auth_saved.retry_count == 0
    assert auth_saved.needs_rebind is True
    assert len(auth_notifications) == 1

    business_saved, business_notifications = run(
        scenario(WendaoBusinessError("bad request", retryable=False, code=4004))
    )
    assert business_saved.retry_count == 0
    assert business_saved.last_completed_date == "2026-07-16"
    assert len(business_notifications) == 1

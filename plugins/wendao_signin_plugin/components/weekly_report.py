from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import html
import re
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from components.account_session import AuthenticatedAccountSession
from components.account_store import AccountStore
from components.api_client import WendaoBusinessError
from components.models import (
    AccountRecord,
    ApiResponse,
    Credentials,
    credentials_fingerprint,
)
from components.weekly_report_client import (
    WeeklyReportClient,
    WeeklyReportNotReadyError,
    WeeklyReportTicket,
    WeeklyReportTokenError,
    parse_weekly_report_ticket,
)


_HTML_TAG_RE = re.compile(r'<[^>]*>')
_WHITESPACE_RE = re.compile(r'\s+')
TAO_DAYS_PER_YEAR = 360


class WeeklyCommunityClient(Protocol):
    async def get_third_url(self) -> ApiResponse: ...

    async def refresh_access_token(self) -> Credentials: ...

    async def aclose(self) -> None: ...


class WeeklyActivityClient(Protocol):
    async def fetch(self, ticket: WeeklyReportTicket) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class WeeklyReportOutcome:
    report_period: str
    message: str
    credentials_fingerprint: str = ''
    already_completed: bool = False


def expected_weekly_report_period(now: datetime) -> str:
    current_monday = now.date() - timedelta(days=now.weekday())
    previous_monday = current_monday - timedelta(days=7)
    previous_sunday = current_monday - timedelta(days=1)
    return (
        f'{previous_monday:%Y年%m月%d日}'
        f'-{previous_sunday:%Y年%m月%d日}'
    )


def _plain_text(value: Any) -> str:
    without_tags = _HTML_TAG_RE.sub('', str(value or ''))
    return _WHITESPACE_RE.sub(' ', html.unescape(without_tags)).strip()


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _format_tao_days(value: Any) -> str:
    text = _plain_text(value)
    try:
        total_days = int(text)
    except ValueError:
        return text
    if total_days < 0:
        return text
    years, days = divmod(total_days, TAO_DAYS_PER_YEAR)
    if years and days:
        return f'{years}年{days}天'
    if years:
        return f'{years}年'
    return f'{days}天'


def format_weekly_report(payload: dict[str, Any]) -> str:
    user = _mapping(payload.get('user'))
    overview = _mapping(payload.get('overview'))
    lines = ['问道周报']

    role_parts = [
        _plain_text(user.get('role_name')),
        _plain_text(user.get('zone_name')),
    ]
    level = _plain_text(user.get('level'))
    if level:
        role_parts.append(f'{level}级')
    role_parts = [part for part in role_parts if part]
    if role_parts:
        lines.append('｜'.join(role_parts))

    period = _plain_text(user.get('week'))
    if period:
        lines.append(f'周期：{period}')
    login_days = _plain_text(overview.get('login_days'))
    if login_days:
        lines.append(f'登录天数：{login_days}天')
    cumulative_activity = _plain_text(overview.get('cum_active'))
    activity_percent = _plain_text(overview.get('active_over'))
    if cumulative_activity:
        suffix = f'（达成 {activity_percent}%）' if activity_percent else ''
        lines.append(f'累计活跃：{cumulative_activity}{suffix}')
    brush_times = _plain_text(overview.get('shuad_times'))
    brush_percent = _plain_text(overview.get('shuad_times_over'))
    if brush_times:
        suffix = f'（超过 {brush_percent}% 道友）' if brush_percent else ''
        lines.append(f'刷道次数：{brush_times}{suffix}')
    tao_add = _format_tao_days(overview.get('tao_add'))
    if tao_add:
        lines.append(f'道行增长：{tao_add}')

    highlights = payload.get('highlights')
    if isinstance(highlights, list):
        formatted_highlights: list[str] = []
        for item in highlights:
            if not isinstance(item, dict):
                continue
            title = _plain_text(item.get('title'))
            contents = item.get('content')
            if isinstance(contents, list):
                content = ' '.join(
                    part for value in contents if (part := _plain_text(value))
                )
            else:
                content = _plain_text(contents)
            if title and content:
                formatted_highlights.append(f'{title}：{content}')
            elif title or content:
                formatted_highlights.append(title or content)
        if formatted_highlights:
            lines.append('精彩时刻：')
            lines.extend(formatted_highlights)

    luck_words = _plain_text(_mapping(payload.get('luckwords')).get('words'))
    if luck_words:
        lines.append(f'本周签语：{luck_words}')
    return '\n'.join(lines)


class WeeklyReportService:
    def __init__(
        self,
        store: AccountStore,
        *,
        session: AuthenticatedAccountSession[WeeklyCommunityClient],
        activity_client_factory: Callable[[], WeeklyActivityClient] | None = None,
        timezone: ZoneInfo | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._session = session
        self._activity_client_factory = (
            activity_client_factory or (lambda: WeeklyReportClient())
        )
        self._timezone = timezone or ZoneInfo('Asia/Shanghai')
        timezone_value = self._timezone
        self._now = now or (lambda: datetime.now(timezone_value))

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
        expected_period: str = '',
    ) -> WeeklyReportOutcome:
        return await self._session.execute(
            bot_uuid,
            sender_id,
            lambda account, client: self._run_with_client(
                account,
                client,
                expected_period=expected_period,
            ),
        )

    @staticmethod
    async def _exchange_ticket(
        client: WeeklyCommunityClient,
    ) -> WeeklyReportTicket:
        response = await client.get_third_url()
        action_url = response.data.get('actionUrl')
        if not isinstance(action_url, str) or not action_url.strip():
            raise WendaoBusinessError(
                '问道周报换票响应格式错误。',
                retryable=False,
            )
        return parse_weekly_report_ticket(action_url)

    async def _run_with_client(
        self,
        account: AccountRecord,
        client: WeeklyCommunityClient,
        *,
        expected_period: str,
    ) -> WeeklyReportOutcome:
        if (
            expected_period
            and account.last_weekly_report_period == expected_period
        ):
            return WeeklyReportOutcome(
                report_period=expected_period,
                message=(
                    account.weekly_report_last_result
                    or '问道周报\n本周期已完成推送。'
                ),
                credentials_fingerprint=credentials_fingerprint(
                    account.credentials
                ),
                already_completed=True,
            )

        activity_client = self._activity_client_factory()
        try:
            ticket = await self._exchange_ticket(client)
            try:
                payload = await activity_client.fetch(ticket)
            except WeeklyReportTokenError:
                ticket = await self._exchange_ticket(client)
                payload = await activity_client.fetch(ticket)
        finally:
            await activity_client.aclose()

        user = _mapping(payload.get('user'))
        report_period = _plain_text(user.get('week'))
        if not report_period:
            raise WendaoBusinessError('问道周报缺少报告周期。', retryable=False)
        if expected_period and report_period != expected_period:
            raise WeeklyReportNotReadyError(
                '问道周报尚未更新到本周周期。',
                retryable=True,
            )

        now = self._current_time()
        message = format_weekly_report(payload)
        updated = replace(
            account,
            last_weekly_report_period=report_period,
            weekly_report_next_retry_at='',
            weekly_report_retry_count=0,
            weekly_report_retry_origin_period='',
            weekly_report_last_attempt_date=now.date().isoformat(),
            weekly_report_last_run_at=now.isoformat(timespec='seconds'),
            weekly_report_last_result=message,
        )
        await self._store.save(updated)
        return WeeklyReportOutcome(
            report_period=report_period,
            message=message,
            credentials_fingerprint=credentials_fingerprint(updated.credentials),
        )

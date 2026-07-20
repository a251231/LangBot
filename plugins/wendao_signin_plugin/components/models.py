from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Credentials:
    token: str
    device: str
    version: str
    version_code: str
    guest_id: str
    client_type: str
    refresh_token: str = ''
    access_token_valid_time: int = 0


def credentials_fingerprint(credentials: Credentials) -> str:
    values = (
        credentials.token,
        credentials.device,
        credentials.version,
        credentials.version_code,
        credentials.guest_id,
        credentials.client_type,
        credentials.refresh_token,
        str(credentials.access_token_valid_time),
    )
    return hashlib.sha256('\0'.join(values).encode('utf-8')).hexdigest()


@dataclass(frozen=True, slots=True)
class ApiResponse:
    code: int
    data: dict[str, Any]
    message: str = ''
    timestamp: int | None = None


@dataclass(frozen=True, slots=True)
class AccountRecord:
    bot_uuid: str
    sender_id: str
    credentials: Credentials
    target_type: str
    target_id: str
    schedule_time: str
    auto_signin: bool
    auto_resign: bool
    auto_milestone: bool
    auto_weekly_report: bool = True
    nickname: str = ''
    user_identifier: str = ''
    needs_rebind: bool = False
    next_retry_at: str = ''
    retry_count: int = 0
    retry_origin_date: str = ''
    last_completed_date: str = ''
    last_resign_attempt_date: str = ''
    last_run_at: str = ''
    last_result: str = ''
    last_weekly_report_period: str = ''
    weekly_report_next_retry_at: str = ''
    weekly_report_retry_count: int = 0
    weekly_report_retry_origin_period: str = ''
    weekly_report_last_attempt_date: str = ''
    weekly_report_last_run_at: str = ''
    weekly_report_last_result: str = ''

    @classmethod
    def create(
        cls,
        *,
        bot_uuid: str,
        sender_id: str,
        credentials: Credentials,
        target_id: str,
        schedule_time: str,
        auto_signin: bool,
        auto_resign: bool,
        auto_milestone: bool,
        auto_weekly_report: bool = True,
        nickname: str = '',
        user_identifier: str = '',
    ) -> 'AccountRecord':
        return cls(
            bot_uuid=bot_uuid,
            sender_id=sender_id,
            credentials=credentials,
            target_type='person',
            target_id=target_id,
            schedule_time=schedule_time,
            auto_signin=auto_signin,
            auto_resign=auto_resign,
            auto_milestone=auto_milestone,
            auto_weekly_report=auto_weekly_report,
            nickname=nickname,
            user_identifier=user_identifier,
        )

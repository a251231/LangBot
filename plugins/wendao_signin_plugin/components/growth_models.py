from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PromoterRecord:
    schema_version: int = field(default=1, init=False)
    bot_uuid: str
    identity_hash: str
    invite_code: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ReferralRecord:
    schema_version: int = field(default=1, init=False)
    bot_uuid: str
    invitee_hash: str
    promoter_hash: str
    invite_code: str
    status: str
    created_at: str
    bound_at: str = ''
    effective_at: str = ''
    user_identifier_hash: str = ''


@dataclass(frozen=True, slots=True)
class PointAccount:
    schema_version: int = field(default=1, init=False)
    bot_uuid: str
    identity_hash: str
    balance: int = 0
    last_entry_id: str = ''
    updated_at: str = ''


@dataclass(frozen=True, slots=True)
class PointEntry:
    schema_version: int = field(default=1, init=False)
    bot_uuid: str
    entry_id: str
    identity_hash: str
    amount: int
    reason: str
    operation_id: str
    balance_after: int
    created_at: str
    previous_entry_id: str = ''


@dataclass(frozen=True, slots=True)
class ProductRecord:
    schema_version: int = field(default=1, init=False)
    bot_uuid: str
    product_id: str
    name: str
    points_cost: int
    duration_days: int
    enabled: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CardRecord:
    schema_version: int = field(default=1, init=False)
    bot_uuid: str
    card_hash: str
    product_id: str
    product_name: str
    duration_days: int
    status: str
    encrypted_code: str
    created_at: str
    issued_to_hash: str = ''
    issued_at: str = ''
    activated_by_hash: str = ''
    activated_at: str = ''


@dataclass(frozen=True, slots=True)
class RedemptionRecord:
    schema_version: int = field(default=1, init=False)
    bot_uuid: str
    redemption_id: str
    identity_hash: str
    product_id: str
    card_hash: str
    points_cost: int
    duration_days: int
    created_at: str


@dataclass(frozen=True, slots=True)
class EntitlementRecord:
    schema_version: int = field(default=1, init=False)
    bot_uuid: str
    identity_hash: str
    expires_at: str
    created_at: str
    updated_at: str
    trial_started_at: str = ''


@dataclass(frozen=True, slots=True)
class GrowthOperation:
    schema_version: int = field(default=1, init=False)
    bot_uuid: str
    operation_id: str
    kind: str
    status: str
    payload: dict[str, object]
    applied_steps: tuple[str, ...]
    created_at: str
    updated_at: str


GrowthRecord = (
    PromoterRecord
    | ReferralRecord
    | PointAccount
    | PointEntry
    | ProductRecord
    | CardRecord
    | RedemptionRecord
    | EntitlementRecord
    | GrowthOperation
)

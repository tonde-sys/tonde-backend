"""Validated QAPE persistence inputs. Thresholds are supplied by policy owners."""
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Limits(StrictModel):
    bucket_capacity: int = Field(ge=1)
    bucket_refill_seconds: float = Field(gt=0)
    max_requests_per_10m: int = Field(ge=1)
    max_tickets_per_day: int = Field(ge=1)


class Risk(StrictModel):
    review_threshold: int = Field(ge=1)
    cooldown_seconds: int = Field(ge=1)
    hard_block_seconds: int = Field(ge=1)


class Counting(StrictModel):
    timezone: str

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value


class StaffOverride(StrictModel):
    enabled: bool
    require_reason: bool


class PolicyConfig(StrictModel):
    limits: Limits
    risk: Risk
    counting: Counting
    staff_override: StaffOverride
    redis_unavailable: Literal["ALLOW", "REVIEW_REQUIRED", "TEMPORARY_BLOCK"] = "REVIEW_REQUIRED"
    # Counting rule: every committed ticket counts, including DONE, CANCELLED,
    # ABSENT. Transfer does not create another ticket; count original service.
    # bucket_refill_seconds is the time to recharge ONE token.


class PolicyInput(StrictModel):
    org_id: str
    service_id: str | None = None
    version: int = Field(ge=1)
    enabled: bool
    config: PolicyConfig
    created_by: str | None = None


class RestrictionInput(StrictModel):
    org_id: str
    service_id: str | None = None
    user_id: str
    restriction_type: str = Field(min_length=1, max_length=40)
    reason_code: str = Field(min_length=1, max_length=80)
    starts_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_dates(self):
        if self.starts_at.tzinfo is None or self.expires_at.tzinfo is None or self.expires_at <= self.starts_at:
            raise ValueError("timezone-aware expires_at must be after starts_at")
        return self


_SENSITIVE = {"otp", "jwt", "token", "secret", "phone", "password", "authorization", "email", "ip"}


def _safe(value: Any) -> bool:
    if isinstance(value, dict):
        return all(isinstance(k, str) and not any(word in k.lower() for word in _SENSITIVE)
                   and _safe(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_safe(v) for v in value)
    return value is None or isinstance(value, (str, int, float, bool))


class EventInput(StrictModel):
    org_id: str
    service_id: str | None = None
    user_id: str | None = None
    ticket_id: str | None = None
    device_id: str | None = None
    kiosk_id: str | None = None
    request_id: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=50)
    decision: str | None = None
    reason_codes: list[str]
    policy_version: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def audit_safety(self):
        if self.event_type == "TICKET_CREATED" and (not self.ticket_id or not self.service_id or not self.user_id):
            raise ValueError("ticket_id, service_id and user_id required for TICKET_CREATED")
        if self.event_type == "TICKET_CREATED" and self.decision != "ALLOW":
            raise ValueError("live TICKET_CREATED requires ALLOW")
        if self.device_id and (len(self.device_id) != 64 or any(c not in "0123456789abcdef" for c in self.device_id)):
            raise ValueError("device_id must be a SHA-256 hexadecimal digest")
        # No free-form metadata until an allowlisted audit contract is approved.
        if self.metadata or not _safe(self.reason_codes) or any(
            not code or len(code) > 64 or not all(ch.isupper() or ch.isdigit() or ch == "_" for ch in code)
            for code in self.reason_codes
        ):
            raise ValueError("audit fields contain sensitive or unsupported data")
        return self

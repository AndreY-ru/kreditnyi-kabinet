"""Модель данных (раздел 7 исследования).

Принципы:
- статус заявки меняется только через app.domain.transitions;
- внешние ответы и скоринг хранятся как есть (аудит и воспроизводимость);
- Application.version — оптимистичная блокировка (параллельные решения аналитиков).
Суммы хранятся в целых рублях.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


_clock_offset = timedelta(0)  # меняется только в сидах, чтобы демо-заявки «жили» в прошлом


def utcnow() -> datetime:
    return datetime.now(timezone.utc) + _clock_offset


@contextmanager
def clock_shift(delta: timedelta) -> Iterator[None]:
    """Сдвинуть «текущее время» приложения (для демо-данных). В рабочем коде не используется."""
    global _clock_offset
    prev, _clock_offset = _clock_offset, delta
    try:
        yield
    finally:
        _clock_offset = prev


TS = DateTime(timezone=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))  # CLIENT, ANALYST, ADMIN
    position: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    memberships: Mapped[list[CompanyMember]] = relationship(back_populates="user")

    @property
    def initials(self) -> str:
        parts = [p for p in self.full_name.split() if p]
        return "".join(p[0] for p in parts[:2]).upper()

    @property
    def first_name(self) -> str:
        return self.full_name.split()[0] if self.full_name else ""


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    inn: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    ogrn: Mapped[str | None] = mapped_column(String(15))
    kind: Mapped[str] = mapped_column(String(10))  # IP, OOO
    name: Mapped[str] = mapped_column(String(500))
    registered_at: Mapped[date | None] = mapped_column(Date)
    okved: Mapped[str | None] = mapped_column(String(20))
    okved_name: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE")  # ACTIVE, LIQUIDATING, REORGANIZING
    address: Mapped[str | None] = mapped_column(String(500))
    director: Mapped[str | None] = mapped_column(String(255))
    founders: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    employees: Mapped[int | None] = mapped_column(Integer)
    egrul_fetched_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    members: Mapped[list[CompanyMember]] = relationship(back_populates="company")

    def business_months(self, today: date | None = None) -> int | None:
        if not self.registered_at:
            return None
        today = today or date.today()
        months = (today.year - self.registered_at.year) * 12 + (today.month - self.registered_at.month)
        if today.day < self.registered_at.day:
            months -= 1
        return max(months, 0)


class CompanyMember(Base):
    __tablename__ = "company_members"
    __table_args__ = (UniqueConstraint("user_id", "company_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    position: Mapped[str] = mapped_column(String(255))
    can_apply: Mapped[bool] = mapped_column(Boolean, default=True)
    can_sign: Mapped[bool] = mapped_column(Boolean, default=False)
    authority: Mapped[str | None] = mapped_column(String(255))  # основание полномочий

    user: Mapped[User] = relationship(back_populates="memberships")
    company: Mapped[Company] = relationship(back_populates="members")


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(40))  # LOAN, CREDIT_LINE, OVERDRAFT
    description: Mapped[str] = mapped_column(Text, default="")
    goals: Mapped[list[str]] = mapped_column(JSONType, default=list)
    min_amount: Mapped[int] = mapped_column(BigInteger)
    max_amount: Mapped[int] = mapped_column(BigInteger)
    min_term: Mapped[int] = mapped_column(Integer)
    max_term: Mapped[int] = mapped_column(Integer)
    min_business_months: Mapped[int] = mapped_column(Integer)
    requires_collateral: Mapped[bool] = mapped_column(Boolean, default=False)
    unsecured_max: Mapped[int | None] = mapped_column(BigInteger)  # без залога — не больше этой суммы
    requires_guarantor: Mapped[bool] = mapped_column(Boolean, default=False)
    base_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    auto_limit: Mapped[int] = mapped_column(BigInteger, default=0)  # 0 = только ручной разбор
    decision_days: Mapped[int] = mapped_column(Integer, default=2)
    gov_support: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


class ScoringModel(Base):
    """Версионированная конфигурация скоринговой карты. Активна одна версия."""

    __tablename__ = "scoring_models"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(20), unique=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONType)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class Application(Base):
    __tablename__ = "applications"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str | None] = mapped_column(String(30), unique=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[int] = mapped_column(BigInteger)
    term_months: Mapped[int] = mapped_column(Integer)
    goal: Mapped[str] = mapped_column(String(40))
    has_collateral: Mapped[bool] = mapped_column(Boolean, default=False)
    collateral_desc: Mapped[str | None] = mapped_column(String(500))
    has_guarantor: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(30), index=True, default="DRAFT")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_version: Mapped[int] = mapped_column(Integer, default=0)
    validation_errors: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    flags: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(TS)

    __mapper_args__ = {"version_id_col": version}

    company: Mapped[Company] = relationship()
    product: Mapped[Product] = relationship()
    author: Mapped[User] = relationship()
    financials: Mapped[FinancialIndicators | None] = relationship(
        back_populates="application", uselist=False, cascade="all, delete-orphan"
    )
    liabilities: Mapped[list[Liability]] = relationship(
        back_populates="application", cascade="all, delete-orphan", order_by="Liability.position"
    )
    history: Mapped[list[StatusHistory]] = relationship(order_by="StatusHistory.id")


class FinancialIndicators(Base):
    __tablename__ = "financial_indicators"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), unique=True)
    period_label: Mapped[str] = mapped_column(String(100), default="Последние 12 месяцев")
    revenue: Mapped[int | None] = mapped_column(BigInteger)
    expenses: Mapped[int | None] = mapped_column(BigInteger)
    ebit: Mapped[int | None] = mapped_column(BigInteger)
    amortization: Mapped[int | None] = mapped_column(BigInteger)
    revenue_trend: Mapped[str | None] = mapped_column(String(20))  # GROWTH, FLAT, DECLINE
    employees: Mapped[int | None] = mapped_column(Integer)

    application: Mapped[Application] = relationship(back_populates="financials")


class Liability(Base):
    __tablename__ = "liabilities"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    creditor: Mapped[str] = mapped_column(String(255))
    balance: Mapped[int] = mapped_column(BigInteger)
    monthly_payment: Mapped[int] = mapped_column(BigInteger, default=0)
    maturity: Mapped[str | None] = mapped_column(String(20))

    application: Mapped[Application] = relationship(back_populates="liabilities")


class ApplicationSnapshot(Base):
    __tablename__ = "application_snapshots"
    __table_args__ = (UniqueConstraint("application_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class Consent(Base):
    __tablename__ = "consents"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # PD, BKI, BKI_GUARANTOR
    subject_name: Mapped[str] = mapped_column(String(500))
    purpose: Mapped[str] = mapped_column(String(500))
    bureau: Mapped[str | None] = mapped_column(String(255))
    given_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    given_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    valid_until: Mapped[datetime | None] = mapped_column(TS)
    text_version: Mapped[str] = mapped_column(String(20))
    text_sha256: Mapped[str] = mapped_column(String(64))
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TS)

    def is_active(self, at: datetime | None = None) -> bool:
        at = at or utcnow()
        if self.revoked_at is not None:
            return False
        return self.valid_until is None or _aware(self.valid_until) > at


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class InfoRequest(Base):
    __tablename__ = "info_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    doc_kind: Mapped[str] = mapped_column(String(255))
    comment: Mapped[str] = mapped_column(Text, default="")
    due_at: Mapped[datetime] = mapped_column(TS)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")  # OPEN, ANSWERED, EXPIRED
    answer_comment: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime | None] = mapped_column(TS)
    answered_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    info_request_id: Mapped[int | None] = mapped_column(ForeignKey("info_requests.id"))
    kind: Mapped[str] = mapped_column(String(255))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(String(500))
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class ExternalCheck(Base):
    __tablename__ = "external_checks"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    source: Mapped[str] = mapped_column(String(30))  # FNS_MOCK, BKI_MOCK
    endpoint: Mapped[str] = mapped_column(String(255))
    request: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    http_status: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class ScoringResult(Base):
    __tablename__ = "scoring_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    model_version: Mapped[str] = mapped_column(String(20))
    score: Mapped[int] = mapped_column(Integer)
    base: Mapped[int] = mapped_column(Integer)
    zone: Mapped[str] = mapped_column(String(10))  # HIGH, GRAY, LOW
    factors: Mapped[list[dict[str, Any]]] = mapped_column(JSONType)
    knockouts: Mapped[list[dict[str, Any]]] = mapped_column(JSONType)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(30))  # APPROVED_AUTO, APPROVED, COUNTEROFFER, DECLINED, MANUAL_REVIEW, NEEDS_INFO
    source: Mapped[str] = mapped_column(String(10))  # SYSTEM, ANALYST
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reason_codes: Mapped[list[str]] = mapped_column(JSONType, default=list)
    review_reasons: Mapped[list[str] | None] = mapped_column(JSONType)
    conditions: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    comment: Mapped[str | None] = mapped_column(Text)
    explanation: Mapped[str | None] = mapped_column(Text)
    explanation_source: Mapped[str | None] = mapped_column(String(20))  # TEMPLATE, LLM
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Offer(Base):
    __tablename__ = "offers"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decisions.id"))
    amount: Mapped[int] = mapped_column(BigInteger)
    term_months: Mapped[int] = mapped_column(Integer)
    rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    monthly_payment: Mapped[int] = mapped_column(BigInteger)
    valid_until: Mapped[datetime] = mapped_column(TS)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")  # ACTIVE, ACCEPTED, REJECTED, EXPIRED
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    responded_at: Mapped[datetime | None] = mapped_column(TS)


class StatusHistory(Base):
    __tablename__ = "status_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30))
    event: Mapped[str] = mapped_column(String(100))
    actor_type: Mapped[str] = mapped_column(String(10))  # SYSTEM, CLIENT, ANALYST
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    actor_name: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)


class ReviewTask(Base):
    __tablename__ = "review_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    assigned_to: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    assigned_at: Mapped[datetime | None] = mapped_column(TS)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(500))
    due_at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(TS)

    application: Mapped[Application] = relationship()
    assignee: Mapped[User | None] = relationship()


class Contract(Base):
    __tablename__ = "contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), unique=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"))
    number: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="READY")  # READY, SIGNED
    signed_at: Mapped[datetime | None] = mapped_column(TS)
    signed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    simulated: Mapped[bool] = mapped_column(Boolean, default=True)


class Disbursement(Base):
    __tablename__ = "disbursements"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), unique=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    account_masked: Mapped[str] = mapped_column(String(40))
    first_payment_date: Mapped[date] = mapped_column(Date)
    disbursed_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    simulated: Mapped[bool] = mapped_column(Boolean, default=True)


class Job(Base):
    """Транзакционный outbox: задача создаётся в той же транзакции, что и смена статуса."""

    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = mapped_column(TS, default=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "scope", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    scope: Mapped[str] = mapped_column(String(100))
    key: Mapped[str] = mapped_column(String(100))
    response: Mapped[dict[str, Any]] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(100))
    entity: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Goal = Literal["working", "equipment", "cash_gap", "new_direction", "refinance"]
Trend = Literal["GROWTH", "FLAT", "DECLINE"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Error(BaseModel):
    error: str
    message: str
    details: Any | None = None


class LoginIn(BaseModel):
    email: str
    password: str


class UserOut(ORM):
    id: int
    email: str
    full_name: str
    role: str
    position: str | None = None


class ProductOut(ORM):
    id: int
    code: str
    name: str
    kind: str
    description: str
    goals: list[str]
    min_amount: int
    max_amount: int
    min_term: int
    max_term: int
    min_business_months: int
    requires_collateral: bool
    unsecured_max: int | None
    requires_guarantor: bool
    base_rate: Decimal
    auto_limit: int
    decision_days: int
    gov_support: bool
    version: int


class MatchIn(BaseModel):
    goal: Goal
    amount: int = Field(gt=0)
    term_months: int = Field(gt=0, le=120)
    has_collateral: bool = False
    has_guarantor: bool = True
    company_id: int | None = None


class MatchOut(BaseModel):
    product: ProductOut
    eligible: bool
    fit: int
    reasons: list[str]
    warnings: list[str]


class CompanyOut(ORM):
    id: int
    inn: str
    ogrn: str | None
    kind: str
    name: str
    registered_at: date | None
    okved: str | None
    okved_name: str | None
    status: str
    address: str | None
    director: str | None
    employees: int | None


class CompanyIn(BaseModel):
    inn: str = Field(pattern=r"^\d{10}(\d{2})?$")
    position: str = "Представитель"


class SelfCheckItem(BaseModel):
    title: str
    ok: bool | None
    text: str


class PrecheckIn(BaseModel):
    goal: Goal = "working"
    amount: int = Field(gt=0)
    term_months: int = Field(gt=0, le=120)
    has_collateral: bool = False
    has_guarantor: bool = True
    revenue: int | None = Field(default=None, ge=0, description="Не указано — подставим из прошлой заявки или ФНС")
    profit: int | None = Field(default=None, description="Прибыль до процентов и налогов, примерно")
    debt: int | None = Field(default=None, ge=0, description="Остаток по действующим кредитам и лизингу")
    revenue_trend: Trend | None = None
    overdues: Literal["none", "minor", "current"] = Field(
        default="none", description="Просрочки за 2 года со слов клиента; в БКИ до согласия не ходим")


class PrecheckFactor(BaseModel):
    title: str
    tone: Literal["ok", "warn", "bad", "na"]
    text: str


class PrecheckProduct(BaseModel):
    product_id: int
    name: str
    verdict: Literal["auto", "review", "low", "unfit"]
    notes: list[str]


class PrecheckOut(BaseModel):
    level: Literal["high", "medium", "low"]
    factors: list[PrecheckFactor]
    products: list[PrecheckProduct]
    tips: list[str]
    comfortable_amount: int | None


class IntakeIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000, description="Задача своими словами")


class IntakeOut(BaseModel):
    goal: Goal | None
    amount: int | None
    term_months: int | None
    has_collateral: bool | None
    has_guarantor: bool | None
    source: Literal["llm", "rules"] = Field(description="llm — модель (числа сверены с текстом), rules — правила")
    missing: list[str]
    summary: str


class MemberIn(BaseModel):
    email: str
    position: str = "Сотрудник"
    can_apply: bool = True
    can_sign: bool = False


class MemberOut(BaseModel):
    user_id: int
    full_name: str
    position: str
    can_apply: bool
    can_sign: bool


class ApplicationCreate(BaseModel):
    company_id: int
    product_id: int
    amount: int = Field(gt=0)
    term_months: int = Field(gt=0, le=120)
    goal: Goal
    has_collateral: bool = False
    has_guarantor: bool = True


class ApplicationPatch(BaseModel):
    product_id: int | None = None
    amount: int | None = Field(default=None, gt=0)
    term_months: int | None = Field(default=None, gt=0, le=120)
    goal: Goal | None = None
    has_collateral: bool | None = None
    collateral_desc: str | None = None
    has_guarantor: bool | None = None


class LiabilityIn(BaseModel):
    creditor: str = Field(min_length=1, max_length=255)
    balance: int = Field(ge=0)
    monthly_payment: int = Field(default=0, ge=0)
    maturity: str | None = None


class FinancialsIn(BaseModel):
    revenue: int | None = Field(default=None, ge=0)
    expenses: int | None = Field(default=None, ge=0)
    ebit: int | None = None
    amortization: int | None = Field(default=None, ge=0)
    revenue_trend: Trend | None = None
    employees: int | None = Field(default=None, ge=0)
    liabilities: list[LiabilityIn] | None = None


class ConsentIn(BaseModel):
    kind: Literal["PD", "BKI", "BKI_GUARANTOR"]


class ConsentOut(ORM):
    id: int
    kind: str
    subject_name: str
    purpose: str
    bureau: str | None
    given_at: datetime
    valid_until: datetime | None
    text_version: str
    text_sha256: str
    simulated: bool


class OfferOut(ORM):
    id: int
    amount: int
    term_months: int
    rate: Decimal
    monthly_payment: int
    valid_until: datetime
    status: str


class HistoryOut(ORM):
    from_status: str | None
    to_status: str
    event: str
    actor_type: str
    actor_name: str
    reason: str | None
    created_at: datetime


class ReasonOut(BaseModel):
    code: str
    title: str
    text: str
    action: str


class ApplicationOut(ORM):
    id: int
    number: str | None
    status: str
    status_label: str
    version: int
    company_id: int
    product_id: int
    product_name: str
    amount: int
    term_months: int
    goal: str
    has_collateral: bool
    has_guarantor: bool
    snapshot_version: int
    created_at: datetime
    submitted_at: datetime | None
    validation_errors: list[dict[str, Any]] | None = None
    offer: OfferOut | None = None
    reasons: list[ReasonOut] = []
    explanation: str | None = None
    info_request: dict[str, Any] | None = None


class SubmitOut(BaseModel):
    application_id: int
    number: str
    status: str
    snapshot_version: int


class DecisionIn(BaseModel):
    kind: Literal["approve", "counter", "info", "decline"]
    expected_version: int
    comment: str = Field(min_length=1)
    amount: int | None = None
    term_months: int | None = None
    rate: Decimal | None = None
    reasons: list[str] = []
    doc_kind: str | None = None
    info_comment: str | None = None
    due_days: int | None = Field(default=None, ge=1, le=30)


class QueueItemOut(BaseModel):
    application_id: int
    number: str | None
    company: str
    inn: str
    product: str
    amount: int
    term_months: int
    status: str
    score: int | None
    zone: str | None
    reason: str
    assigned_to: int | None
    due_at: datetime | None
    overdue: bool


class ScoringModelIn(BaseModel):
    version: str = Field(pattern=r"^v\d+(\.\d+)*$")
    config: dict[str, Any]


class ProductPatch(BaseModel):
    base_rate: Decimal | None = Field(default=None, gt=0, lt=100)
    auto_limit: int | None = Field(default=None, ge=0)
    active: bool | None = None


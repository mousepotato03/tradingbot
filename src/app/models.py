from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Nonnegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class Rating(StrEnum):
    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"
    DEFER = "판단 보류"


class CandidateState(StrEnum):
    UNIVERSE = "UNIVERSE"
    RESEARCH = "RESEARCH_CANDIDATE"
    WATCH = "WATCH_CANDIDATE"
    ENTRY = "ENTRY_CANDIDATE"
    ACTIVE = "ACTIVE_POSITION"
    EXITED = "EXITED"
    INVALIDATED = "INVALIDATED"


class Security(Model):
    ticker: str
    name: str
    exchange: str
    country: str
    currency: str
    asset_type: str = "equity"
    issuer_id: str | None = None
    sector: str | None = None

    @field_validator("ticker")
    @classmethod
    def ticker_valid(cls, value: str) -> str:
        import re

        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", value):
            raise ValueError("Invalid US equity ticker")
        return value


class RiskInputs(Model):
    portfolio_value: Positive
    currency: str
    max_loss_fraction: Annotated[Decimal, Field(gt=0, le=1)]
    max_position_fraction: Annotated[Decimal, Field(gt=0, le=1)]
    max_sector_fraction: Annotated[Decimal, Field(gt=0, le=1)] | None = None
    min_reward_risk: Positive | None = None
    slippage_fraction: Annotated[Decimal, Field(ge=0, lt=1)]
    tax_fraction: Annotated[Decimal, Field(ge=0, lt=1)]
    fx_rate: Positive | None = None
    fx_evidence_id: str | None = None
    max_pair_correlation: Annotated[Decimal, Field(gt=0, le=1)] | None = None


class ResearchRequest(Model):
    ticker: str
    as_of: datetime | None = None
    mode: Literal["quick", "normal", "deep", "critical"] = "deep"
    investor_status: str = "신규 진입 검토"
    horizon: str | None = None
    question: str | None = None
    risk: RiskInputs | None = None
    report_policy: Literal["changes", "always", "none"] = "changes"

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return Security.ticker_valid(value)

    @field_validator("as_of")
    @classmethod
    def aware_as_of(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("as_of must include a timezone")
        return value


class NumericFact(Model):
    name: str
    value: Decimal
    unit: str
    currency: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    accounting_basis: str | None = None


class EvidenceRecord(Model):
    evidence_id: str = Field(default_factory=new_id)
    ticker: str
    evidence_type: str
    source_name: str
    source_tier: Annotated[int, Field(ge=1, le=5)]
    source_url: str
    published_at: datetime | None = None
    effective_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utcnow)
    content_hash: str
    raw_reference: str | None = None
    text: str = ""
    facts: list[NumericFact] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    usable_as_fact: bool = True
    stale_after_seconds: int | None = None
    parser_version: str = "1"
    warnings: list[str] = Field(default_factory=list)

    @field_validator("published_at", "effective_at", "retrieved_at")
    @classmethod
    def aware_times(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Evidence timestamps require timezone")
        return value

    @model_validator(mode="after")
    def provenance(self):
        if not self.source_url or not self.content_hash or not self.source_name:
            raise ValueError("Evidence requires source, URL and content hash")
        if self.published_at and self.published_at > self.retrieved_at:
            raise ValueError("Publication timestamp is after retrieval")
        if self.effective_at and self.effective_at > self.retrieved_at:
            raise ValueError("Effective timestamp is after retrieval")
        return self


class NumericReference(Model):
    value: Decimal
    unit: str
    evidence_id: str
    fact_name: str


class Claim(Model):
    classification: Literal["FACT", "INTERPRETATION", "ASSUMPTION"]
    text: str
    evidence_ids: list[str]
    numeric_references: list[NumericReference] = Field(default_factory=list)
    confidence: Literal["낮음", "중간", "높음"]

    @model_validator(mode="after")
    def fact_needs_evidence(self):
        if self.classification == "FACT" and not self.evidence_ids:
            raise ValueError("FACT requires evidence")
        return self


class AnalysisSection(Model):
    name: str
    claims: list[Claim]
    limitations: list[str]


class ResearchSummary(Model):
    sections: list[AnalysisSection]
    unresolved_questions: list[str]
    material_gaps: list[str]
    evidence_sufficient: bool


class StageReview(Model):
    claims: list[Claim]
    arguments: list[str]
    rebuttals: list[str]
    early_warnings: list[str]
    material_gaps: list[str]


class PlanLevel(Model):
    value: Positive
    evidence_ids: list[str]
    basis: str
    classification: Literal["INTERPRETATION", "ASSUMPTION"]


class TradePlan(Model):
    currency: str
    entry_low: PlanLevel
    entry_high: PlanLevel
    stop: PlanLevel
    targets: list[PlanLevel]
    conditions: list[str]
    invalidation: list[str]
    horizon: str
    quantity: Positive | None = None
    reward_risk: Positive | None = None
    no_trade_conditions: list[str]


class PortfolioDecision(Model):
    rating: Rating
    confidence: Literal["낮음", "중간", "높음"]
    new_entry_action: str
    holder_action: str
    executive_summary: str
    thesis: list[Claim]
    thesis_state: Literal["ACTIVE", "WEAKENED", "INVALIDATED", "UNKNOWN"]
    invalidation_conditions: list[str]
    monitoring_checklist: list[str]
    material_changes: list[Claim]
    material_gaps: list[str]
    trade_plan: TradePlan | None


class ValidationIssue(Model):
    code: str
    field: str
    message: str


class ValidationResult(Model):
    valid: bool
    issues: list[ValidationIssue]
    calculations: dict[str, str]


class ResearchReport(Model):
    run_id: str
    ticker: str
    as_of: datetime
    created_at: datetime
    fixture: bool
    security: Security | None
    research: ResearchSummary
    reviews: dict[str, StageReview]
    decision: PortfolioDecision
    validation: ValidationResult
    rejected_decisions: list[PortfolioDecision]
    evidence_ids: list[str]
    candidate_state: CandidateState
    previous_report_id: str | None
    tool_calls: int
    limitations: list[str]

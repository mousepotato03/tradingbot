import secrets
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


# Short, typed evidence IDs: models copy "ta-3f9a1c0b7d" far more reliably than a UUID, and the
# prefix keeps a technical fact from being cited against the quote record. 40 random bits.
EVIDENCE_PREFIXES = {
    "identity": "id",
    "quote": "qt",
    "ohlcv": "px",
    "technical": "ta",
    "portfolio": "pf",
    "fees": "fee",
    "filings": "fil",
    "financials": "fin",
    "fund_holdings": "fund",
    "search": "srch",
    "document": "doc",
    "browser": "web",
    "calculation": "calc",
    "extracted_fact": "xf",
    "quotation": "quo",
    "screening": "scr",
}


def new_evidence_id(evidence_type: str = "") -> str:
    return f"{EVIDENCE_PREFIXES.get(evidence_type, 'ev')}-{secrets.token_hex(5)}"


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


class NewEntryAction(StrEnum):
    """New-entry actions from the canonical research standard (section 4)."""

    ENTER_NOW = "ENTER_NOW"
    CONDITIONAL_ENTRY = "CONDITIONAL_ENTRY"
    STAGED_ENTRY = "STAGED_ENTRY"
    WAIT = "WAIT"
    AVOID = "AVOID"
    DEFER = "DEFER"


class HolderAction(StrEnum):
    """Existing-holder actions from the canonical research standard (section 4)."""

    ADD = "ADD"
    HOLD = "HOLD"
    TRIM = "TRIM"
    PROTECT_PROFIT = "PROTECT_PROFIT"
    EXIT = "EXIT"
    DEFER = "DEFER"


ACTION_LABELS = {
    NewEntryAction.ENTER_NOW: "지금 진입",
    NewEntryAction.CONDITIONAL_ENTRY: "조건부 진입",
    NewEntryAction.STAGED_ENTRY: "분할 진입",
    NewEntryAction.WAIT: "관망",
    NewEntryAction.AVOID: "진입 회피",
    NewEntryAction.DEFER: "판단 보류",
    HolderAction.ADD: "추가 매수",
    HolderAction.HOLD: "유지",
    HolderAction.TRIM: "일부 축소",
    HolderAction.PROTECT_PROFIT: "이익 보호",
    HolderAction.EXIT: "청산",
    HolderAction.DEFER: "판단 보류",
}


# Marks free-text actions from reports written before the action enums existed.
LEGACY_NOTE_PREFIX = "(이전 형식 자유 서술) "


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
    asset_type: Literal["equity", "etf"] = "equity"
    security_type: str | None = None
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
    # What changed since the previous report when a monitor or discovery queues the run.
    trigger: str | None = None

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
    evidence_id: str = Field(default_factory=new_evidence_id)
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

    @model_validator(mode="before")
    @classmethod
    def typed_id(cls, data):
        if isinstance(data, dict) and not data.get("evidence_id"):
            data = {**data, "evidence_id": new_evidence_id(str(data.get("evidence_type", "")))}
        return data

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


class QuoteReference(Model):
    """Exact source span that grounds a qualitative FACT."""

    evidence_id: str
    text: str = Field(min_length=1, max_length=600)


class Claim(Model):
    classification: Literal["FACT", "INTERPRETATION", "ASSUMPTION"]
    text: str
    evidence_ids: list[str]
    numeric_references: list[NumericReference] = Field(default_factory=list)
    # Defaults keep pre-quote reports loadable; strict model contracts still require the field.
    quotes: list[QuoteReference] = Field(default_factory=list)
    confidence: Literal["낮음", "중간", "높음"]

    @model_validator(mode="after")
    def fact_needs_evidence(self):
        if self.classification == "FACT" and not self.evidence_ids:
            raise ValueError("FACT requires evidence")
        return self


class MaterialGap(Model):
    """A missing, stale or conflicting input.

    Only the research manager and portfolio manager can make a gap binding. The same field on
    other stages is a proposed severity that those two roles must adjudicate.
    """

    description: str
    severity: Literal["blocking", "non_blocking"]

    @model_validator(mode="before")
    @classmethod
    def legacy_text(cls, data):
        # Reports written before severity existed stored bare strings.
        if isinstance(data, str):
            return {"description": data, "severity": "non_blocking"}
        return data


class AnalysisSection(Model):
    name: str
    claims: list[Claim]
    limitations: list[str]


class ResearchSummary(Model):
    sections: list[AnalysisSection]
    unresolved_questions: list[str]
    material_gaps: list[MaterialGap]
    evidence_sufficient: bool


class StageReview(Model):
    claims: list[Claim]
    arguments: list[str]
    rebuttals: list[str]
    early_warnings: list[str]
    material_gaps: list[MaterialGap]


class ResearchManagerReview(StageReview):
    """The research manager's binding sufficiency assessment after the debate stages."""

    evidence_sufficient: bool


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
    # whole_share: quantity/limit order in whole shares.
    # fractional_amount: US market order by dollar amount (Toss orderAmount); fill is not a limit.
    sizing_unit: Literal["whole_share", "fractional_amount"] = "whole_share"


class PositionGuard(Model):
    """Levels that protect an existing holding; the monitor checks them every minute.

    stop: price at or below which the holding is reviewed for exit (loss limit or broken
    structure). take_profit: optional prices above the current price to review a trim.
    """

    currency: str
    stop: PlanLevel
    take_profit: list[PlanLevel]
    rationale: str


class PortfolioDecision(Model):
    rating: Rating
    confidence: Literal["낮음", "중간", "높음"]
    new_entry_action: NewEntryAction
    new_entry_note: str = ""
    holder_action: HolderAction
    holder_note: str = ""
    executive_summary: str
    thesis: list[Claim]
    thesis_state: Literal["ACTIVE", "WEAKENED", "INVALIDATED", "UNKNOWN"]
    invalidation_conditions: list[str]
    monitoring_checklist: list[str]
    material_changes: list[Claim]
    material_gaps: list[MaterialGap]
    trade_plan: TradePlan | None
    # Required for a held position unless the decision is to exit; None for non-holders.
    position_guard: PositionGuard | None = None

    @model_validator(mode="before")
    @classmethod
    def legacy_actions(cls, data):
        """Load reports written with free-text actions and string gaps without guessing intent.

        Legacy free text is preserved as the note and the action becomes DEFER; the old decision
        is not re-validated on load. Legacy PM gaps always forced 판단 보류, so they were blocking.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for field, note, enum in (
            ("new_entry_action", "new_entry_note", NewEntryAction),
            ("holder_action", "holder_note", HolderAction),
        ):
            value = data.get(field)
            # Only the legacy shape (no note field) is converted; new outputs must use the enum.
            if (
                note not in data
                and isinstance(value, str)
                and value not in {item.value for item in enum}
            ):
                data[field] = enum.DEFER.value
                data[note] = LEGACY_NOTE_PREFIX + value
        data["material_gaps"] = [
            {"description": gap, "severity": "blocking"} if isinstance(gap, str) else gap
            for gap in data.get("material_gaps", [])
        ]
        return data

    def blocking_gaps(self) -> list[MaterialGap]:
        return [gap for gap in self.material_gaps if gap.severity == "blocking"]


class TriageDecision(Model):
    """Cheap discovery classification; never an investment rating."""

    priority: Literal["deep_research", "watch_later", "skip"]
    reasons: list[str]
    evidence_ids: list[str]


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
    reviews: dict[str, ResearchManagerReview | StageReview]
    decision: PortfolioDecision
    validation: ValidationResult
    rejected_decisions: list[PortfolioDecision]
    evidence_ids: list[str]
    candidate_state: CandidateState
    previous_report_id: str | None
    tool_calls: int
    limitations: list[str]
    # Current committee draft (reviews may be partial on budget exhaustion); old reports load.
    trade_proposal: PortfolioDecision | None = None
    trade_proposal_validation: ValidationResult | None = None
    # A held position keeps the last validated guard until a new one validates; the decision
    # itself is unchanged. inherited_guard_from names the report that validated it.
    inherited_guard: PositionGuard | None = None
    inherited_guard_from: str | None = None

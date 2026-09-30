from datetime import datetime
from decimal import Decimal

from app.analytics import position_size, reward_risk
from app.evidence import evidence_is_fresh, validate_claims
from app.models import (
    EvidenceRecord,
    PortfolioDecision,
    Rating,
    ResearchRequest,
    ValidationIssue,
    ValidationResult,
)


def validate_decision(
    decision: PortfolioDecision,
    records: list[EvidenceRecord],
    request: ResearchRequest,
    at: datetime,
) -> ValidationResult:
    issues = validate_claims(decision.thesis + decision.material_changes, records, at)
    calculations: dict[str, str] = {}

    def error(code, field, message):
        issues.append(ValidationIssue(code=code, field=field, message=message))

    if decision.material_gaps and decision.rating != Rating.DEFER:
        error("MATERIAL_GAP", "rating", "Material evidence gaps require 판단 보류")
    if decision.rating == Rating.HOLD and not decision.thesis:
        error("UNJUSTIFIED_HOLD", "rating", "Hold needs an evidence-backed explanation")
    plan = decision.trade_plan
    if plan is None:
        return ValidationResult(valid=not issues, issues=issues, calculations=calculations)
    if decision.rating == Rating.DEFER:
        error("DEFERRED_PLAN", "trade_plan", "Deferred decisions cannot carry precise trade plans")
    by_id = {record.evidence_id: record for record in records}
    quotes = [r for r in records if r.evidence_type == "quote" and r.ticker == request.ticker]
    if not quotes or not evidence_is_fresh(quotes[-1], at):
        error("STALE_QUOTE", "trade_plan", "Fresh quote required for precise levels")
    elif quotes[-1].payload.get("currency") != plan.currency:
        error("CURRENCY", "trade_plan", "Quote and trade plan currency differ")
    for field, level in [
        ("entry_low", plan.entry_low),
        ("entry_high", plan.entry_high),
        ("stop", plan.stop),
    ] + [(f"target_{i}", t) for i, t in enumerate(plan.targets)]:
        if not level.evidence_ids or not level.basis.strip():
            error("LEVEL_BASIS", field, "Precise levels need evidence and derivation")
        for evidence_id in level.evidence_ids:
            record = by_id.get(evidence_id)
            if record is None or not record.usable_as_fact or record.retrieved_at > at:
                error("LEVEL_EVIDENCE", field, "Invalid evidence for precise level")
        supported = [
            fact
            for evidence_id in level.evidence_ids
            if evidence_id in by_id
            for fact in by_id[evidence_id].facts
            if fact.value == level.value
            and fact.unit == f"{plan.currency}/share"
            and by_id[evidence_id].ticker == request.ticker
        ]
        if not supported:
            error(
                "UNSUPPORTED_LEVEL", field, "Level must match an observed or calculated price fact"
            )
    entry, stop = plan.entry_high.value, plan.stop.value
    if plan.entry_low.value > entry or stop >= plan.entry_low.value:
        error("GEOMETRY", "entry", "Stop must be below the entire entry range")
    if not plan.targets:
        error("MISSING_TARGET", "targets", "Precise long plan requires a target")
    for i, target in enumerate(plan.targets):
        try:
            rr = reward_risk(entry, stop, target.value)
            calculations[f"reward_risk_{i}"] = str(rr)
            if (
                i == 0
                and plan.reward_risk is not None
                and abs(plan.reward_risk - rr) > Decimal("0.0001")
            ):
                error(
                    "RR_MISMATCH",
                    "reward_risk",
                    "Proposed RR differs from deterministic calculation",
                )
            if request.risk and request.risk.min_reward_risk and rr < request.risk.min_reward_risk:
                error("RR_LIMIT", "targets", "Reward/risk is below the supplied minimum")
        except ValueError:
            error("GEOMETRY", f"target_{i}", "Target must be above long entry")
    if plan.quantity is not None:
        portfolios = [r for r in records if r.evidence_type == "portfolio"]
        fees = [r for r in records if r.evidence_type == "fees"]
        risk = request.risk
        if risk is None or not portfolios or not fees:
            error(
                "SIZING_INPUTS",
                "quantity",
                "Sizing requires explicit risk, portfolio and fee inputs",
            )
        elif not evidence_is_fresh(portfolios[-1], at) or not evidence_is_fresh(fees[-1], at):
            error("STALE_PORTFOLIO", "quantity", "Sizing inputs are stale")
        elif not portfolios[-1].payload.get("complete"):
            error(
                "INCOMPLETE_PORTFOLIO",
                "quantity",
                "Cannot size against incomplete account exposure",
            )
        else:
            portfolio, fee = portfolios[-1].payload, fees[-1].payload
            if risk.currency != plan.currency or portfolio.get("currency") != plan.currency:
                error(
                    "CURRENCY", "quantity", "Sizing currency must match risk and account valuation"
                )
            else:
                try:
                    existing = sum(
                        (
                            Decimal(str(h["market_value"]))
                            for h in portfolio["holdings"]
                            if h["ticker"] == request.ticker
                        ),
                        Decimal(0),
                    )
                    maximum = position_size(
                        risk.portfolio_value,
                        risk.max_loss_fraction,
                        entry,
                        stop,
                        Decimal(str(portfolio["buying_power"])),
                        risk.max_position_fraction,
                        existing,
                        Decimal(str(fee["rate"])),
                        risk.slippage_fraction,
                        risk.tax_fraction,
                    )
                    calculations["max_quantity"] = str(maximum)
                    if plan.quantity > maximum:
                        error(
                            "SIZE_LIMIT",
                            "quantity",
                            "Quantity exceeds validated risk/cash/concentration limit",
                        )
                    if risk.max_sector_fraction is not None:
                        sector = portfolio.get("security_sector")
                        if not sector or any(
                            h.get("sector") is None for h in portfolio["holdings"]
                        ):
                            error(
                                "UNKNOWN_SECTOR",
                                "quantity",
                                "Sector exposure required by risk policy",
                            )
                        else:
                            exposure = sum(
                                (
                                    Decimal(str(h["market_value"]))
                                    for h in portfolio["holdings"]
                                    if h["sector"] == sector
                                ),
                                Decimal(0),
                            )
                            if (
                                exposure + plan.quantity * entry
                                > risk.portfolio_value * risk.max_sector_fraction
                            ):
                                error(
                                    "SECTOR_LIMIT",
                                    "quantity",
                                    "Sector concentration limit exceeded",
                                )
                    if risk.max_pair_correlation is not None:
                        correlations = portfolio.get("correlations")
                        if correlations is None:
                            error(
                                "UNKNOWN_CORRELATION",
                                "quantity",
                                "Correlation evidence required by risk policy",
                            )
                        elif any(
                            Decimal(str(v)) > risk.max_pair_correlation
                            for v in correlations.values()
                        ):
                            error(
                                "CORRELATION_LIMIT",
                                "quantity",
                                "Correlated exposure exceeds supplied limit",
                            )
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    error("SIZING_INPUTS", "quantity", "Invalid or missing account sizing fields")
    return ValidationResult(valid=not issues, issues=issues, calculations=calculations)

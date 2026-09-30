from datetime import datetime
from decimal import Decimal

from app.analytics import position_size, reward_risk
from app.evidence import evidence_is_fresh, lineage_fresh, validate_claims
from app.models import (
    EvidenceRecord,
    HolderAction,
    NewEntryAction,
    PortfolioDecision,
    Rating,
    ResearchRequest,
    ValidationIssue,
    ValidationResult,
)

N, H = NewEntryAction, HolderAction
# Rating -> (allowed new-entry actions, allowed holder actions, allowed thesis states).
# Mirrors the rating definitions in docs/INVESTMENT_POLICY.md and the research standard.
DECISION_MATRIX = {
    Rating.BUY: (
        {N.ENTER_NOW, N.CONDITIONAL_ENTRY, N.STAGED_ENTRY},
        {H.ADD, H.HOLD},
        {"ACTIVE"},
    ),
    Rating.OVERWEIGHT: (
        {N.STAGED_ENTRY, N.CONDITIONAL_ENTRY},
        {H.ADD, H.HOLD},
        {"ACTIVE"},
    ),
    Rating.HOLD: (
        {N.WAIT, N.CONDITIONAL_ENTRY},
        {H.HOLD, H.PROTECT_PROFIT},
        {"ACTIVE", "WEAKENED"},
    ),
    Rating.UNDERWEIGHT: (
        {N.AVOID},
        {H.TRIM, H.PROTECT_PROFIT},
        {"ACTIVE", "WEAKENED"},
    ),
    Rating.SELL: (
        {N.AVOID},
        {H.EXIT, H.TRIM},
        {"ACTIVE", "WEAKENED", "INVALIDATED"},
    ),
    Rating.DEFER: (
        {N.DEFER},
        {H.DEFER},
        {"ACTIVE", "WEAKENED", "INVALIDATED", "UNKNOWN"},
    ),
}
# A long entry plan must serve an entry or an add; TradePlan cannot express trims or exits.
PLAN_NEW_ENTRY_ACTIONS = {N.ENTER_NOW, N.CONDITIONAL_ENTRY, N.STAGED_ENTRY}
# Actions executed now; a stated entry range must contain the fresh price.
IMMEDIATE_NEW_ENTRY_ACTIONS = {N.ENTER_NOW, N.STAGED_ENTRY}
# Toss US orders: whole-share quantities, or dollar-amount market buys filled to 6 decimals.
LOT_STEPS = {"whole_share": Decimal(1), "fractional_amount": Decimal("0.000001")}


def held_quantity(records: list[EvidenceRecord], ticker: str) -> Decimal | None:
    """Quantity held per the latest account evidence; None when the account was not read."""
    portfolios = [r for r in records if r.evidence_type == "portfolio"]
    if not portfolios:
        return None
    return sum(
        (
            Decimal(str(h["quantity"]))
            for h in portfolios[-1].payload.get("holdings", [])
            if h.get("ticker") == ticker
        ),
        Decimal(0),
    )


def validate_consistency(decision: PortfolioDecision) -> list[ValidationIssue]:
    """Rating, actions, thesis state and trade plan must describe one coherent decision."""
    issues: list[ValidationIssue] = []

    def error(code, field, message):
        issues.append(ValidationIssue(code=code, field=field, message=message))

    entries, holders, states = DECISION_MATRIX[decision.rating]
    if decision.new_entry_action not in entries:
        error(
            "ACTION_RATING_MISMATCH",
            "new_entry_action",
            f"{decision.new_entry_action} contradicts rating {decision.rating.value}",
        )
    if decision.holder_action not in holders:
        error(
            "ACTION_RATING_MISMATCH",
            "holder_action",
            f"{decision.holder_action} contradicts rating {decision.rating.value}",
        )
    if decision.thesis_state not in states:
        error(
            "THESIS_RATING_MISMATCH",
            "thesis_state",
            f"Thesis state {decision.thesis_state} contradicts rating {decision.rating.value}",
        )
    if decision.trade_plan is not None and not (
        decision.new_entry_action in PLAN_NEW_ENTRY_ACTIONS or decision.holder_action == H.ADD
    ):
        error(
            "PLAN_ACTION_MISMATCH",
            "trade_plan",
            "A long entry plan requires an entry action or a holder add",
        )
    if decision.rating != Rating.DEFER and not any(
        claim.classification != "ASSUMPTION" and claim.evidence_ids for claim in decision.thesis
    ):
        error(
            "MISSING_THESIS",
            "thesis",
            "Every tradeable rating needs at least one evidence-backed thesis claim",
        )
    return issues


def validate_decision(
    decision: PortfolioDecision,
    records: list[EvidenceRecord],
    request: ResearchRequest,
    at: datetime,
    blocking_gaps: list[str] | None = None,
) -> ValidationResult:
    """Validate a PM decision.

    `blocking_gaps` are binding gaps asserted outside the decision (the research manager's).
    """
    issues = validate_claims(decision.thesis + decision.material_changes, records, at)
    issues += validate_consistency(decision)
    calculations: dict[str, str] = {}

    def error(code, field, message):
        issues.append(ValidationIssue(code=code, field=field, message=message))

    blocking = [gap.description for gap in decision.blocking_gaps()] + list(blocking_gaps or [])
    if blocking and decision.rating != Rating.DEFER:
        error(
            "BLOCKING_GAP",
            "rating",
            "Blocking evidence gaps require 판단 보류: " + "; ".join(blocking),
        )
    if decision.rating == Rating.DEFER and not blocking:
        error("UNEXPLAINED_DEFER", "material_gaps", "판단 보류 must name a blocking evidence gap")
    by_id = {record.evidence_id: record for record in records}

    def current_price(currency, field):
        quotes = [r for r in records if r.evidence_type == "quote" and r.ticker == request.ticker]
        if not quotes or not evidence_is_fresh(quotes[-1], at):
            error("STALE_QUOTE", field, "Fresh quote required for precise levels")
        elif quotes[-1].payload.get("currency") != currency:
            error("CURRENCY", field, "Quote and level currency differ")
        else:
            return Decimal(str(quotes[-1].payload["price"]))
        return None

    def check_levels(levels, currency):
        for field, level in levels:
            if not level.evidence_ids or not level.basis.strip():
                error("LEVEL_BASIS", field, "Precise levels need evidence and derivation")
            for evidence_id in level.evidence_ids:
                record = by_id.get(evidence_id)
                if record is None or not record.usable_as_fact or record.retrieved_at > at:
                    error("LEVEL_EVIDENCE", field, "Invalid evidence for precise level")
                elif not lineage_fresh(record, by_id, at):
                    error(
                        "LEVEL_STALE",
                        field,
                        "Level evidence or one of its inputs is stale at the decision cutoff",
                    )
            supported = [
                fact
                for evidence_id in level.evidence_ids
                if evidence_id in by_id
                for fact in by_id[evidence_id].facts
                if fact.value == level.value
                and fact.unit == f"{currency}/share"
                and by_id[evidence_id].ticker == request.ticker
            ]
            if not supported:
                error(
                    "UNSUPPORTED_LEVEL",
                    field,
                    "Level must match an observed or calculated price fact",
                )

    guard = decision.position_guard
    held = held_quantity(records, request.ticker)
    if (
        held
        and guard is None
        and decision.rating != Rating.DEFER
        and decision.holder_action != HolderAction.EXIT
    ):
        error(
            "MISSING_POSITION_GUARD",
            "position_guard",
            "A held position needs an evidence-backed stop the monitor can watch",
        )
    if guard is not None:
        if held == 0:
            error("GUARD_WITHOUT_POSITION", "position_guard", "The account holds no position")
        guard_price = current_price(guard.currency, "position_guard")
        check_levels(
            [("guard_stop", guard.stop)]
            + [(f"guard_take_profit_{i}", t) for i, t in enumerate(guard.take_profit)],
            guard.currency,
        )
        if guard_price is not None:
            if guard.stop.value >= guard_price:
                error(
                    "GUARD_GEOMETRY",
                    "guard_stop",
                    "A holding stop must be below the current price; an exit is a holder action",
                )
            if any(t.value <= guard_price for t in guard.take_profit):
                error(
                    "GUARD_GEOMETRY",
                    "guard_take_profit",
                    "Take-profit levels must be above the current price",
                )
    plan = decision.trade_plan
    if plan is None:
        return ValidationResult(valid=not issues, issues=issues, calculations=calculations)
    if decision.rating == Rating.DEFER:
        error("DEFERRED_PLAN", "trade_plan", "Deferred decisions cannot carry precise trade plans")
    price = current_price(plan.currency, "trade_plan")
    check_levels(
        [
            ("entry_low", plan.entry_low),
            ("entry_high", plan.entry_high),
            ("stop", plan.stop),
        ]
        + [(f"target_{i}", t) for i, t in enumerate(plan.targets)],
        plan.currency,
    )
    entry, stop = plan.entry_high.value, plan.stop.value
    if plan.entry_low.value > entry or stop >= plan.entry_low.value:
        error("GEOMETRY", "entry", "Stop must be below the entire entry range")
    if (
        price is not None
        and decision.new_entry_action in IMMEDIATE_NEW_ENTRY_ACTIONS
        and not plan.entry_low.value <= price <= plan.entry_high.value
    ):
        error(
            "ENTRY_ACTION_PRICE",
            "new_entry_action",
            "Immediate entry requires the fresh price inside the entry range; "
            "use CONDITIONAL_ENTRY otherwise",
        )
    if plan.sizing_unit == "fractional_amount" and plan.currency != "USD":
        error(
            "FRACTIONAL_UNSUPPORTED",
            "sizing_unit",
            "Amount-based fractional orders exist only for USD-listed securities",
        )
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
        step = LOT_STEPS[plan.sizing_unit]
        calculations["lot_step"] = str(step)
        if plan.quantity % step != 0:
            error("QUANTITY_STEP", "quantity", f"Quantity must be a multiple of {step}")
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
                        lot_step=step,
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

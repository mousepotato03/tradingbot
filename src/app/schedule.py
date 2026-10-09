"""When scheduled re-research runs.

Routine research runs shortly after the regular session opens, every
`routine_research_trading_days` trading days, so decisions and precise levels are validated
against fresh regular-session quotes. Price, position and data-quality events still trigger
research immediately in between; a completed event run restarts the routine count.
"""

import logging
from datetime import timedelta


def _open_slot(settings, market, now, trading_days):
    """The regular open `trading_days` sessions ahead plus the offset, or None."""
    offset = timedelta(minutes=settings.market_open_offset_minutes)
    opening, after = None, now - offset
    try:
        for _ in range(trading_days):
            opening = market.next_regular_open(after)
            if opening is None:
                return None
            after = opening
    except Exception:
        return None
    return opening + offset


def next_research_at(settings, market, now):
    """The routine research slot (or a fixed interval)."""
    if settings.research_schedule == "market_open":
        slot = _open_slot(settings, market, now, settings.routine_research_trading_days)
        if slot is not None:
            return slot
        logging.warning("Routine schedule fell back to the interval: market calendar unavailable")
    return now + timedelta(hours=settings.research_interval_hours)


def first_research_at(settings, market, now):
    """A new watch is researched now during a regular session, otherwise at the next open."""
    if settings.research_schedule != "market_open":
        return now
    try:
        if market.regular_session_open(now):
            return now
    except Exception:
        return now
    return _open_slot(settings, market, now, 1) or now + timedelta(
        hours=settings.research_interval_hours
    )

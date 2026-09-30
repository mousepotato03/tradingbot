"""When scheduled re-research runs.

Research runs shortly after the regular session opens on trading days, so decisions and
precise levels are validated against fresh regular-session quotes. Price and position events
still trigger research immediately; this only sets the routine cadence.
"""

from datetime import timedelta


def next_research_at(settings, market, now):
    """The next trading day's regular open plus the configured offset (or a fixed interval)."""
    if settings.research_schedule == "market_open":
        offset = timedelta(minutes=settings.market_open_offset_minutes)
        try:
            opening = market.next_regular_open(now - offset)
        except Exception:
            opening = None
        if opening is not None:
            return opening + offset
    return now + timedelta(hours=settings.research_interval_hours)


def first_research_at(settings, market, now):
    """A new watch is researched now during a regular session, otherwise at the next slot."""
    if settings.research_schedule != "market_open":
        return now
    try:
        if market.regular_session_open(now):
            return now
    except Exception:
        return now
    return next_research_at(settings, market, now)

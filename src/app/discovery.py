from sqlalchemy import select

from app.models import CandidateState, ResearchRequest, Security, utcnow
from app.storage import RunRow, StateRow


class Discovery:
    """Rotate verified identities through full research; current entry price is not a screen."""

    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def scan(self, batch=None):
        if batch is not None and batch < 1:
            raise ValueError("Discovery batch must be positive")
        securities = [Security.model_validate(row) for row in self.engine.market.universe()]
        selected = []
        with self.store.transaction() as session:
            for security in securities:
                state = session.get(StateRow, security.ticker)
                if state is None:
                    state = StateRow(
                        ticker=security.ticker,
                        report_id="",
                        state=CandidateState.UNIVERSE.value,
                        body={
                            "identity": security.model_dump(mode="json"),
                            "discovered_at": utcnow().isoformat(),
                        },
                    )
                    session.add(state)
                pending = session.scalar(
                    select(RunRow)
                    .where(
                        RunRow.ticker == security.ticker, RunRow.status.in_(["PENDING", "RUNNING"])
                    )
                    .limit(1)
                )
                if state.state == CandidateState.UNIVERSE and pending is None:
                    selected.append(security.ticker)
        run_ids = []
        for ticker in selected[: batch or self.engine.settings.discovery_batch]:
            run_ids.append(self.store.enqueue(ResearchRequest(ticker=ticker)))
            with self.store.transaction() as session:
                session.get(StateRow, ticker).state = CandidateState.RESEARCH.value
        return run_ids

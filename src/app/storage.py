from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    func,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.evidence import persistent_record
from app.models import EvidenceRecord, ResearchReport, ResearchRequest, new_id, utcnow


class Base(DeclarativeBase):
    pass


class RunRow(Base):
    __tablename__ = "research_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    request: Mapped[dict] = mapped_column(JSON)
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class EvidenceRow(Base):
    __tablename__ = "evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    body: Mapped[dict] = mapped_column(JSON)


class TraceRow(Base):
    __tablename__ = "tool_traces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    body: Mapped[dict] = mapped_column(JSON)


class ReportRow(Base):
    __tablename__ = "reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    body: Mapped[dict] = mapped_column(JSON)
    markdown: Mapped[str] = mapped_column(Text)


class StateRow(Base):
    __tablename__ = "candidate_states"
    ticker: Mapped[str] = mapped_column(String(20), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(36))
    state: Mapped[str] = mapped_column(String(30))
    body: Mapped[dict] = mapped_column(JSON)


class OutboxRow(Base):
    __tablename__ = "notification_outbox"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    body: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    retry_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobRow(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    available_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_until: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutcomeRow(Base):
    __tablename__ = "outcomes"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(36), index=True)
    body: Mapped[dict] = mapped_column(JSON)
    # All forward windows observed; the report is not re-fetched afterwards.
    mature: Mapped[bool] = mapped_column(Boolean, default=False)


class ObservationRow(Base):
    """Monitor observations. Completed research evidence is never appended to."""

    __tablename__ = "monitor_observations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    report_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    observed_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    body: Mapped[dict] = mapped_column(JSON)


class SearchUsageRow(Base):
    """One row per live web search, for the rolling provider quota."""

    __tablename__ = "search_usage"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(20))
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    used_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class WatchRow(Base):
    __tablename__ = "watch_schedules"
    ticker: Mapped[str] = mapped_column(String(20), primary_key=True)
    next_research_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    next_condition_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    body: Mapped[dict] = mapped_column(JSON, default=dict)


class Store:
    def __init__(self, url: str):
        if url.startswith("sqlite:///") and ":memory:" not in url:
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        if ":memory:" in url:
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
        self.engine = create_engine(url, **kwargs)

    def initialize(self):
        # Development/fixture convenience. Production uses Alembic migrations.
        Base.metadata.create_all(self.engine)
        # create_all never alters existing tables; fixture DBs made before 0002 lack this column.
        columns = {c["name"] for c in inspect(self.engine).get_columns("outcomes")}
        if "mature" not in columns:
            with self.engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE outcomes ADD COLUMN mature BOOLEAN NOT NULL DEFAULT FALSE")
                )

    @contextmanager
    def transaction(self):
        with Session(self.engine) as session, session.begin():
            yield session

    def enqueue(self, request: ResearchRequest) -> str:
        run_id = new_id()
        with self.transaction() as session:
            session.add(
                RunRow(id=run_id, ticker=request.ticker, request=request.model_dump(mode="json"))
            )
            session.add(JobRow(id=new_id(), run_id=run_id))
            state = session.get(StateRow, request.ticker)
            if request.as_of is None:
                if state is None:
                    session.add(
                        StateRow(
                            ticker=request.ticker, report_id="", state="RESEARCH_CANDIDATE", body={}
                        )
                    )
                elif state.state != "ACTIVE_POSITION":
                    from app.lifecycle import transition
                    from app.models import CandidateState

                    state.state = transition(
                        CandidateState(state.state), CandidateState.RESEARCH
                    ).value
        return run_id

    def get_run(self, run_id: str) -> dict:
        with self.transaction() as session:
            row = session.get(RunRow, run_id)
            if row is None:
                raise KeyError(run_id)
            return {
                "id": row.id,
                "ticker": row.ticker,
                "status": row.status,
                "request": row.request,
                "checkpoint": row.checkpoint,
                "error": row.error,
            }

    def checkpoint(self, run_id: str, state: dict, status="RUNNING"):
        with self.transaction() as session:
            row = session.get(RunRow, run_id)
            row.checkpoint, row.status = state, status

    def start_run(self, run_id: str, lease_seconds: int):
        with self.transaction() as session:
            changed = session.execute(
                update(RunRow)
                .where(RunRow.id == run_id, RunRow.status == "PENDING")
                .values(status="RUNNING")
            )
            if changed.rowcount != 1:
                raise RuntimeError("Run is already active or failed; enqueue a new run")
            session.execute(
                update(JobRow)
                .where(JobRow.run_id == run_id)
                .values(status="RUNNING", lease_until=utcnow() + timedelta(seconds=lease_seconds))
            )

    def recover_single_worker(self):
        """Call once when the sole worker starts after its previous process has stopped."""
        with self.transaction() as session:
            run_ids = list(session.scalars(select(JobRow.run_id).where(JobRow.status == "RUNNING")))
            if run_ids:
                session.execute(
                    update(JobRow)
                    .where(JobRow.run_id.in_(run_ids))
                    .values(status="PENDING", lease_until=None)
                )
                session.execute(
                    update(RunRow)
                    .where(RunRow.id.in_(run_ids), RunRow.status == "RUNNING")
                    .values(status="PENDING")
                )

    def fail(self, run_id: str, error_class: str):
        with self.transaction() as session:
            row = session.get(RunRow, run_id)
            row.status, row.error = "FAILED", error_class
            session.execute(update(JobRow).where(JobRow.run_id == run_id).values(status="FAILED"))

    def add_evidence(self, run_id: str, record: EvidenceRecord):
        record = persistent_record(record)
        with self.transaction() as session:
            run = session.get(RunRow, run_id)
            if run is not None and run.status == "COMPLETED":
                # A completed report's evidence ledger is immutable; monitors use observations.
                raise ValueError("Completed research evidence is immutable")
            session.add(
                EvidenceRow(
                    id=record.evidence_id, run_id=run_id, body=record.model_dump(mode="json")
                )
            )

    def evidence(self, run_id: str) -> list[EvidenceRecord]:
        with self.transaction() as session:
            rows = session.scalars(select(EvidenceRow).where(EvidenceRow.run_id == run_id)).all()
            return sorted(
                [EvidenceRecord.model_validate(row.body) for row in rows],
                key=lambda r: r.retrieved_at,
            )

    def add_observation(
        self, ticker: str, kind: str, record: EvidenceRecord, report_id: str | None = None
    ):
        record = persistent_record(record)
        with self.transaction() as session:
            session.add(
                ObservationRow(
                    id=record.evidence_id,
                    ticker=ticker,
                    kind=kind,
                    report_id=report_id,
                    observed_at=record.retrieved_at,
                    body=record.model_dump(mode="json"),
                )
            )

    def observations(self, ticker: str, kind: str | None = None) -> list[EvidenceRecord]:
        with self.transaction() as session:
            query = select(ObservationRow).where(ObservationRow.ticker == ticker)
            if kind is not None:
                query = query.where(ObservationRow.kind == kind)
            rows = session.scalars(query.order_by(ObservationRow.observed_at)).all()
            return [EvidenceRecord.model_validate(row.body) for row in rows]

    def consume_search(self, provider: str, run_id: str | None, limit: int | None) -> bool:
        """Record one search if the rolling 30-day count is below `limit`.

        Counting before the call keeps the cap conservative: failed requests also count.
        """
        now = utcnow()
        with self.transaction() as session:
            used = session.scalar(
                select(func.count())
                .select_from(SearchUsageRow)
                .where(SearchUsageRow.used_at > now - timedelta(days=30))
            )
            if limit is not None and used >= limit:
                return False
            session.add(SearchUsageRow(provider=provider, run_id=run_id, used_at=now))
            return True

    def trace(self, run_id: str, body: dict):
        with self.transaction() as session:
            session.add(TraceRow(run_id=run_id, body=body))

    def traces(self, run_id: str) -> list[dict]:
        with self.transaction() as session:
            return [
                r.body for r in session.scalars(select(TraceRow).where(TraceRow.run_id == run_id))
            ]

    def get_report(self, run_id: str) -> ResearchReport:
        with self.transaction() as session:
            row = session.get(ReportRow, run_id)
            if row is None:
                raise KeyError(run_id)
            return ResearchReport.model_validate(row.body)

    def previous(
        self, ticker: str, as_of: datetime | None = None, fixture: bool | None = None
    ) -> ResearchReport | None:
        with self.transaction() as session:
            if as_of is not None:
                # The live state points at today's latest report, not the report available then.
                rows = session.scalars(
                    select(ReportRow)
                    .where(ReportRow.ticker == ticker, ReportRow.created_at <= as_of)
                    .order_by(ReportRow.created_at.desc())
                )
                for row in rows:
                    report = ResearchReport.model_validate(row.body)
                    if (
                        report.created_at <= as_of
                        and report.as_of <= as_of
                        and (fixture is None or report.fixture == fixture)
                    ):
                        return report
                return None
            state = session.get(StateRow, ticker)
            if state is None or not state.report_id:
                return None
            return ResearchReport.model_validate(session.get(ReportRow, state.report_id).body)

    def commit_report(
        self,
        report: ResearchReport,
        markdown: str,
        events: list[dict],
        next_research: datetime | None = None,
    ):
        with self.transaction() as session:
            if session.get(ReportRow, report.run_id):
                return
            session.add(
                ReportRow(
                    id=report.run_id,
                    ticker=report.ticker,
                    body=report.model_dump(mode="json"),
                    markdown=markdown,
                )
            )
            # Historical replay must not replace the live candidate state or emit live alerts.
            run = session.get(RunRow, report.run_id)
            if not run.request.get("as_of"):
                from app.lifecycle import transition
                from app.models import CandidateState

                current = session.get(StateRow, report.ticker)
                if current:
                    transition(CandidateState(current.state), report.candidate_state)
                session.merge(
                    StateRow(
                        ticker=report.ticker,
                        report_id=report.run_id,
                        state=report.candidate_state.value,
                        body={"thesis_state": report.decision.thesis_state},
                    )
                )
                watched = session.get(WatchRow, report.ticker)
                # Re-research inherits the investor's conditions; monitor triggers do not replace them.
                base = {
                    key: value
                    for key, value in run.request.items()
                    if key not in {"as_of", "trigger"}
                }
                if watched is None:
                    session.add(
                        WatchRow(
                            ticker=report.ticker,
                            next_research_at=next_research or utcnow() + timedelta(hours=24),
                            next_condition_at=utcnow() + timedelta(seconds=60),
                            body={"base_request": base},
                        )
                    )
                else:
                    watched.next_research_at = next_research or utcnow() + timedelta(hours=24)
                    if not run.request.get("trigger") or "base_request" not in watched.body:
                        watched.body = {**watched.body, "base_request": base}
                for event in events:
                    if session.get(OutboxRow, event["id"]) is None:
                        session.add(OutboxRow(id=event["id"], body=event))
            run.status = "COMPLETED"
            session.execute(
                update(JobRow).where(JobRow.run_id == report.run_id).values(status="COMPLETED")
            )

    def claim_job(self, lease_seconds: int) -> str | None:
        now = utcnow()
        with self.transaction() as session:
            expired = list(
                session.scalars(
                    select(JobRow.run_id).where(
                        JobRow.status == "RUNNING", JobRow.lease_until < now
                    )
                )
            )
            if expired:
                session.execute(
                    update(RunRow)
                    .where(RunRow.id.in_(expired), RunRow.status == "RUNNING")
                    .values(status="PENDING")
                )
            session.execute(
                update(JobRow)
                .where(JobRow.status == "RUNNING", JobRow.lease_until < now)
                .values(status="PENDING", lease_until=None)
            )
            query = (
                select(JobRow)
                .where(JobRow.status == "PENDING", JobRow.available_at <= now)
                .order_by(JobRow.available_at)
            )
            if self.engine.dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            row = session.scalars(query.limit(1)).first()
            if row is None:
                return None
            result = session.execute(
                update(JobRow)
                .where(JobRow.id == row.id, JobRow.status == "PENDING")
                .values(status="RUNNING", lease_until=now + timedelta(seconds=lease_seconds))
            )
            return row.run_id if result.rowcount == 1 else None

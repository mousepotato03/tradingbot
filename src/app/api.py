from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from app.config import Settings
from app.models import ResearchRequest
from app.storage import ReportRow, Store


def create_app(settings: Settings | None = None, store: Store | None = None):
    settings = settings or Settings()
    store = store or Store(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        if settings.mode == "fixture":
            store.initialize()
        app.state.store = store
        yield

    application = FastAPI(title="Evidence research", lifespan=lifespan)

    @application.get("/health")
    def health():
        with store.transaction() as session:
            session.execute(select(1))
        return {"status": "ok", "mode": settings.mode}

    @application.post("/research-runs", status_code=202)
    def research(request: ResearchRequest):
        return {"run_id": store.enqueue(request)}

    @application.get("/research-runs/{run_id}")
    def run(run_id: str):
        try:
            return store.get_run(run_id)
        except KeyError:
            raise HTTPException(404, "Run not found") from None

    @application.get("/research-runs/{run_id}/report")
    def report(run_id: str):
        try:
            return store.get_report(run_id)
        except KeyError:
            raise HTTPException(404, "Report not available") from None

    @application.get("/research-runs/{run_id}/markdown", response_class=PlainTextResponse)
    def report_markdown(run_id: str):
        with store.transaction() as session:
            row = session.get(ReportRow, run_id)
            if row is None:
                raise HTTPException(404, "Report not available")
            return row.markdown

    @application.get("/research-runs/{run_id}/evidence")
    def evidence(run_id: str):
        return store.evidence(run_id)

    return application


app = create_app()

import argparse
import json
from pathlib import Path

from app.config import Settings
from app.engine import ResearchEngine
from app.models import ResearchRequest
from app.monitoring import Monitor
from app.reporting import markdown
from app.storage import Store


def main():
    parser = argparse.ArgumentParser(description="Evidence-based equity research")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    sub.add_parser("worker")
    research = sub.add_parser("research")
    research.add_argument("ticker")
    research.add_argument(
        "--request", type=Path, help="JSON request including optional explicit risk inputs"
    )
    research.add_argument("--enqueue", action="store_true")
    research.add_argument("--output", type=Path)
    status = sub.add_parser("status")
    status.add_argument("run_id")
    report = sub.add_parser("report")
    report.add_argument("run_id")
    watch = sub.add_parser("watch")
    watch.add_argument("ticker")
    sub.add_parser("doctor")
    sub.add_parser("evaluate")
    discovery = sub.add_parser("discover")
    discovery.add_argument("--batch", type=int, default=5)
    args = parser.parse_args()
    settings, store = Settings(), None
    if args.command == "init-db" and settings.mode != "fixture":
        parser.error("Live databases must be initialized with alembic upgrade head")
    if args.command == "doctor":
        if settings.mode == "live":
            settings.require_live()
        print(json.dumps({"mode": settings.mode, "configuration": "valid", "live_calls": False}))
        return
    store = Store(settings.database_url)
    if args.command == "init-db" or settings.mode == "fixture":
        store.initialize()
    if args.command == "init-db":
        print("Database initialized")
    elif args.command == "worker":
        from app.worker import serve

        serve(settings)
    elif args.command == "status":
        print(json.dumps(store.get_run(args.run_id), ensure_ascii=False, indent=2))
    elif args.command == "report":
        print(markdown(store.get_report(args.run_id), store.evidence(args.run_id)))
    elif args.command == "watch":
        Monitor(ResearchEngine(settings, store)).watch(args.ticker)
        print("Scheduled " + args.ticker.upper())
    elif args.command == "discover":
        from app.discovery import Discovery

        print(json.dumps({"run_ids": Discovery(ResearchEngine(settings, store)).scan(args.batch)}))
    elif args.command == "evaluate":
        from app.evaluation import OutcomeTracker

        print(json.dumps({"updated": OutcomeTracker(ResearchEngine(settings, store)).update()}))
    elif args.command == "research":
        request = (
            ResearchRequest.model_validate_json(args.request.read_text(encoding="utf-8"))
            if args.request
            else ResearchRequest(ticker=args.ticker)
        )
        if request.ticker != args.ticker.upper():
            parser.error("Ticker and request JSON disagree")
        run_id = store.enqueue(request)
        if args.enqueue:
            print(run_id)
            return
        result = ResearchEngine(settings, store).run(run_id)
        content = markdown(result, store.evidence(run_id))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content, encoding="utf-8")
        else:
            print(content)
        print("Run: " + run_id)


if __name__ == "__main__":
    main()

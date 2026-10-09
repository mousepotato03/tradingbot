import logging
import signal
import threading
import time
from datetime import timedelta

from app.config import Settings
from app.discovery import Discovery
from app.engine import ResearchEngine
from app.evaluation import OutcomeTracker
from app.models import utcnow
from app.monitoring import Monitor
from app.notifications import DiscordNotifier
from app.storage import Store


def serve(settings: Settings | None = None):
    settings = settings or Settings()
    store = Store(settings.database_url)
    if settings.mode == "fixture":
        store.initialize()
    store.recover_single_worker()
    engine, stopped = ResearchEngine(settings, store), threading.Event()
    notifier, monitor = DiscordNotifier(settings, store), Monitor(engine)
    for name in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, name), lambda *_: stopped.set())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    def background():
        while not stopped.is_set():
            settings.artifact_dir.parent.mkdir(parents=True, exist_ok=True)
            (settings.artifact_dir.parent / "worker-heartbeat").touch()
            try:
                monitor.tick()
                notifier.flush()
            except Exception as error:
                logging.error("Scheduler operation failed error_class=%s", type(error).__name__)
            stopped.wait(10)

    def prune():
        cutoff = utcnow() - timedelta(days=settings.observation_retention_days)
        logging.info("Pruned monitor observations count=%s", store.prune_observations(cutoff))

    def maintenance():
        # Discovery triage (model calls) and outcome refresh are slow; keep them off the
        # condition-monitoring loop so entry/stop checks stay on schedule.
        due = {"discovery": 0.0, "evaluation": 0.0, "prune": 0.0}
        jobs = {
            "discovery": lambda: Discovery(engine).scan(),
            "evaluation": lambda: OutcomeTracker(engine).update(),
            "prune": prune,
        }
        while not stopped.is_set():
            for name, job in jobs.items():
                if name == "discovery" and not settings.discovery_enabled:
                    continue
                if time.monotonic() < due[name]:
                    continue
                try:
                    job()
                    due[name] = time.monotonic() + 86400
                except Exception as error:
                    # Retry failed daily jobs hourly rather than on every loop.
                    due[name] = time.monotonic() + 3600
                    logging.error(
                        "Maintenance %s failed error_class=%s", name, type(error).__name__
                    )
            stopped.wait(60)

    scheduler = threading.Thread(target=background, daemon=True)
    scheduler.start()
    slow = threading.Thread(target=maintenance, daemon=True)
    slow.start()
    while not stopped.is_set():
        try:
            run_id = store.claim_job(settings.worker_lease_seconds)
            if run_id:
                engine.run(run_id)
                logging.info("Research completed run_id=%s", run_id)
        except Exception as error:
            logging.error("Worker operation failed error_class=%s", type(error).__name__)
        stopped.wait(2)
    scheduler.join(timeout=30)
    slow.join(timeout=30)


if __name__ == "__main__":
    serve()

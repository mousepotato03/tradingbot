import logging
import signal
import threading
import time

from app.config import Settings
from app.discovery import Discovery
from app.engine import ResearchEngine
from app.evaluation import OutcomeTracker
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
        next_discovery, next_evaluation = 0, 0
        while not stopped.is_set():
            settings.artifact_dir.parent.mkdir(parents=True, exist_ok=True)
            (settings.artifact_dir.parent / "worker-heartbeat").touch()
            try:
                monitor.tick()
                notifier.flush()
                if settings.discovery_enabled and time.monotonic() >= next_discovery:
                    Discovery(engine).scan()
                    next_discovery = time.monotonic() + 86400
                if time.monotonic() >= next_evaluation:
                    OutcomeTracker(engine).update()
                    next_evaluation = time.monotonic() + 86400
            except Exception as error:
                logging.error("Scheduler operation failed error_class=%s", type(error).__name__)
            stopped.wait(10)

    scheduler = threading.Thread(target=background, daemon=True)
    scheduler.start()
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


if __name__ == "__main__":
    serve()

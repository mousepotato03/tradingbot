from datetime import timedelta

from sqlalchemy import select

from app.adapters.http import ToolError, Transport
from app.config import Settings
from app.models import utcnow
from app.storage import OutboxRow, Store


class DiscordNotifier:
    def __init__(self, settings: Settings, store: Store, transport=None):
        self.settings, self.store = settings, store
        self.transport = transport or Transport()

    def flush(self):
        webhook = self.settings.discord_webhook.get_secret_value()
        if not webhook or self.settings.mode != "live":
            return 0
        now, delivered = utcnow(), 0
        with self.store.transaction() as session:
            rows = session.scalars(
                select(OutboxRow)
                .where(OutboxRow.status == "PENDING", OutboxRow.retry_at <= now)
                .order_by(OutboxRow.created_at)
                .limit(5)
            ).all()
            for row in rows:
                if row.body.get("fixture") or row.body.get("content", "").startswith("[FIXTURE]"):
                    row.status = "SKIPPED_FIXTURE"
                    continue
                expires = row.body.get("expires_at")
                if expires:
                    from datetime import datetime

                    if datetime.fromisoformat(expires) <= now:
                        row.status = "EXPIRED"
                        continue
                try:
                    # Webhook API has no idempotency guarantee: timeout may cause repeat delivery.
                    self.transport.request(
                        "POST",
                        webhook,
                        params={"wait": "true"},
                        json={
                            "content": row.body["content"][:1900],
                            "allowed_mentions": {"parse": []},
                        },
                    )
                    row.status = "SENT"
                    delivered += 1
                except ToolError:
                    row.attempts += 1
                    row.retry_at = now + timedelta(
                        seconds=min(3600, 60 * 2 ** min(row.attempts, 6))
                    )
                    if row.attempts >= 10:
                        row.status = "FAILED"
                    break
        return delivered

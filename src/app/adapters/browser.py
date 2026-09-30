import base64
from urllib.parse import urlsplit

import httpx

from app.adapters.http import ToolError
from app.config import Settings
from app.evidence import content_hash
from app.models import EvidenceRecord


class BrowserAdapter:
    def __init__(self, settings: Settings, run_id: str, registry):
        self.settings, self.run_id, self.registry = settings, run_id, registry
        self.client = httpx.Client(timeout=60, trust_env=False)

    def _request(self, route, body):
        token = self.settings.browser_token.get_secret_value()
        if not token:
            raise ToolError("BROWSER_NOT_CONFIGURED")
        try:
            response = self.client.post(
                self.settings.browser_url + route,
                json={"session_id": self.run_id, **body},
                headers={"Authorization": "Bearer " + token},
            )
            if response.status_code != 200:
                raise ToolError("BROWSER_REQUEST_FAILED")
            data = response.json()
        except httpx.HTTPError:
            raise ToolError("BROWSER_UNAVAILABLE", True) from None
        screenshot = data.pop("screenshot_base64", None)
        if screenshot:
            raw = base64.b64decode(screenshot, validate=True)
            data["screenshot_hash"] = content_hash(raw)
            if self.settings.retain_browser_screenshots:
                self.settings.artifact_dir.mkdir(parents=True, exist_ok=True)
                path = self.settings.artifact_dir / (content_hash(raw) + ".png")
                path.write_bytes(raw)
                data["screenshot_reference"] = str(path)
            self.registry.images.append("data:image/png;base64," + screenshot)
        text = data.pop("text", "")
        for download in data.get("downloads", []):
            text += "\n" + download.get("text", "")
        data["retain_full_text"] = (
            urlsplit(data["url"]).hostname in self.settings.retained_document_hosts
        )
        return EvidenceRecord(
            ticker=self.registry.ticker,
            evidence_type="browser",
            source_name=data["url"].split("/")[2],
            source_tier=3,
            source_url=data["url"],
            content_hash=content_hash({"observation": data, "text": text}),
            text=text,
            payload=data,
        )

    def open(self, url):
        return self._request("/open", {"url": url})

    def action(self, **kwargs):
        return self._request("/action", kwargs)

    def close(self):
        if self.settings.browser_token.get_secret_value():
            try:
                self.client.post(
                    self.settings.browser_url + "/close",
                    json={"session_id": self.run_id},
                    headers={
                        "Authorization": "Bearer " + self.settings.browser_token.get_secret_value()
                    },
                )
            except httpx.HTTPError:
                pass
        self.client.close()

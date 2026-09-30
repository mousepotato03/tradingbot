from datetime import datetime
from io import BytesIO
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from pypdf import PdfReader

from app.adapters.http import ToolError, Transport
from app.config import Settings
from app.evidence import content_hash
from app.models import EvidenceRecord


class BraveSearch:
    URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, settings: Settings, transport=None):
        self.settings, self.transport = settings, transport or Transport()

    def search(self, ticker, query, count=10):
        response = self.transport.request(
            "GET",
            self.URL,
            params={"q": query, "count": count},
            headers={"X-Subscription-Token": self.settings.brave_api_key.get_secret_value()},
        )
        data = response.json()
        results = [
            {
                "title": r["title"],
                "url": r["url"],
                "snippet": r.get("description", ""),
                "published_at": r.get("page_age"),
            }
            for r in data.get("web", {}).get("results", [])
        ]
        return EvidenceRecord(
            ticker=ticker,
            evidence_type="search",
            source_name="Brave Search",
            source_tier=3,
            source_url=self.URL,
            content_hash=content_hash(results),
            usable_as_fact=False,
            payload={"query": query, "results": results},
            warnings=["Search snippets are discovery aids. Open original sources."],
        )


class DocumentReader:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.transport = transport or Transport(proxy=settings.egress_proxy)

    def read(self, ticker, url):
        final_url, mime, content = self.transport.public_get(url)
        warnings, published, pages = [], None, []
        if "pdf" in mime or content.startswith(b"%PDF"):
            reader = PdfReader(BytesIO(content))
            if len(reader.pages) > 400:
                raise ToolError("DOCUMENT_TOO_LARGE")
            pages = [
                {"page": i + 1, "text": page.extract_text() or ""}
                for i, page in enumerate(reader.pages)
            ]
            text, title, method = (
                "\n".join(p["text"] for p in pages),
                final_url.rsplit("/", 1)[-1],
                "pdf-text",
            )
            if not text.strip():
                warnings.append("Image-only PDF; extracted text unavailable")
        else:
            soup = BeautifulSoup(content, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else final_url
            meta = soup.find("meta", property="article:published_time")
            if meta and meta.get("content"):
                try:
                    date = datetime.fromisoformat(meta["content"].replace("Z", "+00:00"))
                    if date.tzinfo:
                        published = date
                except ValueError:
                    pass
            for node in soup(["script", "style", "noscript"]):
                node.decompose()
            text, method = soup.get_text("\n", strip=True), "html-text"
        if len(text) > 200_000:
            text = text[:200_000]
            warnings.append("Text truncated; follow-up document sections may be required")
        return EvidenceRecord(
            ticker=ticker,
            evidence_type="document",
            source_name=final_url.split("/")[2],
            source_tier=1
            if urlsplit(final_url).hostname
            in {
                "www.sec.gov",
                "sec.gov",
                "data.sec.gov",
                "www.federalreserve.gov",
                "www.bls.gov",
                "www.bea.gov",
            }
            else 3,
            source_url=final_url,
            published_at=published,
            content_hash=content_hash(content),
            text=text,
            payload={
                "title": title,
                "extraction_method": method,
                "pages": pages[:400],
                "retain_full_text": urlsplit(final_url).hostname
                in self.settings.retained_document_hosts,
            },
            warnings=warnings,
        )

import json
import re
from datetime import datetime
from io import BytesIO
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag
from pypdf import PdfReader

from app.adapters.http import ToolError, Transport
from app.config import Settings
from app.evidence import content_hash
from app.models import EvidenceRecord, utcnow

OFFICIAL_HOSTS = {
    "www.sec.gov",
    "sec.gov",
    "data.sec.gov",
    "www.federalreserve.gov",
    "www.bls.gov",
    "www.bea.gov",
}
# Page chrome that is not the document: navigation, consent banners, promos and forms.
BOILERPLATE_TAGS = [
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "iframe",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "button",
]
BOILERPLATE_ROLES = {
    "navigation",
    "banner",
    "contentinfo",
    "dialog",
    "alertdialog",
    "complementary",
}
BOILERPLATE_TOKEN = re.compile(
    r"^(cookie|cookies|consent|gdpr|banner|newsletter|subscribe|subscription|modal|popup|"
    r"advert|advertisement|ads|promo|social|share|sharing|breadcrumb|breadcrumbs|menu|"
    r"sidebar|footer|navbar|nav|related|recommended|paywall)$"
)
TOKEN_SPLIT = re.compile(r"[\s_\-]+")
PUBLISHED_META = [
    ("property", "article:published_time"),
    ("property", "og:published_time"),
    ("name", "article:published_time"),
    ("name", "pubdate"),
    ("name", "publishdate"),
    ("name", "publish-date"),
    ("name", "date"),
    ("name", "dc.date.issued"),
    ("name", "dc.date"),
    ("name", "parsely-pub-date"),
    ("name", "sailthru.date"),
    ("itemprop", "datePublished"),
]
MAIN_CONTENT_MINIMUM = 200


def search_record(ticker, provider, url, query, options, unsupported, results, **extra):
    """Search results are discovery aids; they never substantiate a FACT."""
    return EvidenceRecord(
        ticker=ticker,
        evidence_type="search",
        source_name=provider,
        source_tier=3,
        source_url=url,
        content_hash=content_hash(results),
        usable_as_fact=False,
        payload={
            "query": query,
            "options": options,
            "unsupported_options": unsupported,
            "results": results,
            **extra,
        },
        warnings=["Search snippets are discovery aids. Open original sources."],
    )


class BraveSearch:
    URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, settings: Settings, transport=None):
        self.settings, self.transport = settings, transport or Transport()

    def search(
        self,
        ticker,
        query,
        count=10,
        freshness=None,
        domain=None,
        country=None,
        search_lang=None,
        offset=0,
        topic=None,
    ):
        options = {
            "freshness": freshness,
            "domain": domain,
            "country": country,
            "search_lang": search_lang,
            "offset": offset,
            "topic": topic,
        }
        params = {"q": f"{query} site:{domain}" if domain else query, "count": count}
        params.update(
            {
                key: value
                for key, value in (
                    ("freshness", freshness),
                    ("country", country),
                    ("search_lang", search_lang),
                    ("offset", offset or None),
                )
                if value is not None
            }
        )
        response = self.transport.request(
            "GET",
            self.URL,
            params=params,
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
        return search_record(
            ticker, "Brave Search", self.URL, query, options, ["topic"] if topic else [], results
        )


# Tavily expects lowercase English country names; only mapped markets are sent.
TAVILY_COUNTRIES = {
    "US": "united states",
    "CA": "canada",
    "GB": "united kingdom",
    "DE": "germany",
    "FR": "france",
    "NL": "netherlands",
    "CH": "switzerland",
    "IE": "ireland",
    "IT": "italy",
    "ES": "spain",
    "SE": "sweden",
    "IL": "israel",
    "IN": "india",
    "JP": "japan",
    "KR": "south korea",
    "CN": "china",
    "TW": "taiwan",
    "SG": "singapore",
    "AU": "australia",
    "BR": "brazil",
    "MX": "mexico",
}
TAVILY_TIME_RANGES = {"pd": "day", "pw": "week", "pm": "month", "py": "year"}
# 432: plan credits exhausted; 433: pay-as-you-go cap reached.
TAVILY_QUOTA_ERRORS = {"HTTP_432", "HTTP_433"}


class TavilySearch:
    """Tavily /search. Basic depth costs one credit per request."""

    URL = "https://api.tavily.com/search"

    def __init__(self, settings: Settings, transport=None):
        self.settings, self.transport = settings, transport or Transport()

    def search(
        self,
        ticker,
        query,
        count=10,
        freshness=None,
        domain=None,
        country=None,
        search_lang=None,
        offset=0,
        topic=None,
    ):
        options = {
            "freshness": freshness,
            "domain": domain,
            "country": country,
            "search_lang": search_lang,
            "offset": offset,
            "topic": topic,
        }
        body = {
            "query": query,
            "search_depth": "basic",
            "topic": topic or "general",
            "max_results": count,
            "include_answer": False,
            "include_raw_content": False,
            "include_published_date": True,
        }
        unsupported = []
        if freshness in TAVILY_TIME_RANGES:
            body["time_range"] = TAVILY_TIME_RANGES[freshness]
        elif freshness:
            body["start_date"], body["end_date"] = freshness.split("to")
        if domain:
            body["include_domains"] = [domain]
        if country:
            # Tavily applies country boosting only to general-topic searches.
            if country in TAVILY_COUNTRIES and body["topic"] == "general":
                body["country"] = TAVILY_COUNTRIES[country]
            else:
                unsupported.append("country")
        if search_lang:
            body["language"] = search_lang
        if offset:
            unsupported.append("offset")  # Tavily has no result pagination.
        try:
            response = self.transport.request(
                "POST",
                self.URL,
                json=body,
                headers={
                    "Authorization": "Bearer " + self.settings.tavily_api_key.get_secret_value()
                },
            )
        except ToolError as error:
            if error.code in TAVILY_QUOTA_ERRORS:
                raise ToolError("SEARCH_QUOTA_EXHAUSTED") from None
            raise
        data = response.json()
        results = [
            {
                "title": r.get("title", ""),
                "url": r["url"],
                "snippet": r.get("content", ""),
                "published_at": r.get("published_date"),
                "score": r.get("score"),
            }
            for r in data.get("results", [])
        ]
        return search_record(
            ticker,
            "Tavily Search",
            self.URL,
            query,
            options,
            unsupported,
            results,
            request_id=data.get("request_id"),
        )


def _aware(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _json_ld(soup) -> dict:
    """First article-like JSON-LD object: headline, dates and author."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (TypeError, ValueError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop(0)
            if not isinstance(item, dict):
                continue
            stack += item.get("@graph", []) if isinstance(item.get("@graph"), list) else []
            if item.get("datePublished") or item.get("headline"):
                author = item.get("author")
                if isinstance(author, list):
                    author = author[0] if author else None
                return {
                    "headline": item.get("headline"),
                    "date_published": item.get("datePublished"),
                    "date_modified": item.get("dateModified"),
                    "author": author.get("name") if isinstance(author, dict) else author,
                }
    return {}


def _is_boilerplate(node: Tag) -> bool:
    if node.get("role") in BOILERPLATE_ROLES or node.get("aria-modal") == "true":
        return True
    names = " ".join(node.get("class", [])) + " " + (node.get("id") or "")
    return any(
        BOILERPLATE_TOKEN.match(token.lower()) for token in TOKEN_SPLIT.split(names) if token
    )


def _main_node(soup):
    """Readability-style choice: explicit main/article, else the densest paragraph container."""
    for selector in ("main", "article", "[role=main]", "[itemprop=articleBody]"):
        node = soup.select_one(selector)
        if node and len(node.get_text(" ", strip=True)) >= MAIN_CONTENT_MINIMUM:
            return node
    scores: dict[int, list] = {}

    def add(node, value):
        if isinstance(node, Tag):
            scores.setdefault(id(node), [node, 0.0])[1] += value

    for paragraph in soup.find_all("p"):
        length = len(paragraph.get_text(" ", strip=True))
        if length >= 25:
            add(paragraph.parent, length)
            add(paragraph.parent.parent if paragraph.parent else None, length / 2)
    if scores:
        node, score = max(scores.values(), key=lambda item: item[1])
        if score >= MAIN_CONTENT_MINIMUM:
            return node
    return soup.body or soup


def _strip_boilerplate(soup):
    for node in soup(BOILERPLATE_TAGS):
        node.decompose()
    total = len((soup.body or soup).get_text(" ", strip=True)) or 1
    for node in [n for n in soup.find_all(True) if _is_boilerplate(n)]:
        # A matching wrapper that holds most of the page is the page, not chrome.
        if not node.decomposed and len(node.get_text(" ", strip=True)) <= 0.4 * total:
            node.decompose()


def _flatten_tables(node):
    """Keep table rows on one line so labels stay next to their numbers."""
    for table in node.find_all("table"):
        rows = []
        for row in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["th", "td"])]
            if any(cells):
                rows.append(" | ".join(cells))
        table.replace_with("\n".join(rows) + "\n")


class DocumentReader:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.transport = transport or Transport(proxy=settings.egress_proxy)

    def _headers(self, url):
        host = urlsplit(url).hostname or ""
        # SEC requires a declared contact; send it only to SEC hosts.
        if (host == "sec.gov" or host.endswith(".sec.gov")) and self.settings.sec_user_agent:
            return {"User-Agent": self.settings.sec_user_agent}
        return None

    def read(self, ticker, url):
        final_url, mime, content = self.transport.public_get(url, headers_for=self._headers)
        warnings, published, pages, meta = [], None, [], {}
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
            meta = _json_ld(soup)
            candidates = [meta.get("date_published")]
            for attribute, value in PUBLISHED_META:
                tag = soup.find(
                    "meta", attrs={attribute: re.compile(f"^{re.escape(value)}$", re.I)}
                )
                candidates.append(tag.get("content") if tag else None)
            time_tag = soup.find("time", attrs={"datetime": True})
            candidates.append(time_tag.get("datetime") if time_tag else None)
            for candidate in candidates:
                published = _aware(candidate)
                if published:
                    break
            if published and published > utcnow():
                warnings.append("Page declares a future publication time; ignored")
                published = None
            if meta.get("headline"):
                title = meta["headline"]
            _strip_boilerplate(soup)
            main = _main_node(soup)
            _flatten_tables(main)
            text = main.get_text("\n", strip=True)
            method = "html-main-content" if main is not (soup.body or soup) else "html-text"
            if not text:
                text, method = soup.get_text("\n", strip=True), "html-text"
        if len(text) > 200_000:
            text = text[:200_000]
            warnings.append("Text truncated; follow-up document sections may be required")
        host = urlsplit(final_url).hostname
        return EvidenceRecord(
            ticker=ticker,
            evidence_type="document",
            source_name=final_url.split("/")[2],
            source_tier=1 if host in OFFICIAL_HOSTS else 3,
            source_url=final_url,
            published_at=published,
            content_hash=content_hash(content),
            text=text,
            payload={
                "title": title,
                "extraction_method": method,
                "pages": pages[:400],
                "author": meta.get("author"),
                "date_modified": meta.get("date_modified"),
                "retain_full_text": host in self.settings.retained_document_hosts,
            },
            warnings=warnings,
        )

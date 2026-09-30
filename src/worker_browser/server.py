import asyncio
import base64
import hmac
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

from app.adapters.http import ToolError, public_url
from app.evidence import content_hash


class SessionInput(BaseModel):
    session_id: str = Field(pattern=r"^[a-zA-Z0-9-]{1,64}$")


class OpenInput(SessionInput):
    url: str


class ActionInput(SessionInput):
    action: Literal["click", "type", "scroll", "screenshot", "back", "tab", "close"]
    ref: str | None = None
    text: str | None = None
    x: int | None = None
    y: int | None = None
    amount: int | None = None


class BrowserRuntime:
    def __init__(
        self, browser, *, allow_test_network=False, downloads=Path("/tmp/browser-downloads")
    ):
        self.browser, self.allow_test_network, self.downloads = (
            browser,
            allow_test_network,
            downloads,
        )
        self.contexts, self.refs, self.accessed = {}, {}, {}
        self.downloaded = {}
        self.active_pages = {}
        self.lock = asyncio.Lock()

    async def check_url(self, url):
        if self.allow_test_network:
            return
        try:
            await asyncio.to_thread(public_url, url)
        except ToolError:
            raise HTTPException(400, "Unsafe or unavailable URL") from None

    async def context(self, session_id):
        self.accessed[session_id] = time.monotonic()
        for name in list(self.contexts):
            if time.monotonic() - self.accessed.get(name, 0) > 600 and name != session_id:
                await self.close(name)
        if session_id not in self.contexts:
            if self.contexts:
                raise HTTPException(429, "Browser session limit reached")
            proxy = os.environ.get("BROWSER_EGRESS_PROXY")
            context = await self.browser.new_context(
                viewport={"width": 1280, "height": 900},
                accept_downloads=True,
                service_workers="block",
                proxy={"server": proxy} if proxy else None,
            )

            async def route_handler(route):
                try:
                    await self.check_url(route.request.url)
                    await route.continue_()
                except HTTPException:
                    await route.abort()

            await context.route("**/*", route_handler)
            self.contexts[session_id] = context
            self.downloaded[session_id] = []
            context.on(
                "page",
                lambda page: page.on(
                    "download",
                    lambda download: asyncio.create_task(self.download(session_id, download)),
                ),
            )
        return self.contexts[session_id]

    async def download(self, session_id, download):
        directory = self.downloads / session_id
        directory.mkdir(parents=True, exist_ok=True)
        if len(self.downloaded.get(session_id, [])) >= 5:
            await download.cancel()
            return
        path = directory / (content_hash(download.url) + ".download")
        await download.save_as(path)
        size = path.stat().st_size
        if size > 10_000_000:
            path.unlink()
            return
        raw = path.read_bytes()
        data = {"url": download.url, "content_hash": content_hash(raw), "size": size, "text": ""}
        if raw.startswith(b"%PDF"):
            from pypdf import PdfReader

            try:
                reader = PdfReader(path)
                data["pages"] = [
                    {"page": i + 1, "text": page.extract_text() or ""}
                    for i, page in enumerate(reader.pages[:100])
                ]
                data["text"] = "\n".join(p["text"] for p in data["pages"])[:100000]
            except Exception:
                data["warning"] = "PDF extraction failed"
        self.downloaded.setdefault(session_id, []).append(data)

    async def observation(self, session_id, page):
        await self.check_url(page.url)
        text = await page.locator("body").inner_text(timeout=10000)
        self.refs[session_id] = {}
        elements = []
        for i, locator in enumerate(
            await page.locator("a,button,input,textarea,select,[role=button]").all()
        ):
            if i >= 150:
                break
            if not await locator.is_visible():
                continue
            ref = f"element-{i}"
            self.refs[session_id][ref] = locator
            label = await locator.get_attribute("aria-label") or await locator.get_attribute(
                "placeholder"
            )
            if not label:
                label = (await locator.text_content() or "")[:200]
            elements.append(
                {"ref": ref, "label": label, "href": await locator.get_attribute("href")}
            )
        screenshot = await page.screenshot(type="png")
        return {
            "url": page.url,
            "title": await page.title(),
            "text": text[:100000],
            "elements": elements,
            "tabs": [
                {"index": i, "url": p.url} for i, p in enumerate(self.contexts[session_id].pages)
            ],
            "downloads": self.downloaded.get(session_id, []),
            "screenshot_base64": base64.b64encode(screenshot).decode(),
        }

    async def open(self, data):
        await self.check_url(data.url)
        context = await self.context(data.session_id)
        page = await context.new_page() if not context.pages else context.pages[-1]
        self.active_pages[data.session_id] = page
        await page.goto(data.url, timeout=30000, wait_until="domcontentloaded")
        return await self.observation(data.session_id, page)

    async def action(self, data):
        context = await self.context(data.session_id)
        if not context.pages:
            raise HTTPException(409, "Open a page first")
        page = self.active_pages.get(data.session_id) or context.pages[-1]
        if data.action == "tab":
            index = data.amount if data.amount is not None else 0
            if not 0 <= index < len(context.pages):
                raise HTTPException(400, "Invalid tab index")
            page = context.pages[index]
            await page.bring_to_front()
            self.active_pages[data.session_id] = page
        elif data.action == "back":
            await page.go_back(wait_until="domcontentloaded")
        elif data.action == "close":
            await page.close()
            if not context.pages:
                raise HTTPException(409, "No remaining tabs")
            page = context.pages[-1]
            self.active_pages[data.session_id] = page
        elif data.action == "scroll":
            await page.mouse.wheel(0, max(-2000, min(2000, data.amount or 700)))
        elif data.action in {"click", "type"}:
            locator = self.refs.get(data.session_id, {}).get(data.ref)
            if data.action == "type":
                if locator is not None:
                    await locator.fill((data.text or "")[:4000])
                elif data.x is not None and data.y is not None:
                    await page.mouse.click(data.x, data.y)
                    await page.keyboard.insert_text((data.text or "")[:4000])
                else:
                    raise HTTPException(409, "Current element reference required")
            elif locator is not None:
                await locator.click(timeout=10000)
            elif data.x is not None and data.y is not None:
                await page.mouse.click(data.x, data.y)
            else:
                raise HTTPException(409, "Current element reference or coordinates required")
        await page.wait_for_timeout(150)
        return await self.observation(data.session_id, page)

    async def close(self, session_id):
        context = self.contexts.pop(session_id, None)
        self.refs.pop(session_id, None)
        self.accessed.pop(session_id, None)
        self.active_pages.pop(session_id, None)
        self.downloaded.pop(session_id, None)
        if context:
            await context.close()
        directory = (self.downloads / session_id).resolve()
        if directory.is_relative_to(self.downloads.resolve()) and directory.exists():
            for file in directory.iterdir():
                if file.is_file():
                    file.unlink()
            directory.rmdir()


@asynccontextmanager
async def lifespan(app):
    if not os.environ.get("BROWSER_TOKEN"):
        raise RuntimeError("BROWSER_TOKEN is required")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, chromium_sandbox=os.name != "nt")
        app.state.runtime = BrowserRuntime(browser)
        yield
        await browser.close()


app = FastAPI(lifespan=lifespan)
auth = HTTPBearer()


def authorize(credentials: HTTPAuthorizationCredentials = Depends(auth)):
    expected = os.environ.get("BROWSER_TOKEN", "")
    if not expected or not hmac.compare_digest(credentials.credentials, expected):
        raise HTTPException(403, "Access denied")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/open", dependencies=[Depends(authorize)])
async def open_page(data: OpenInput):
    async with app.state.runtime.lock:
        try:
            return await asyncio.wait_for(app.state.runtime.open(data), 45)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, "Browser operation failed") from None


@app.post("/action", dependencies=[Depends(authorize)])
async def action(data: ActionInput):
    async with app.state.runtime.lock:
        try:
            return await asyncio.wait_for(app.state.runtime.action(data), 30)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, "Browser operation failed") from None


@app.post("/close", dependencies=[Depends(authorize)])
async def close(data: SessionInput):
    await app.state.runtime.close(data.session_id)
    return {"status": "closed"}

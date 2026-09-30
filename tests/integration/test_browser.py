import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from playwright.async_api import async_playwright

from worker_browser.server import ActionInput, BrowserRuntime, OpenInput


def test_actual_chromium_dynamic_page_coordinates_tabs_and_screenshot(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if self.path == "/download":
                self.send_header("Content-Disposition", 'attachment; filename="../../escape.txt"')
                self.end_headers()
                self.wfile.write(b"Synthetic official download")
                return
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b'<html><title>Local test issuer</title><body><input aria-label="Query"><button onclick="document.getElementById(\'result\').innerText=\'Official release loaded\'">Load release</button><p id="result"></p><a href="/other" target="_blank">Open tab</a><a href="/download">Download release</a></body></html>'
            )

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def verify():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            runtime = BrowserRuntime(browser, allow_test_network=True, downloads=tmp_path)
            observation = await runtime.open(
                OpenInput(session_id="test", url=f"http://127.0.0.1:{server.server_port}")
            )
            assert observation["title"] == "Local test issuer" and observation["screenshot_base64"]
            input_ref = next(e["ref"] for e in observation["elements"] if e["label"] == "Query")
            await runtime.action(
                ActionInput(session_id="test", action="type", ref=input_ref, text="earnings")
            )
            assert (
                await runtime.contexts["test"].pages[0].locator("input").input_value() == "earnings"
            )
            observation = await runtime.action(
                ActionInput(session_id="test", action="click", ref="element-1")
            )
            assert "Official release loaded" in observation["text"]
            box = await runtime.contexts["test"].pages[0].locator("button").bounding_box()
            await runtime.action(
                ActionInput(
                    session_id="test", action="click", x=int(box["x"] + 10), y=int(box["y"] + 10)
                )
            )
            await runtime.action(ActionInput(session_id="test", action="click", ref="element-2"))
            await asyncio.sleep(0.2)
            assert len(runtime.contexts["test"].pages) == 2
            observation = await runtime.action(
                ActionInput(session_id="test", action="tab", amount=0)
            )
            assert observation["screenshot_base64"]
            await runtime.action(ActionInput(session_id="test", action="click", ref="element-3"))
            for _ in range(20):
                if runtime.downloaded["test"]:
                    break
                await asyncio.sleep(0.1)
            assert runtime.downloaded["test"][0]["size"] == len(b"Synthetic official download")
            assert not (tmp_path.parent / "escape.txt").exists()
            assert all(p.parent == tmp_path / "test" for p in (tmp_path / "test").iterdir())
            await runtime.close("test")
            assert not runtime.contexts
            assert not (tmp_path / "test").exists()
            await browser.close()

    try:
        asyncio.run(verify())
    finally:
        server.shutdown()
        server.server_close()


def test_browser_is_read_only_posts_and_websockets_never_leave(tmp_path):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(("GET", self.path))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><title>Forms</title><body>"
                b'<form method="post" action="/submit"><input name="q" aria-label="Post field">'
                b'<button type="submit">Send post</button></form>'
                b'<form method="get" action="/search"><input name="q" aria-label="Search field">'
                b'<button type="submit">Search</button></form>'
                b"<script>try{new WebSocket('ws://'+location.host+'/ws')}catch(e){}</script>"
                b"</body></html>"
            )

        def do_POST(self):
            received.append(("POST", self.path))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def verify():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            runtime = BrowserRuntime(
                browser, allow_test_network=True, downloads=tmp_path, post_hosts=frozenset()
            )
            observation = await runtime.open(
                OpenInput(session_id="ro", url=f"http://127.0.0.1:{server.server_port}/")
            )
            ref = {e["label"]: e["ref"] for e in observation["elements"]}
            await runtime.action(
                ActionInput(session_id="ro", action="type", ref=ref["Post field"], text="x")
            )
            observation = await runtime.action(
                ActionInput(session_id="ro", action="click", ref=ref["Send post"])
            )
            await asyncio.sleep(0.3)
            blocked = {(b["method"], b["url"].split("/")[-1]) for b in runtime.blocked["ro"]}
            assert ("POST", "submit") in blocked
            assert any(b["method"] == "WEBSOCKET" for b in runtime.blocked["ro"])
            assert observation["blocked_requests"]
            observation = await runtime.open(
                OpenInput(session_id="ro", url=f"http://127.0.0.1:{server.server_port}/")
            )
            ref = {e["label"]: e["ref"] for e in observation["elements"]}
            await runtime.action(
                ActionInput(session_id="ro", action="type", ref=ref["Search field"], text="10-K")
            )
            await runtime.action(ActionInput(session_id="ro", action="click", ref=ref["Search"]))
            await asyncio.sleep(0.3)
            await runtime.close("ro")
            await browser.close()

    try:
        asyncio.run(verify())
    finally:
        server.shutdown()
        server.server_close()
    assert not any(method == "POST" for method, _ in received)
    assert ("GET", "/search?q=10-K") in received


def test_post_allowlist_is_exact_host_and_never_other_writes():
    runtime = BrowserRuntime(None, post_hosts=frozenset({"efts.sec.gov"}))
    assert runtime.method_allowed("GET", "https://example.com/")
    assert runtime.method_allowed("POST", "https://efts.sec.gov/LATEST/search-index")
    assert not runtime.method_allowed("POST", "https://evil.efts.sec.gov.example.com/")
    assert not runtime.method_allowed("PUT", "https://efts.sec.gov/")
    assert not runtime.method_allowed("DELETE", "https://efts.sec.gov/")

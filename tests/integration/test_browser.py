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

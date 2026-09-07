"""Browser regression tests for the production magnet-copy handlers.

Requires Python Playwright and its Chromium/Firefox/WebKit browsers:
    python -m playwright install chromium firefox webkit
    python frontend/tests/test_clipboard.py

Uses isolated browser contexts and synthetic links. Clipboard contents are
verified by a real paste, not by the success message alone. WebKit coverage
does not replace testing Safari on macOS/iOS.
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import uuid4

from playwright.sync_api import sync_playwright


SOURCE = Path(__file__).resolve().parents[1] / "index.html"
MAGNET = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=测试 空格 '引号' &tr=" + "x" * 4096 + "&run=" + uuid4().hex
MODES = ("native", "missing", "rejected", "delayed", "failed", "throws")


def fixture():
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("async function copyTextToClipboard(")
    handlers = source[start:source.index("async function translateCard(", start)]
    return ("""<!doctype html><meta charset="utf-8">
<title>Magnet clipboard regression</title>
<button id="other">Other magnet</button>
<button id="copy">Copy test magnet</button>
<label>Paste verification<textarea id="paste"></textarea></label>
<output id="toast"></output><pre id="status"></pre>
<script>
const mode = new URLSearchParams(location.search).get('mode') || 'native';
const magnet = MAGNET_JSON + '&test=' + mode;
const status = document.getElementById('status');
let legacyCalls = 0;
function renderStatus() {
  status.textContent = JSON.stringify({mode, secure: isSecureContext,
    api: !!navigator.clipboard, legacyCalls,
    temporaryTextareas: document.querySelectorAll('textarea').length - 1});
}
const nativeCopy = document.execCommand.bind(document);
document.execCommand = function(command) {
  legacyCalls++;
  if (mode === 'throws') throw new Error('Simulated legacy failure');
  return mode === 'failed' ? false : nativeCopy(command);
};
if (mode === 'missing') {
  Object.defineProperty(navigator, 'clipboard', {value: undefined});
} else if (mode !== 'native') {
  Object.defineProperty(navigator, 'clipboard', {value: {
    writeText() {
      return new Promise((resolve, reject) => setTimeout(
        () => reject(new DOMException('Simulated denial', 'NotAllowedError')),
        mode === 'delayed' ? 100 : 0));
    }
  }});
}
function showToast(message) { document.getElementById('toast').textContent = message; }
HANDLERS
document.getElementById('copy').onclick = async function() {
  await copyMagnet(magnet, this);
  renderStatus();
  document.body.dataset.done = 'true';
};
renderStatus();
</script>""".replace("MAGNET_JSON", json.dumps(MAGNET)).replace("HANDLERS", handlers)).encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(fixture())

    def log_message(self, *_args):
        pass


def check_browser(browser, base_url, modes):
    context = browser.new_context()
    page = context.new_page()
    dialogs = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def dismiss(dialog):
        dialogs.append((dialog.type, dialog.default_value))
        dialog.dismiss()

    page.on("dialog", dismiss)
    try:
        for mode in modes:
            expected = MAGNET + "&test=" + mode
            dialogs.clear()
            page.goto(f"{base_url}/?mode={mode}")
            page.locator("#copy").click()
            page.locator('body[data-done="true"]').wait_for()
            state = json.loads(page.locator("#status").inner_text())
            assert state["temporaryTextareas"] == 0, state
            # A browser may reject native writes under its default policy;
            # successful fallback is valid behavior in that case too.
            assert state["legacyCalls"] in ((0, 1) if mode == "native" else (1,)), state
            assert page.locator("#other").inner_text() == "Other magnet"
            if mode in ("failed", "throws"):
                assert dialogs == [("prompt", expected)], dialogs
                assert page.locator("#toast").inner_text() == ""
                assert page.locator("#copy").inner_text() == "Copy test magnet"
            else:
                assert dialogs == [], dialogs
                assert "已复制" in page.locator("#copy").inner_text()
                page.locator("#paste").focus()
                page.keyboard.press("ControlOrMeta+V")
                page.wait_for_function("document.getElementById('paste').value.length > 0")
                assert page.locator("#paste").input_value() == expected
            assert not errors, errors
            print(f"  PASS {mode} (legacy calls: {state['legacyCalls']})", flush=True)
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browsers", nargs="+", default=["chromium", "chrome", "edge", "firefox", "webkit"],
                        choices=["chromium", "chrome", "edge", "firefox", "webkit"])
    parser.add_argument("--modes", nargs="+", default=list(MODES), choices=MODES)
    args = parser.parse_args()
    selected = args.browsers
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    failures = []
    try:
        with sync_playwright() as p:
            for key, name, engine, options in (
                ("chromium", "Chromium", p.chromium, {}),
                ("chrome", "Chrome", p.chromium, {"channel": "chrome"}),
                ("edge", "Edge", p.chromium, {"channel": "msedge"}),
                ("firefox", "Firefox", p.firefox, {}),
                ("webkit", "WebKit (not Safari)", p.webkit, {}),
            ):
                if key not in selected:
                    continue
                print(name, flush=True)
                browser = None
                try:
                    browser = engine.launch(headless=True, **options)
                    print(f"  Version {browser.version}", flush=True)
                    check_browser(browser, base_url, args.modes)
                except Exception as error:
                    failures.append(name)
                    print(f"  FAILED/UNAVAILABLE: {error}", flush=True)
                finally:
                    if browser:
                        browser.close()
    finally:
        server.shutdown()
        server.server_close()
    if failures:
        raise SystemExit("Not fully verified: " + ", ".join(failures))


if __name__ == "__main__":
    main()

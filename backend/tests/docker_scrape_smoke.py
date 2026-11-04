"""Live HTTP smoke test. Run only inside a disposable Docker test container.

Requires the application on :8085 and a separate tmpfs at /cross-volume.
Uses synthetic files and local qB/Emby stubs; no real services or media.
"""
import base64
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

BASE = "http://127.0.0.1:8085"
ROOT = Path("/testdata")
TORRENTS = []
NOTIFIED = []
UPLOADS = []


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, data, status=200):
        raw = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        self.send_response(status)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/v2/app/version":
            self.reply("v5.0-test")
        elif path == "/api/v2/torrents/info":
            self.reply(TORRENTS)
        elif path == "/Items":
            self.reply({"Items": [{"Id": "movie-test", "Type": "Movie", "People": [
                {"Name": "Test Actor", "Id": "actor-test", "ImageTags": {"Primary": "before"}}]}]})
        elif path == "/Items/actor-test/Images":
            self.reply([{"ImageType": "Primary", "ImageTag": "after"}])
        else:
            self.reply({}, 404)

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/api/v2/auth/login":
            self.reply("Ok.")
        elif self.path == "/Library/Media/Updated":
            NOTIFIED.extend(json.loads(data)["Updates"])
            self.reply({})
        elif self.path == "/Items/actor-test/Images/Primary":
            UPLOADS.append(base64.b64decode(data))
            self.reply({})
        else:
            self.reply({}, 404)


def request(path, payload=None, method=None, expected=200):
    response = httpx.request(method or ("POST" if payload is not None else "GET"),
                             BASE + path, json=payload, timeout=45)
    assert response.status_code == expected, (path, response.status_code, response.text)
    return response.json()


def passed(message):
    print("PASS " + message, flush=True)


def main():
    assert Path("/.dockerenv").exists(), "This script requires a disposable Docker container"
    ROOT.mkdir(exist_ok=True)
    for attempt in range(50):
        try:
            request("/api/health")
            break
        except httpx.TransportError:
            time.sleep(0.2)
    else:
        raise AssertionError("Application did not start")
    if "--prepare-restart" in sys.argv:
        assert request("/api/config", {"scrape_enabled": True})["success"]
        passed("saved master switch ON before container restart")
        return
    if "--verify-restart" in sys.argv:
        config = request("/api/config")
        assert config["scrape_enabled"] is True
        assert config["scrape_multi_enabled"] is True
        assert [pair["emby_media_root"] for pair in config["scrape_directory_pairs"]] == ["/media/a", "/media/b"]
        assert request("/api/library/scrape/monitor")["enabled"]
        assert request("/api/config", {"scrape_enabled": False})["success"]
        passed("container restart preserved ON state and both Emby mappings; switched OFF after verification")
        return
    assert request("/api/config")["scrape_enabled"] is False
    assert not request("/api/library/scrape/monitor")["enabled"]
    passed("fresh installation defaults to scraping OFF")
    html = httpx.get(BASE, timeout=10).text
    assert 'id="btnScrapeToggle"' in html and "scrape-emby-root" in html
    passed("container serves updated settings UI")

    server = ThreadingHTTPServer(("127.0.0.1", 19090), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    pairs = []
    for group, code in (("a", "TSTA-123"), ("b", "TSTB-456")):
        watch, output = ROOT / "downloads" / group, ROOT / "archive" / group
        watch.mkdir(parents=True, exist_ok=True)
        output.mkdir(parents=True, exist_ok=True)
        video = watch / f"{code}.mp4"
        video.write_bytes(("synthetic movie " + group).encode() * 100)
        TORRENTS.append({"name": video.name, "content_path": str(video), "save_path": str(watch),
                         "hash": group * 40, "progress": 1, "amount_left": 0, "state": "uploading"})
        pairs.append({"watch_dir": str(watch), "output_dir": str(output), "emby_media_root": "/media/" + group})

    settings = {"scrape_enabled": False, "scrape_multi_enabled": True, "scrape_directory_pairs": pairs,
                "scrape_meta_enabled": False, "scrape_organize_enabled": False, "scrape_video_rename_enabled": False,
                "scrape_delete_extras": False, "scrape_min_size_mb": 0, "archive_by_month": False,
                "archive_mode": "hardlink", "archive_enabled": True, "qb_url": "http://127.0.0.1:19090",
                "emby_url": "http://127.0.0.1:19090", "emby_api_key": "test-only",
                "emby_actor_sync_enabled": True, "public_trackers_auto_update": False,
                "actor_javdb_directory_enabled": False, "show_latest": False}
    assert request("/api/config", settings)["success"]
    assert request("/api/config")["scrape_directory_pairs"] == pairs
    passed("two independent download/archive/Emby mappings saved and reloaded")
    invalid = request("/api/config", {"scrape_directory_pairs": [pairs[0], {**pairs[1],
        "output_dir": pairs[0]["output_dir"]}]}, expected=400)
    assert "Emby" in invalid["detail"]
    passed("conflicting Emby mappings for shared archive are rejected")

    linked = request("/api/library/scrape/test-hardlink", pairs[0])
    assert linked["supported"], linked
    crossed = request("/api/library/scrape/test-hardlink", {"watch_dir": pairs[0]["watch_dir"],
        "output_dir": "/cross-volume"})
    assert not crossed["supported"], crossed
    assert not list(ROOT.rglob(".jav-hardlink-*"))
    assert not list(Path("/cross-volume").iterdir())
    passed("real Linux hardlink succeeds on same filesystem; EXDEV fails across tmpfs; probes cleaned")

    result = request("/api/library/scrape/run-once", method="POST")
    assert result["processed"] == 2, result
    archived = []
    for pair, code in zip(pairs, ("TSTA-123", "TSTB-456")):
        source = Path(pair["watch_dir"]) / f"{code}.mp4"
        target = Path(pair["output_dir"]) / code / source.name
        assert source.samefile(target), (source, target)
        archived.append(target)
    assert request("/api/library/scrape/run-once", method="POST")["processed"] == 0
    forced = request("/api/library/scrape/run-once?mapping_index=1&force=true", method="POST")
    assert forced["processed"] == 1, forced
    assert forced["directory_pairs"][0]["emby_media_root"] == "/media/b"
    assert len(list((ROOT / "archive").rglob("*.mp4"))) == 2
    passed("both groups archive by hardlink; normal repeat skips; force targets only group B without duplicates")

    # Run the real portrait synchronization code in another process using saved
    # global settings, as a restarted retry worker would. Stubs capture HTTP paths
    # and uploaded bytes; no image download or live Emby server is involved.
    for group, target in zip(("a", "b"), archived):
        notified_before = len(NOTIFIED)
        portrait = target.parent / "actors" / "Test Actor.jpg"
        portrait.parent.mkdir(exist_ok=True)
        portrait.write_bytes((b"LOCAL PORTRAIT " + group.encode()) * 200)
        code = (
            "import asyncio; from pathlib import Path; import actor_scraper; from config_manager import load; "
            f"result=asyncio.run(actor_scraper.sync_emby_folder(Path({str(target.parent)!r}), load(), "
            "actors=[{'name':'Test Actor','avatar':'https://unused.invalid/remote.jpg'}])); "
            "assert result['emby_updated']==1, result"
        )
        subprocess.run([sys.executable, "-c", code], check=True)
        expected_folder = f"/media/{group}/{target.parent.name}"
        updates = NOTIFIED[notified_before:]
        assert any(item["Path"] == expected_folder for item in updates), updates
        assert all(item["Path"] == expected_folder or item["Path"].startswith(expected_folder + "/")
                   for item in updates), updates
        assert UPLOADS[-1] == portrait.read_bytes()
    passed("real Emby client notifies each group's mapped folder and uploads LOCAL portrait bytes")

    assert request("/api/config", {"scrape_enabled": True})["success"]
    assert request("/api/library/scrape/monitor")["enabled"]
    assert request("/api/config", {"scrape_enabled": False, "qb_url": ""})["success"]
    for _ in range(50):
        status = request("/api/library/scrape/monitor")
        if not status["running"]:
            break
        time.sleep(0.1)
    assert not status["enabled"] and not status["running"], status
    passed("master switch starts/stops the real background monitor")
    server.shutdown()
    print("DOCKER SMOKE TEST PASSED", flush=True)


if __name__ == "__main__":
    main()

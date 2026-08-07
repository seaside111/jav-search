import asyncio
import os
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import library
import actor_scraper
import emby
import qbittorrent
import transmission
import main
import intake
from scrapers import _javu_base, _javbus_base, _fsgate, jav321, dmm, javdb
from config_manager import DEFAULT_CONFIG


class JavDbFlareSolverrTests(unittest.TestCase):
    def test_detail_resolver_prefers_jav321_code_search_over_non_durable_url(self):
        import scrapers
        detail = {
            "code": "CLOT-041", "source": "JAV321", "detail_loaded": True,
            "samples": ["https://img.jav321/sample-1.jpg"],
            "url": "https://www.jav321.com/search",
        }
        search_mock = mock.AsyncMock(return_value=([detail], "ok"))
        enrich_mock = mock.AsyncMock(return_value=[])
        with mock.patch.object(main, "load_config", return_value={"sources": ["jav321"]}), \
                mock.patch.object(scrapers, "search_source_status", search_mock), \
                mock.patch.object(main, "enrich", enrich_mock):
            result = asyncio.run(main.api_detail_resolve(main.ResolveDetailRequest(
                code="CLOT-041", source="jav321",
                url="https://www.jav321.com/search")))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["detail"]["samples"], detail["samples"])
        search_mock.assert_awaited_once()
        enrich_mock.assert_not_awaited()

    def test_pushed_intake_keeps_clicked_detail_artwork(self):
        original_file = intake._FILE
        with tempfile.TemporaryDirectory() as raw:
            intake._FILE = Path(raw) / "pushed_intake.json"
            try:
                intake.register("ABC123", {
                    "code": "CLOT-041", "title": "title", "detail_loaded": True,
                    "samples": ["https://img.jav321/sample-1.jpg"],
                    "source_urls": {"JAV321": "https://www.jav321.com/search"},
                    "magnets": [{"link": "magnet:?xt=urn:btih:ABC123"}],
                    "score_count": "100",
                }, False)
                saved = intake._load()["abc123"]
            finally:
                intake._FILE = original_file

        self.assertEqual(saved["samples"], ["https://img.jav321/sample-1.jpg"])
        self.assertEqual(saved["source_urls"]["JAV321"], "https://www.jav321.com/search")
        self.assertEqual(saved["score_count"], "100")

    def test_scrape_artwork_reuses_detail_image_cache_bytes(self):
        cached = mock.AsyncMock(return_value=(b"cached-image", "image/jpeg"))
        with mock.patch.object(main, "fetch_image_cached", cached):
            data = asyncio.run(library._fetch_cover(
                "https://img.jav321/sample-1.jpg", None))
        self.assertEqual(data, b"cached-image")
        cached.assert_awaited_once_with("https://img.jav321/sample-1.jpg")

    def test_frontend_detail_failures_remain_retryable(self):
        html = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text(
            encoding="utf-8")
        self.assertIn("/api/details/resolve", html)
        self.assertIn("['jav321', 'javdb'].filter", html)
        self.assertNotIn("m._javdb_extra_loaded = true", html)
        self.assertNotIn("idxs.forEach(i => { if (currentResults[i]) currentResults[i].detail_loaded = true; })", html)

    def test_jav321_is_preferred_over_shielded_javdb_for_merged_artwork(self):
        import scrapers
        merged = scrapers._merge_lists([
            ("javdb", [{"code": "CLOT-041", "title": "JavDB title",
                        "cover": "https://javdb/cover.jpg", "source": "JavDB",
                        "url": "https://javdb/item"}]),
            ("jav321", [{"code": "CLOT-041", "title": "JAV321 title",
                         "cover": "https://jav321/cover.jpg", "source": "JAV321",
                         "url": "https://jav321/item",
                         "samples": ["https://jav321/sample.jpg"]}]),
        ])
        self.assertEqual(merged[0]["source"], "JAV321")
        self.assertEqual(merged[0]["cover"], "https://jav321/cover.jpg")
        self.assertEqual(merged[0]["samples"], ["https://jav321/sample.jpg"])
        self.assertEqual(merged[0]["source_urls"]["JavDB"], "https://javdb/item")

    def test_javdb_artwork_lookup_gets_slow_source_timeout_budget(self):
        import scrapers
        observed = []

        async def fake_search(*_args, **_kwargs):
            return []

        async def fake_wait_for(coro, timeout):
            observed.append(timeout)
            return await coro

        with mock.patch.object(scrapers.javdb, "search_list", fake_search), \
                mock.patch.object(scrapers.asyncio, "wait_for", fake_wait_for):
            rows, status = asyncio.run(scrapers.search_source_status(
                "CLOT-041", "code", "javdb", max_results=3))

        self.assertEqual((rows, status), ([], "ok"))
        self.assertEqual(observed, [scrapers._PER_SOURCE_TIMEOUT_DETAIL])
        self.assertGreater(observed[0], 40)

    def test_authenticated_proxy_uses_flaresolverr_session(self):
        calls = []

        class Response:
            status_code = 200
            def __init__(self, payload): self.payload = payload
            def json(self): return self.payload

        class Client:
            def __init__(self, **_kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return False
            async def post(self, _url, json):
                calls.append(json)
                if json["cmd"] == "request.get":
                    return Response({"status": "ok", "solution": {
                        "status": 200, "response": "<html>ok</html>"}})
                return Response({"status": "ok", "session": json.get("session")})

        with mock.patch.object(_fsgate.httpx, "AsyncClient", Client):
            html, status, error, connected, healthy = asyncio.run(
                _fsgate._request_one(
                    "http://fs:8191", "https://javdb.com",
                    "http://user:pass@proxy:7890", None, 40000, 70))

        self.assertEqual((status, error, connected, healthy), (200, "", True, True))
        self.assertIn("ok", html)
        self.assertEqual([call["cmd"] for call in calls],
                         ["sessions.create", "request.get", "sessions.destroy"])
        self.assertEqual(calls[0]["proxy"], {
            "url": "http://proxy:7890", "username": "user", "password": "pass"})
        self.assertNotIn("proxy", calls[1])
        self.assertEqual(calls[1]["session"], calls[0]["session"])

    def test_javdb_current_preview_markup_returns_all_original_images(self):
        html = """
        <html><title>ABC-123</title><h2 class="title"><strong>ABC-123 title</strong></h2>
        <a data-fancybox="gallery" href="https://img.test/sample-1.jpg">
          <img data-src="https://img.test/thumb-1.jpg"></a>
        <a data-fancybox="preview-gallery" href="/samples/sample-2.webp">
          <picture><source srcset="/thumb-2.webp 1x, /thumb-2@2x.webp 2x"></picture></a>
        <a data-fancybox="gallery" href="https://img.test/sample-1.jpg"></a>
        </html>
        """
        detail = javdb._parse_detail(html, "https://javdb.com/v/abc")
        self.assertEqual(detail["samples"], [
            "https://img.test/sample-1.jpg",
            "https://javdb.com/samples/sample-2.webp",
        ])

    def test_browser_session_cookies_are_not_forwarded_to_flaresolverr(self):
        calls = []

        async def fake_fetch(url, flaresolverr_url, proxy, cookies=None, priority=0):
            calls.append({"url": url, "proxy": proxy, "cookies": cookies})
            return "<html><title>JavDB</title></html>", 200, ""

        opts = {
            "flaresolverr_url": "http://flaresolverr:8191",
            "flaresolverr_use_proxy": True,
            "cookie": "cf_clearance=secret; _jdb_session=session; custom=value",
        }
        with mock.patch.object(javdb, "_fetch_via_flaresolverr", fake_fetch):
            html, status, error = asyncio.run(javdb._fetch_html(
                "https://javdb.com/censored", "http://proxy:7890", opts))

        self.assertEqual(status, 200)
        self.assertEqual(error, "")
        self.assertIn("JavDB", html)
        self.assertEqual(calls[0]["cookies"], javdb.BASE_COOKIES)
        self.assertNotIn("cf_clearance", calls[0]["cookies"])
        self.assertNotIn("_jdb_session", calls[0]["cookies"])


class VideoClassificationTests(unittest.TestCase):
    def test_emby_name_key_normalizes_japanese_width_and_hidden_marks(self):
        expected = emby._name_key("チーチー")
        self.assertEqual(emby._name_key("ﾁｰﾁｰ"), expected)
        self.assertEqual(emby._name_key("チ\u200bーチー"), expected)

    def test_emby_primary_verification_requires_image_tag_change(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return [{"ImageType": "Primary", "ImageTag": "same-tag"}]

        class Client:
            async def get(self, *_args, **_kwargs):
                return Response()

        unchanged = asyncio.run(emby._verify_primary(
            Client(), "http://emby", {}, "person", "same-tag"))
        changed = asyncio.run(emby._verify_primary(
            Client(), "http://emby", {}, "person", "old-tag"))
        self.assertEqual(unchanged, (False, "same-tag"))
        self.assertEqual(changed, (True, "same-tag"))

    def test_emby_person_fallback_matches_half_width_katakana(self):
        class Response:
            def __init__(self, status=200, payload=None):
                self.status_code = status
                self._payload = payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            def json(self):
                return self._payload

        class Client:
            uploads = []

            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, url, **kwargs):
                if url.endswith("/Library/Media/Updated"):
                    return Response(204)
                if "/Images/Primary" in url:
                    Client.uploads.append(url)
                    return Response(204)
                raise AssertionError(url)

            async def get(self, url, **kwargs):
                if url.endswith("/Persons") and "SearchTerm" in kwargs.get("params", {}):
                    return Response(200, {"Items": [], "TotalRecordCount": 0})
                if "/Persons/" in url:
                    return Response(404)
                if url.endswith("/Persons"):
                    start = kwargs.get("params", {}).get("StartIndex", 0)
                    item = ({"Name": "Other Person", "Id": "other"} if start == 0 else
                            {"Name": "ﾁｰﾁｰ", "Id": "katakana-person"})
                    return Response(200, {"Items": [item], "TotalRecordCount": 2})
                if url.endswith("/Images"):
                    return Response(200, [{"ImageType": "Primary"}])
                raise AssertionError(url)

        original_client = emby.httpx.AsyncClient
        emby.httpx.AsyncClient = Client
        try:
            result = asyncio.run(emby.sync_person_images(
                "http://emby:8096", "admin-key",
                [{"name": "チーチー", "image": b"local-image"}],
                media_paths=["/media/ABC-123"], poll_delays=()))
        finally:
            emby.httpx.AsyncClient = original_client
        self.assertTrue(result["results"][0]["updated"])
        self.assertEqual(Client.uploads, [
            "http://emby:8096/Items/katakana-person/Images/Primary"])

    def test_emby_batch_notifies_only_current_media_and_verifies_people(self):
        class Response:
            def __init__(self, status=200, payload=None):
                self.status_code = status
                self._payload = payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            def json(self):
                return self._payload

        class Client:
            media_updates = []
            uploads = []
            searches = {}

            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, url, **kwargs):
                if url.endswith("/Library/Media/Updated"):
                    Client.media_updates.append(kwargs.get("json"))
                    return Response(204)
                if "/Images/Primary" in url:
                    Client.uploads.append((url, kwargs.get("content")))
                    return Response(204)
                raise AssertionError(url)

            async def get(self, url, **kwargs):
                if url.endswith("/Persons"):
                    name = kwargs["params"]["SearchTerm"]
                    Client.searches[name] = Client.searches.get(name, 0) + 1
                    items = [] if Client.searches[name] == 1 else [{"Name": name, "Id": f"id-{name}"}]
                    return Response(200, {"Items": items})
                if url.endswith("/Images"):
                    return Response(200, [{"ImageType": "Primary"}])
                raise AssertionError(url)

        original_client = emby.httpx.AsyncClient
        original_sleep = emby.asyncio.sleep

        async def no_sleep(_seconds):
            return None

        emby.httpx.AsyncClient = Client
        emby.asyncio.sleep = no_sleep
        try:
            result = asyncio.run(emby.sync_person_images(
                "http://emby:8096", "admin-key", [
                    {"name": "Actor A", "image": b"image-a"},
                    {"name": "Actor B", "image": b"image-b"},
                ], media_paths=["/media/202608/SAN-475", "/media/202608/SAN-475/SAN-475.mp4"],
                poll_delays=(0,)))
        finally:
            emby.httpx.AsyncClient = original_client
            emby.asyncio.sleep = original_sleep
        self.assertTrue(result["media_update_triggered"])
        self.assertEqual(Client.media_updates, [{"Updates": [
            {"Path": "/media/202608/SAN-475", "UpdateType": "Created"},
            {"Path": "/media/202608/SAN-475/SAN-475.mp4", "UpdateType": "Created"},
        ]}])
        self.assertEqual(len(Client.uploads), 2)
        self.assertTrue(all(item["updated"] for item in result["results"]))

    def test_emby_finds_person_id_from_current_movie_people(self):
        class Response:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self._payload

        class Client:
            async def get(self, url, **kwargs):
                self.path = kwargs["params"]["Path"]
                return Response({"Items": [{
                    "Id": "movie-id", "People": [
                        {"Name": "逢沢みゆ", "Id": "person-id", "Type": "Actor"}
                    ]
                }]})

        client = Client()
        result = asyncio.run(emby._find_people_from_media(
            client, "http://emby", {}, ["逢沢みゆ"],
            ["/media/NACT-163", "/media/NACT-163/NACT-163.mp4"]))
        self.assertEqual(result["逢沢みゆ"]["Id"], "person-id")
        self.assertEqual(client.path, "/media/NACT-163/NACT-163.mp4")

    def test_actor_nfo_is_written_before_emby_media_notification(self):
        original_sync = actor_scraper.emby.sync_person_images

        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            nfo = folder / "ABC-123.nfo"
            nfo.write_text("<movie><uniqueid>ABC-123</uniqueid></movie>", encoding="utf-8")
            tree = actor_scraper.ET.parse(nfo)
            actor_dir = folder / "actors"
            actor_dir.mkdir()
            (actor_dir / "Actor A.jpg").write_bytes(b"x" * 2048)

            async def fake_sync(_url, _key, portraits, media_paths=None):
                current = nfo.read_text(encoding="utf-8")
                self.assertIn("Actor A", current)
                self.assertEqual([item["name"] for item in portraits], ["Actor A"])
                self.assertIn(str(folder.resolve()), media_paths)
                return {"media_update_triggered": True, "results": [
                    {"name": "Actor A", "updated": True, "message": "ok"}]}

            actor_scraper.emby.sync_person_images = fake_sync
            try:
                result = asyncio.run(actor_scraper.process_movie(
                    folder, [{"name": "Actor A", "avatar": "https://img/a.jpg"}], "ABC-123",
                    {"actor_scrape_sources": ["javbus"],
                     "actor_scrape_lookup_by_code": False,
                     "actor_scrape_write_nfo": True,
                     "scrape_actor_thumb_in_nfo": True,
                     "actor_scrape_cache_dir": str(folder / "cache"),
                     "emby_actor_sync_enabled": True,
                     "emby_url": "http://emby:8096", "emby_api_key": "key"},
                    nfo, tree))
            finally:
                actor_scraper.emby.sync_person_images = original_sync
        self.assertEqual(result["emby_updated"], 1)

    def test_cached_actor_metadata_restores_thumb_url_into_nfo(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw) / "movie"
            cache = Path(raw) / "cache" / "Actor A"
            folder.mkdir()
            cache.mkdir(parents=True)
            (cache / "portrait.jpg").write_bytes(b"x" * 2048)
            (cache / "metadata.json").write_text(
                '{"url":"https://img.test/a.jpg","source":"javbus"}', encoding="utf-8")
            nfo = folder / "ABC-123.nfo"
            nfo.write_text("<movie><actor><name>Actor A</name></actor></movie>", encoding="utf-8")
            tree = actor_scraper.ET.parse(nfo)
            result = asyncio.run(actor_scraper.process_movie(
                folder, [{"name": "Actor A", "avatar": ""}], "ABC-123",
                {"actor_scrape_cache_dir": str(Path(raw) / "cache"),
                 "actor_scrape_sources": ["javbus"],
                 "actor_scrape_lookup_by_code": True,
                 "actor_scrape_write_nfo": True,
                 "scrape_actor_thumb_in_nfo": True},
                nfo, tree, sync_emby=False))
            written = nfo.read_text(encoding="utf-8")
        self.assertEqual(result["saved"], 1)
        self.assertIn("<thumb>https://img.test/a.jpg</thumb>", written)

    def test_local_actor_image_without_metadata_recovers_url_without_download(self):
        original_code_lookup = actor_scraper._actors_by_code
        original_download = actor_scraper._download
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw) / "movie"
            actors_dir = folder / "actors"
            actors_dir.mkdir(parents=True)
            (actors_dir / "Actor A.jpg").write_bytes(b"x" * 2048)
            nfo = folder / "ABC-123.nfo"
            nfo.write_text("<movie><actor><name>Actor A</name></actor></movie>", encoding="utf-8")
            tree = actor_scraper.ET.parse(nfo)

            async def fake_lookup(*_args, **_kwargs):
                return [{"name": "Actor A", "avatar": "https://img.test/recovered.jpg"}]

            async def fail_download(*_args, **_kwargs):
                raise AssertionError("existing portrait must not be downloaded again")

            actor_scraper._actors_by_code = fake_lookup
            actor_scraper._download = fail_download
            try:
                result = asyncio.run(actor_scraper.process_movie(
                    folder, [{"name": "Actor A", "avatar": ""}], "ABC-123",
                    {"actor_scrape_cache_dir": str(Path(raw) / "cache"),
                     "actor_scrape_sources": ["javbus"],
                     "actor_scrape_lookup_by_code": True,
                     "actor_scrape_write_nfo": True,
                     "scrape_actor_thumb_in_nfo": True},
                    nfo, tree, sync_emby=False))
            finally:
                actor_scraper._actors_by_code = original_code_lookup
                actor_scraper._download = original_download
            metadata = (Path(raw) / "cache" / "Actor A" / "metadata.json").read_text(encoding="utf-8")
            written = nfo.read_text(encoding="utf-8")
        self.assertEqual(result["saved"], 1)
        self.assertIn("<thumb>https://img.test/recovered.jpg</thumb>", written)
        self.assertIn("https://img.test/recovered.jpg", metadata)

    def test_permanent_actor_portrait_404_is_removed_from_nfo(self):
        original_download = actor_scraper._download
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw) / "movie"
            folder.mkdir()
            nfo = folder / "FNS-232.nfo"
            nfo.write_text("""<movie><uniqueid>FNS-232</uniqueid><actor>
              <name>Actor A</name><thumb>https://img.test/missing.jpg</thumb>
            </actor></movie>""", encoding="utf-8")
            tree = actor_scraper.ET.parse(nfo)

            async def gone(*_args, **_kwargs):
                return None, "gone"

            actor_scraper._download = gone
            try:
                result = asyncio.run(actor_scraper.process_movie(
                    folder, [{"name": "Actor A",
                              "avatar": "https://img.test/missing.jpg"}], "FNS-232",
                    {"actor_scrape_lookup_by_code": False,
                     "actor_scrape_write_nfo": True,
                     "scrape_actor_thumb_in_nfo": True,
                     "actor_scrape_cache_dir": str(Path(raw) / "cache")},
                    nfo, tree, sync_emby=False))
            finally:
                actor_scraper._download = original_download
            written = nfo.read_text(encoding="utf-8")
        self.assertEqual(result["saved"], 0)
        self.assertNotIn("<thumb>", written)

    def test_emby_archive_root_maps_only_current_movie_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            archive = Path(raw) / "archive"
            folder = archive / "202608" / "SAN-475"
            folder.mkdir(parents=True)
            (folder / "SAN-475.mp4").write_bytes(b"video")
            (folder / "SAN-475.nfo").write_text("<movie/>", encoding="utf-8")
            (folder / "ignore.jpg").write_bytes(b"cover")
            paths, error = actor_scraper._emby_media_paths(folder, {
                "scrape_output_dir": str(archive),
                "emby_media_root": "/data/av/jp",
            })
        self.assertEqual(error, "")
        self.assertEqual(paths, [
            "/data/av/jp/202608/SAN-475",
            "/data/av/jp/202608/SAN-475/SAN-475.mp4",
            "/data/av/jp/202608/SAN-475/SAN-475.nfo",
        ])

    def test_library_syncs_emby_only_after_archive_finishes(self):
        original_scrape = library._scrape_one
        original_archive = library._archive_file
        original_sync = actor_scraper.sync_emby_folder
        events = []
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "downloads" / "SAN-475.mp4"
            video.parent.mkdir()
            video.write_bytes(b"video")
            target = root / "archive" / "202608" / "SAN-475"

            async def fake_scrape(*_args, **_kwargs):
                events.append("scrape")
                return {"success": True, "code": "SAN-475", "actors": [
                    {"name": "Actor A", "avatar": "https://img/a.jpg"}],
                    "actor_images_saved": 1, "title_original": "", "folder_title": ""}

            def fake_archive(*_args, **_kwargs):
                events.append("archive")
                target.mkdir(parents=True)
                (target / "SAN-475.mp4").write_bytes(b"video")
                return {"archived": True, "moved_original": False,
                        "target_dir": str(target), "files": ["SAN-475.mp4"]}

            async def fake_sync(folder, _config, code, _actors):
                events.append("emby")
                self.assertEqual(folder, target)
                self.assertEqual(code, "SAN-475")
                self.assertTrue((folder / "SAN-475.mp4").exists())
                return {"emby_updated": 1, "message": "ok"}

            library._scrape_one = fake_scrape
            library._archive_file = fake_archive
            actor_scraper.sync_emby_folder = fake_sync
            try:
                result = asyncio.run(library._process_completed_file(video, {
                    "scrape_output_dir": str(root / "archive"),
                    "scrape_meta_enabled": True, "scrape_organize_enabled": True,
                    "archive_enabled": True, "archive_mode": "hardlink",
                    "archive_by_month": True, "scrape_move_on_fail": True,
                    "scrape_watch_dir": str(video.parent),
                    "emby_actor_sync_enabled": True,
                }))
            finally:
                library._scrape_one = original_scrape
                library._archive_file = original_archive
                actor_scraper.sync_emby_folder = original_sync
        self.assertEqual(events, ["scrape", "archive", "emby"])
        self.assertEqual(result["emby_updated"], 1)

    def test_javbus_current_star_link_portrait_markup(self):
        html = """
        <div class="container"><h3>SDMUA-095 title</h3></div>
        <div class="star-name">
          <a href="https://www.javbus.com/star/s0i">優梨まいな</a>
        </div>
        """
        detail = _javbus_base.parse_detail(
            html, "https://www.javbus.com/SDMUA-095",
            "https://www.javbus.com", "JavBus")
        self.assertEqual(detail["actors"], [{
            "name": "優梨まいな",
            "avatar": "https://www.javbus.com/pics/actress/s0i_a.jpg",
        }])

    def test_javbus_multi_actor_rejects_placeholder_portrait(self):
        html = """
        <div class="container"><h3>SAN-477 title</h3></div>
        <a href="https://www.javbus.com/star/u3u">
          <img src="/pics/actress/u3u_a.jpg" title="優木なお">
        </a>
        <div class="star-name"><a href="https://www.javbus.com/star/u3u">優木なお</a></div>
        <a href="https://www.javbus.com/star/13vj">
          <img src="https://pics.dmm.co.jp/mono/actjpgs/nowprinting.gif" title="尋井うみ">
        </a>
        <div class="star-name"><a href="https://www.javbus.com/star/13vj">尋井うみ</a></div>
        """
        detail = _javbus_base.parse_detail(
            html, "https://www.javbus.com/SAN-477",
            "https://www.javbus.com", "JavBus")
        self.assertEqual([actor["name"] for actor in detail["actors"]],
                         ["優木なお", "尋井うみ"])
        self.assertTrue(detail["actors"][0]["avatar"].endswith("/u3u_a.jpg"))
        self.assertEqual(detail["actors"][1]["avatar"], "")

    def test_read_nfo_deduplicates_multi_actor_entries(self):
        with tempfile.TemporaryDirectory() as raw:
            nfo = Path(raw) / "ABC-123.nfo"
            nfo.write_text("""<movie>
              <actor><name>Actor A</name><thumb>https://img/a.jpg</thumb></actor>
              <actor><name>Actor-A</name><thumb>https://img/duplicate.jpg</thumb></actor>
              <actor><name>Actor B</name><thumb>https://img/nowprinting.gif</thumb></actor>
            </movie>""", encoding="utf-8")
            _tree, actors, _code = actor_scraper._read_nfo(nfo)
        self.assertEqual(actors, [
            {"name": "Actor A", "avatar": "https://img/a.jpg"},
            {"name": "Actor B", "avatar": ""},
        ])

    def test_actor_details_keep_already_loaded_live_result(self):
        original_search = actor_scraper.search
        original_enrich = actor_scraper.enrich

        async def fake_search(**_kwargs):
            return [{"code": "SDMUA-095", "detail_loaded": True,
                     "actors": [{"name": "優梨まいな", "avatar": "https://live/a.jpg"}]}]

        async def stale_enrich(*_args, **_kwargs):
            raise AssertionError("已加载的详情不应再次读取旧缓存")

        actor_scraper.search = fake_search
        actor_scraper.enrich = stale_enrich
        try:
            rows = asyncio.run(actor_scraper._details(
                "SDMUA-095", "code", "javbus", None))
        finally:
            actor_scraper.search = original_search
            actor_scraper.enrich = original_enrich
        self.assertEqual(rows[0]["actors"][0]["avatar"], "https://live/a.jpg")

    def test_download_completion_uses_client_api_fields(self):
        # 即使磁盘已预分配到最终大小，只要下载器进度/剩余字节未完成就必须是 False。
        self.assertFalse(qbittorrent._torrent_completed({
            "progress": 0.35, "amount_left": 1024, "size": 10_000_000_000}))
        self.assertTrue(qbittorrent._torrent_completed({
            "progress": 1.0, "amount_left": 0, "size": 10_000_000_000}))
        self.assertFalse(transmission._torrent_completed({
            "percentDone": 0.8, "leftUntilDone": 2048, "totalSize": 10_000_000_000}))
        self.assertTrue(transmission._torrent_completed({
            "percentDone": 1.0, "leftUntilDone": 0, "totalSize": 10_000_000_000}))

    def test_matches_downloader_task_across_mount_prefixes(self):
        with tempfile.TemporaryDirectory() as raw:
            watch = Path(raw)
            video = watch / "SAN-475" / "part1.mp4"
            video.parent.mkdir()
            video.write_bytes(b"x")
            task = {"name": "SAN-475", "content_path": "/downloads/SAN-475",
                    "completed": False}
            self.assertIs(library._match_downloader_torrent(video, watch, [task]), task)
            tr_task = {"name": "different", "files": ["SAN-475/part1.mp4"],
                       "completed": False}
            self.assertIs(library._match_downloader_torrent(video, watch, [tr_task]), tr_task)

    def test_keeps_unique_and_large_videos_and_drops_small_ad(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            main = root / "ABC-123.mp4"
            large = root / "bonus.mp4"
            ad = root / "ad.mp4"
            main.write_bytes(b"x" * 1000)
            large.write_bytes(b"x" * 600)
            ad.write_bytes(b"x" * 10)
            keep, drop = library.classify_videos(
                [main, large, ad], str(root), min_bytes=100, keep_bytes=500)
            self.assertIn(main, keep)
            self.assertIn(large, keep)
            self.assertIn(ad, drop)

    def test_segment_marker_is_never_dropped(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            main = root / "ABC-123-CD1.mp4"
            second = root / "ABC-123-CD2.mp4"
            main.write_bytes(b"x" * 1000)
            second.write_bytes(b"x" * 150)
            keep, drop = library.classify_videos(
                [main, second], str(root), min_bytes=200, keep_bytes=500)
            self.assertIn(second, keep)
            self.assertNotIn(second, drop)

    def test_same_duration_different_resolution_is_quality_variant(self):
        a = Path("ABC-123 source-a.mp4")
        b = Path("ABC-123 source-b.mp4")
        original = library._probe_video
        facts = {
            a: {"duration": 7200.0, "width": 1920, "height": 1080, "codec": "h264"},
            b: {"duration": 7201.0, "width": 3840, "height": 2160, "codec": "hevc"},
        }
        library._probe_video = lambda path: facts[path]
        try:
            self.assertTrue(library._is_quality_variant(a, b))
        finally:
            library._probe_video = original

    def test_real_cd_parts_are_not_quality_variants(self):
        a = Path("ABC-123-CD1.mp4")
        b = Path("ABC-123-CD2.mp4")
        original = library._probe_video
        facts = {
            a: {"duration": 3600.0, "width": 1920, "height": 1080, "codec": "h264"},
            b: {"duration": 4100.0, "width": 1920, "height": 1080, "codec": "h264"},
        }
        library._probe_video = lambda path: facts[path]
        try:
            self.assertFalse(library._is_quality_variant(a, b))
        finally:
            library._probe_video = original

    def test_only_higher_resolution_variant_is_archive_representative(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            low = root / "ABC-123 source-a.mp4"
            high = root / "ABC-123 source-b.mp4"
            low.write_bytes(b"low")
            high.write_bytes(b"high quality")
            original = library._probe_video
            facts = {
                low: {"duration": 7200.0, "width": 1280, "height": 720, "codec": "h264"},
                high: {"duration": 7201.0, "width": 3840, "height": 2160, "codec": "hevc"},
            }
            library._probe_video = lambda path: facts[path]
            try:
                self.assertTrue(library._is_nonpreferred_variant(low, "ABC-123", str(root)))
                self.assertFalse(library._is_nonpreferred_variant(high, "ABC-123", str(root)))
                self.assertEqual(library._part_suffix(high, "ABC-123", str(root)), "")
            finally:
                library._probe_video = original

    def test_artwork_uses_independent_sample_for_fanart(self):
        self.assertEqual(
            library._artwork_urls({
                "cover": "https://img/poster.jpg",
                "samples": ["https://img/poster.jpg", "https://img/backdrop.jpg"],
            }),
            ("https://img/poster.jpg", "https://img/backdrop.jpg"),
        )
        self.assertEqual(
            library._artwork_urls({"cover": "https://img/poster.jpg", "samples": []}),
            ("https://img/poster.jpg", ""),
        )

    def test_artwork_merge_fills_missing_fields_without_overwrite(self):
        def png(width, height):
            return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
                    + width.to_bytes(4, "big") + height.to_bytes(4, "big"))

        self.assertFalse(library._is_fanart_image(png(800, 1200)))
        self.assertFalse(library._is_fanart_image(png(600, 338)))
        self.assertTrue(library._is_fanart_image(png(1280, 720)))

        source = Image.new("RGB", (1500, 1000), "red")
        source.paste(Image.new("RGB", (750, 1000), "blue"), (750, 0))
        raw = BytesIO()
        source.save(raw, format="JPEG", quality=95)
        unchanged, rejected = library._poster_bytes(raw.getvalue())
        self.assertFalse(rejected)
        self.assertEqual(unchanged, raw.getvalue())
        poster, cropped = library._poster_bytes(raw.getvalue(), confirmed_jacket=True)
        with Image.open(BytesIO(poster)) as image:
            self.assertTrue(cropped)
            self.assertEqual(image.size, (795, 1000))
            # The crop includes a narrow strip left of the centre seam while
            # retaining the complete right/front panel.
            self.assertGreater(image.getpixel((20, 500))[0], 150)
            self.assertGreater(image.getpixel((700, 500))[2], 200)

        self.assertTrue(library._is_confirmed_jacket_cover({
            "source": "JavBus",
            "cover": "https://img.example/pics/cover/abc_b.jpg",
            "samples": ["https://img.example/samples/abc-1.jpg"],
        }, "https://img.example/pics/cover/abc_b.jpg"))
        self.assertTrue(library._is_confirmed_jacket_cover({
            "source": "JavDB",
            "cover": "https://c0.jdbstatic.com/covers/ab/cover.jpg",
            "samples": [],
        }, "https://c0.jdbstatic.com/covers/ab/cover.jpg"))
        self.assertFalse(library._is_confirmed_jacket_cover({
            "source": "JavDB",
            "cover": "https://c0.jdbstatic.com/samples/still.jpg",
            "samples": ["https://c0.jdbstatic.com/samples/still.jpg"],
        }, "https://c0.jdbstatic.com/samples/still.jpg"))

        movie = {"cover": "https://primary/poster.jpg", "samples": []}
        self.assertTrue(library._merge_artwork(movie, {
            "cover": "https://fallback/poster.jpg",
            "samples": ["https://fallback/fanart.jpg"],
        }, "javdb"))
        self.assertEqual(movie["cover"], "https://primary/poster.jpg")
        self.assertEqual(movie["samples"], ["https://fallback/fanart.jpg"])
        self.assertEqual(movie["fanart_source"], "javdb")

    def test_artwork_backfill_stops_after_first_successful_source(self):
        import scrapers
        movie = {
            "code": "ABC-123", "cover": "https://primary/poster.jpg", "samples": [],
            "source": "JavBus", "url": "https://javbus/item",
            "source_urls": {
                "JavBus": "https://javbus/item",
                "JavDB": "https://javdb/item",
                "AVSOX": "https://avsox/item",
            },
        }
        original = scrapers.enrich
        calls = []

        async def fake_enrich(items, **kwargs):
            calls.append(items[0]["source"])
            return [{"samples": ["https://javdb/fanart.jpg"]}]

        scrapers.enrich = fake_enrich
        try:
            asyncio.run(library._backfill_artwork(
                movie, "ABC-123",
                {"sources": ["javbus", "javdb", "avsox"],
                 "scrape_artwork_fallback_limit": 2}, None))
        finally:
            scrapers.enrich = original
        self.assertEqual(calls, ["javdb"])
        self.assertEqual(movie["samples"], ["https://javdb/fanart.jpg"])

    def test_artwork_backfill_continues_to_next_enabled_source(self):
        import scrapers
        movie = {
            "code": "ABC-123", "cover": "https://primary/poster.jpg", "samples": [],
            "source_urls": {
                "JavDB": "https://javdb/item",
                "AVSOX": "https://avsox/item",
                "JavBus": "https://javbus/item",
            },
        }
        original = scrapers.enrich
        calls = []

        async def fake_enrich(items, **kwargs):
            source = items[0]["source"]
            calls.append(source)
            if source == "avsox":
                return [{"samples": ["https://avsox/fanart.jpg"]}]
            return [None]

        scrapers.enrich = fake_enrich
        try:
            asyncio.run(library._backfill_artwork(
                movie, "ABC-123",
                {"sources": ["javbus", "javdb", "avsox"],
                 "scrape_artwork_fallback_limit": 1}, None))
        finally:
            scrapers.enrich = original
        self.assertEqual(calls, ["javdb", "avsox"])
        self.assertEqual(movie["samples"], ["https://avsox/fanart.jpg"])

    def test_artwork_timeouts_do_not_consume_effective_source_limit(self):
        import scrapers
        movie = {
            "code": "ABC-123", "cover": "https://primary/poster.jpg", "samples": [],
            "source_urls": {
                "DMM/FANZA": "dmmapi://abc00123",
                "JAV321": "https://jav321/item",
                "JavDB": "https://javdb/item",
            },
        }
        original = scrapers.enrich
        calls = []

        async def fake_enrich(items, **kwargs):
            source = items[0]["source"]
            calls.append(source)
            if source == "javdb":
                return [({"samples": ["https://javdb/fanart.jpg"]}, "ok")]
            return [(None, "timeout")]  # 模拟 DMM 与 JAV321 超时/请求失败

        scrapers.enrich = fake_enrich
        try:
            asyncio.run(library._backfill_artwork(
                movie, "ABC-123",
                {"sources": ["dmm", "jav321", "javdb"],
                 "scrape_artwork_fallback_limit": 1}, None))
        finally:
            scrapers.enrich = original
        self.assertEqual(calls, ["dmm", "jav321", "javdb"])
        self.assertEqual(movie["samples"], ["https://javdb/fanart.jpg"])

    def test_ambiguous_same_code_files_are_not_guessed_as_cd_parts(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = root / "ABC-123 main.mp4"
            second = root / "ABC-123 alternate.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            with mock.patch.object(library, "_probe_video", return_value={}):
                self.assertEqual(library._part_suffix(first, "ABC-123", str(root)), "")
                self.assertEqual(library._part_suffix(second, "ABC-123", str(root)), "")

    def test_jav321_parser_keeps_cover_and_sample_separate(self):
        html = """
        <html><h3>ABC-123 Sample title</h3><div>品番：ABC-123</div>
        <div class="col-md-3"><img src="/images/cover.jpg"></div>
        <div id="sample-waterfall"><a href="/images/sample1.jpg">sample</a></div>
        </html>
        """
        item = jav321._parse(html, query="ABC-123", url="https://www.jav321.com/video/abc-123")
        self.assertEqual(item["cover"], "https://www.jav321.com/images/cover.jpg")
        self.assertEqual(item["samples"], ["https://www.jav321.com/images/sample1.jpg"])

    def test_jav321_parser_reads_current_wide_column_gallery(self):
        html = """
        <html><h3>CLOT-041 title</h3><div>品番：CLOT-041</div>
        <div class="col-md-3"><img class="img-responsive" src="/cover/clot041.jpg"></div>
        <div class="col-md-9" id="video_info">
          <a href="https://pics.test/clot041jp-1.jpg"><img src="/thumb/one.jpg"></a>
          <picture><source srcset="https://pics.test/clot041jp-2.webp 1x"></picture>
          <img data-lazy-src="https://pics.test/clot041jp-3.jpg">
          <img src="/images/logo.png">
        </div></html>
        """
        item = jav321._parse(
            html, query="CLOT-041", url="https://www.jav321.com/video/clot00041")
        self.assertEqual(item["cover"], "https://www.jav321.com/cover/clot041.jpg")
        self.assertEqual(item["samples"], [
            "https://pics.test/clot041jp-1.jpg",
            "https://pics.test/clot041jp-2.webp",
            "https://pics.test/clot041jp-3.jpg",
        ])

    def test_dmm_api_mapping_exposes_official_cover_and_samples(self):
        item = dmm._item({
            "content_id": "ssis00123", "title": "Title",
            "imageURL": {"large": "https://pics.dmm.co.jp/cover.jpg"},
            "sampleImageURL": {"sample_l": {"image": ["https://pics.dmm.co.jp/sample.jpg"]}},
            "maker": {"name": "Maker"}, "actress": [{"name": "Actor"}],
        })
        self.assertEqual(item["code"], "SSIS-123")
        self.assertEqual(item["cover"], "https://pics.dmm.co.jp/cover.jpg")
        self.assertEqual(item["samples"], ["https://pics.dmm.co.jp/sample.jpg"])
        self.assertTrue(item["detail_loaded"])

    def test_dmm_code_search_rejects_nonmatching_first_result(self):
        original = dmm._api

        async def fake_api(params, proxy):
            return [{"content_id": "other00999", "title": "OTHER-999"}]

        dmm._api = fake_api
        try:
            result = asyncio.run(dmm.search_list("ABC-123", "code"))
        finally:
            dmm._api = original
        self.assertEqual(result, [])


class NamingAndTrackerTests(unittest.TestCase):
    def test_javu_search_uses_current_object_parameter_shape(self):
        original = _javu_base._call
        captured = {}

        async def fake_call(bases, method, body, proxy, source):
            captured.update(method=method, body=body)
            return [], bases[0]

        _javu_base._call = fake_call
        try:
            asyncio.run(_javu_base.search_list(
                "https://example.test", "AVSOX", "LOCK-014", "code", None, 6))
        finally:
            _javu_base._call = original
        self.assertEqual(captured["method"], "search")
        self.assertEqual(captured["body"], [{
            "search": "LOCK-014", "page": 1, "pageSize": 30, "lang": "cn"}])

    def test_push_buttons_use_clicked_node_and_page_unique_ids(self):
        frontend = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text(
            encoding="utf-8")
        self.assertIn('id="qb-${idx}-${i}"', frontend)
        self.assertIn("pushToQb('${escAttr(qbUrl)}', this,", frontend)
        self.assertNotIn('id="qb-${i}"', frontend)

    def test_sukebei_is_default_resource_source(self):
        self.assertFalse(DEFAULT_CONFIG["jackett_enabled"])
        paths = {route.path for route in main.app.routes}
        self.assertIn("/api/resources/search", paths)
        self.assertIn("/api/jackett/search", paths)

    def test_all_actors_folder_name(self):
        name = library._archive_folder_name(
            "ABC-123", "", "", [{"name": "ActorA"}, {"name": "ActorB"}],
            {"scrape_folder_naming": "code_actor", "scrape_folder_actor_mode": "all"})
        self.assertEqual(name, "ABC-123 ActorA ActorB")

    def test_title_and_actor_folder_name(self):
        name = library._archive_folder_name(
            "ABC-123", "A Title", "中文标题", [{"name": "ActorA"}],
            {"scrape_folder_naming": "code_title_actor",
             "scrape_folder_title_translate": True,
             "scrape_folder_actor_mode": "first"})
        self.assertEqual(name, "ABC-123 中文标题 ActorA")

    def test_title_translation_never_changes_japanese_actor_name(self):
        name = library._archive_folder_name(
            "ABC-123", "元のタイトル", "中文标题", [{"name": "葵つかさ"}],
            {"scrape_folder_naming": "code_title_actor",
             "scrape_folder_title_translate": True,
             "scrape_folder_actor_mode": "first"})
        self.assertEqual(name, "ABC-123 中文标题 葵つかさ")
        self.assertIn("葵つかさ", name)

    def test_archive_transfer_same_path_never_deletes_movie(self):
        with tempfile.TemporaryDirectory() as raw:
            movie = Path(raw) / "ABC-123.mp4"
            movie.write_bytes(b"movie")
            self.assertTrue(library._transfer(movie, movie, "move"))
            self.assertEqual(movie.read_bytes(), b"movie")

    def test_transfer_modes_preserve_expected_source_state(self):
        for mode in ("hardlink", "copy", "move"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                src = root / "source.mp4"
                dst = root / "archive" / "movie.mp4"
                dst.parent.mkdir()
                src.write_bytes(b"complete movie bytes")
                self.assertTrue(library._transfer(src, dst, mode))
                self.assertEqual(dst.read_bytes(), b"complete movie bytes")
                self.assertEqual(src.exists(), mode != "move")
                if mode == "hardlink":
                    self.assertEqual(src.stat().st_ino, dst.stat().st_ino)

    def test_transfer_never_overwrites_existing_target(self):
        for mode in ("hardlink", "copy", "move"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                src = root / "source.mp4"
                dst = root / "archive.mp4"
                src.write_bytes(b"new movie")
                dst.write_bytes(b"existing movie")
                self.assertFalse(library._transfer(src, dst, mode))
                self.assertEqual(src.read_bytes(), b"new movie")
                self.assertEqual(dst.read_bytes(), b"existing movie")

    def test_archive_modes_only_rename_destination_and_move_only_removes_source(self):
        for mode in ("hardlink", "copy", "move"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / "downloads" / "torrent"
                output = root / "archive"
                source.mkdir(parents=True)
                original = source / "site-ad.ABC-123.1080P.MKV"
                original.write_bytes(b"movie data")
                result = library._archive_file(
                    original, str(output), "ABC-123", mode=mode, rename=True,
                    watch_dir=str(root / "downloads"), folder_name="ABC-123",
                    by_month=False)
                archived = output / "ABC-123" / "ABC-123.mkv"
                self.assertTrue(result["archived"])
                self.assertEqual(archived.read_bytes(), b"movie data")
                self.assertEqual(original.exists(), mode != "move")
                self.assertEqual(result["moved_original"], mode == "move")

    def test_organize_off_preserves_original_filename_in_archive(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "downloads"
            source.mkdir()
            original = source / "Original Name [ABC-123].MP4"
            original.write_bytes(b"movie")
            result = library._archive_file(
                original, str(root / "archive"), "ABC-123", mode="copy",
                rename=False, folder_name="ABC-123", by_month=False)
            self.assertTrue(result["archived"])
            self.assertTrue((root / "archive" / "ABC-123" / original.name).exists())
            self.assertEqual(original.name, "Original Name [ABC-123].MP4")

    def test_failed_copy_cleans_partial_target_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            src = root / "source.mp4"
            dst = root / "archive.mp4"
            src.write_bytes(b"complete movie")

            def partial_then_fail(_src, tmp):
                Path(tmp).write_bytes(b"partial")
                raise OSError("disk full")

            with mock.patch.object(library.shutil, "copy2", side_effect=partial_then_fail):
                self.assertFalse(library._transfer(src, dst, "copy"))
            self.assertEqual(src.read_bytes(), b"complete movie")
            self.assertFalse(dst.exists())
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_move_cross_volume_fallback_lands_before_removing_source(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            src = root / "source.mp4"
            dst = root / "archive.mp4"
            src.write_bytes(b"cross-volume movie")
            original_link = library.os.link
            calls = 0

            def fail_first_link(source, target):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("cross-device link")
                return original_link(source, target)

            with mock.patch.object(library.os, "link", side_effect=fail_first_link):
                self.assertTrue(library._transfer(src, dst, "move"))
            self.assertFalse(src.exists())
            self.assertEqual(dst.read_bytes(), b"cross-volume movie")

    def test_move_source_unlink_failure_rolls_back_target_and_keeps_source(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            src = root / "source.mp4"
            dst = root / "archive.mp4"
            src.write_bytes(b"movie")
            original_unlink = library.Path.unlink

            locked = PermissionError(13, "file is being used", str(src))
            locked.winerror = 32

            def deny_source_unlink(path, *args, **kwargs):
                if path == src:
                    raise locked
                return original_unlink(path, *args, **kwargs)

            logs = []
            with (mock.patch.object(library.Path, "unlink", autospec=True,
                                    side_effect=deny_source_unlink),
                  mock.patch.object(library, "_log", side_effect=logs.append)):
                self.assertFalse(library._transfer(src, dst, "move"))
            self.assertEqual(src.read_bytes(), b"movie")
            self.assertFalse(dst.exists())
            message = "\n".join(logs)
            self.assertIn("文件被其他程序占用或锁定", message)
            self.assertIn("下载器校验/做种占用", message)
            self.assertIn("winerror=32", message)

    def test_remove_error_diagnosis_distinguishes_readonly_mount(self):
        error = OSError(library.errno.EROFS, "read-only file system")
        reason, action = library._diagnose_source_remove_error(Path("movie.mp4"), error)
        self.assertIn("只读文件系统", reason)
        self.assertIn(":ro", action)

    def test_linux_remove_error_diagnosis_distinguishes_busy_and_stale_nfs(self):
        with mock.patch.object(library.sys, "platform", "linux"):
            reason, action = library._diagnose_source_remove_error(
                Path("movie.mp4"), OSError(library.errno.EBUSY, "busy"))
            self.assertIn("挂载点正忙", reason)
            self.assertIn("NFS/CIFS", action)

            stale = getattr(library.errno, "ESTALE", 116)
            reason, action = library._diagnose_source_remove_error(
                Path("movie.mp4"), OSError(stale, "stale file handle"))
            self.assertIn("句柄已失效", reason)
            self.assertIn("重新挂载", action)

    def test_move_collision_preserves_source_and_existing_archive(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "downloads" / "ABC-123"
            target = root / "archive" / "ABC-123"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            incoming = source / "ABC-123-CD1.mp4"
            existing = target / "ABC-123-cd1.mp4"
            incoming.write_bytes(b"incoming")
            existing.write_bytes(b"existing")
            result = library._archive_file(
                incoming, str(root / "archive"), "ABC-123", mode="move",
                rename=True, watch_dir=str(root / "downloads"),
                folder_name="ABC-123", by_month=False)
            self.assertFalse(result["archived"])
            self.assertEqual(incoming.read_bytes(), b"incoming")
            self.assertEqual(existing.read_bytes(), b"existing")

    def test_move_multipart_cleanup_waits_until_every_video_is_archived(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            watch = root / "downloads"
            source = watch / "ABC-123"
            output = root / "archive"
            source.mkdir(parents=True)
            cd1 = source / "ABC-123-CD1.mp4"
            cd2 = source / "ABC-123-CD2.mp4"
            cd1.write_bytes(b"part one")
            cd2.write_bytes(b"part two")

            first = library._archive_file(
                cd1, str(output), "ABC-123", mode="move", rename=True,
                watch_dir=str(watch), folder_name="ABC-123", by_month=False)
            self.assertTrue(first["moved_original"])
            library._cleanup_source(source, watch, 1)
            self.assertTrue(source.exists())
            self.assertEqual(cd2.read_bytes(), b"part two")

            second = library._archive_file(
                cd2, str(output), "ABC-123", mode="move", rename=True,
                watch_dir=str(watch), folder_name="ABC-123", by_month=False)
            self.assertTrue(second["moved_original"])
            library._cleanup_source(source, watch, 1)
            self.assertFalse(source.exists())
            self.assertEqual((output / "ABC-123" / "ABC-123-cd1.mp4").read_bytes(), b"part one")
            self.assertEqual((output / "ABC-123" / "ABC-123-cd2.mp4").read_bytes(), b"part two")

    def test_move_with_organize_off_keeps_original_archive_name(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "downloads"
            source.mkdir()
            original = source / "Original.Name.ABC-123.MP4"
            original.write_bytes(b"movie")
            result = library._archive_file(
                original, str(root / "archive"), "ABC-123", mode="move",
                rename=False, folder_name="ABC-123", by_month=False)
            self.assertTrue(result["moved_original"])
            self.assertFalse(original.exists())
            self.assertEqual(
                (root / "archive" / "ABC-123" / "Original.Name.ABC-123.MP4").read_bytes(),
                b"movie")

    def test_same_path_move_is_not_reported_as_source_removed(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            target = output / "ABC-123"
            target.mkdir()
            movie = target / "ABC-123.mp4"
            movie.write_bytes(b"movie")
            result = library._archive_file(
                movie, str(output), "ABC-123", mode="move", rename=True,
                folder_name="ABC-123", by_month=False)
            self.assertTrue(result["archived"])
            self.assertFalse(result["moved_original"])
            self.assertEqual(movie.read_bytes(), b"movie")

    def test_similar_code_folder_is_not_reused(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            (output / "ABC-1234 Existing").mkdir(parents=True)
            video = source / "ABC-123.mp4"
            video.write_bytes(b"movie")
            result = library._archive_file(
                video, str(output), "ABC-123", mode="copy", rename=True,
                folder_name="ABC-123 New", by_month=False)
            self.assertTrue(result["archived"])
            self.assertEqual(Path(result["target_dir"]).name, "ABC-123 New")
            self.assertTrue((output / "ABC-123 New" / "ABC-123.mp4").exists())

    def test_cleanup_scan_error_preserves_entire_source_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            watch = Path(raw) / "watch"
            folder = watch / "torrent"
            folder.mkdir(parents=True)
            marker = folder / "important.txt"
            marker.write_text("keep", encoding="utf-8")
            with mock.patch.object(library.Path, "rglob", side_effect=PermissionError("denied")):
                library._cleanup_source(folder, watch, 1)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_cleanup_preserves_folder_when_any_small_video_remains(self):
        with tempfile.TemporaryDirectory() as raw:
            watch = Path(raw) / "watch"
            folder = watch / "torrent"
            folder.mkdir(parents=True)
            short_video = folder / "unknown-short.mp4"
            short_video.write_bytes(b"x")
            library._cleanup_source(
                folder, watch, min_bytes=100 * 1024 * 1024,
                keep_bytes=300 * 1024 * 1024)
            self.assertEqual(short_video.read_bytes(), b"x")

    def test_destructive_extra_cleanup_defaults_on_but_is_independent(self):
        self.assertTrue(DEFAULT_CONFIG["scrape_delete_extras"])

    def test_archive_subtree_is_excluded_from_watch_scan(self):
        with tempfile.TemporaryDirectory() as raw:
            watch = Path(raw)
            incoming = watch / "incoming.mp4"
            archived = watch / "jav" / "202608" / "ABC-123" / "ABC-123.mp4"
            incoming.write_bytes(b"incoming")
            archived.parent.mkdir(parents=True)
            archived.write_bytes(b"archived")
            found = list(library._iter_video_files(watch, {watch / "jav"}))
            self.assertEqual(found, [incoming])

    def test_custom_trackers_are_deduplicated(self):
        state = asyncio.run(qbittorrent.ensure_trackers_fresh({
            "public_trackers": "udp://one.test:80/announce\nudp://one.test:80/announce",
            "public_trackers_auto_update": True,
        }))
        self.assertEqual(state["source"], "user")
        self.assertEqual(len(state["list"]), 1)

    def test_actor_image_falls_back_to_movie_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            original = library._download_image

            async def fake_download(url, proxy, referer):
                return b"actor-image"

            library._download_image = fake_download
            try:
                count = asyncio.run(library._save_actor_images(
                    {"actors": [{"name": "ActorA", "avatar": "https://example.test/a.jpg"}]},
                    {"scrape_actor_images_enabled": True, "scrape_actor_images_dir": ""},
                    None, root))
            finally:
                library._download_image = original
            self.assertEqual(count, 1)
            self.assertEqual((root / "actors" / "ActorA.jpg").read_bytes(), b"actor-image")

    def test_missing_actor_avatar_is_merged_by_name(self):
        movie = {"actors": [{"name": "Actor A", "avatar": ""},
                            {"name": "Actor B", "avatar": "https://old.test/b.jpg"}]}
        filled = library._merge_actor_avatars(movie, [
            {"name": "Actor-A", "avatar": "https://new.test/a.jpg"},
            {"name": "Actor B", "avatar": "https://new.test/b.jpg"},
        ])
        self.assertEqual(filled, 1)
        self.assertEqual(movie["actors"][0]["avatar"], "https://new.test/a.jpg")
        self.assertEqual(movie["actors"][1]["avatar"], "https://old.test/b.jpg")

    def test_actor_thumb_can_be_omitted_from_nfo(self):
        movie = {"code": "ABC-123", "actors": [
            {"name": "Actor A", "avatar": "https://example.test/a.jpg"}]}
        with_thumb = library._build_nfo(movie, "ABC-123", "", True)
        without_thumb = library._build_nfo(movie, "ABC-123", "", False)
        self.assertIn("<thumb>https://example.test/a.jpg</thumb>", with_thumb)
        self.assertNotIn("<thumb>", without_thumb)
        self.assertIn("<name>Actor A</name>", without_thumb)

    def test_independent_actor_scraper_adds_missing_nfo_actor(self):
        with tempfile.TemporaryDirectory() as raw:
            nfo = Path(raw) / "ABC-123.nfo"
            nfo.write_text("<movie><uniqueid type='num'>ABC-123</uniqueid></movie>", encoding="utf-8")
            tree, actors, code = actor_scraper._read_nfo(nfo)
            self.assertEqual(code, "ABC-123")
            self.assertEqual(actors, [])
            actor_scraper._write_nfo(nfo, tree, [
                {"name": "Actor A", "avatar": "https://example.test/a.jpg"}], True)
            _, actors, _ = actor_scraper._read_nfo(nfo)
            self.assertEqual(actors[0]["name"], "Actor A")
            self.assertEqual(actors[0]["avatar"], "https://example.test/a.jpg")

    def test_actor_source_priority_filters_unknown_sources(self):
        self.assertEqual(actor_scraper._sources({
            "actor_scrape_sources": ["javdb", "unknown", "javbus"]}),
            ["javbus"])
        self.assertEqual(actor_scraper._sources({
            "actor_scrape_sources": ["avmoo", "avsox", "javbus"]}),
            ["avsox", "javbus"])

    def test_actor_code_lookup_stops_after_complete_first_source(self):
        original = actor_scraper._details
        called = []

        async def fake_details(query, mode, source, proxy):
            called.append(source)
            return [{"code": "ABC-123", "actors": [
                {"name": "Actor A", "avatar": "https://example.test/a.jpg"}]}]

        actor_scraper._details = fake_details
        try:
            actors = asyncio.run(actor_scraper._actors_by_code(
                "ABC-123", ["javbus", "avsox"], None, ["Actor A"]))
        finally:
            actor_scraper._details = original
        self.assertEqual(called, ["javbus"])
        self.assertEqual(actors[0]["name"], "Actor A")

    def test_actor_name_lookup_skips_sources_without_portraits(self):
        original = actor_scraper._details
        called = []

        async def fake_details(query, mode, source, proxy):
            called.append(source)
            return []

        actor_scraper._details = fake_details
        try:
            asyncio.run(actor_scraper._avatar_by_name(
                "Actor A", ["avsox", "avmoo", "javdb", "javbus"], None))
        finally:
            actor_scraper._details = original
        self.assertEqual(called, ["javbus"])

    def test_archive_includes_case_insensitive_extras_and_actor_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            video = source / "san-475.mp4"
            video.write_bytes(b"video")
            (source / "san-475.NFO").write_text("<movie/>", encoding="utf-8")
            (source / "san-475-POSTER.JPG").write_bytes(b"poster")
            (source / "san-475-FANART.JPG").write_bytes(b"fanart")
            actor_dir = source / ".actors"
            actor_dir.mkdir()
            (actor_dir / "ActorA.jpg").write_bytes(b"actor")
            result = library._archive_file(
                video, str(output), "SAN-475", mode="copy", rename=True,
                folder_name="SAN-475 ActorA", by_month=False)
            target = output / "SAN-475 ActorA"
            self.assertTrue(result["archived"])
            self.assertTrue((target / "SAN-475.nfo").exists())
            self.assertTrue((target / "SAN-475-poster.jpg").exists())
            self.assertTrue((target / "SAN-475-fanart.jpg").exists())
            self.assertTrue((target / "actors" / "ActorA.jpg").exists())

    def test_non_fc2_artwork_search_skips_fc2_source(self):
        import scrapers
        original_search = scrapers.search_source_status
        original_enrich = scrapers.enrich
        searched_sources = []

        async def fake_search(_query, _mode, source, **_kwargs):
            searched_sources.append(source)
            return [], "ok"

        async def fake_enrich(*_args, **_kwargs):
            return [None]

        scrapers.search_source_status = fake_search
        scrapers.enrich = fake_enrich
        try:
            asyncio.run(library._backfill_artwork({
                "code": "DLDSS-509", "cover": "https://javbus/poster.jpg",
                "samples": [], "source": "JavBus", "url": "https://javbus/item",
            }, "DLDSS-509", {
                "sources": ["javbus", "javdb", "fc2"],
                "scrape_artwork_fallback_limit": 2,
            }, None))
        finally:
            scrapers.search_source_status = original_search
            scrapers.enrich = original_enrich
        self.assertIn("javdb", searched_sources)
        self.assertNotIn("fc2", searched_sources)

    def test_pending_fanart_is_written_to_exact_archive_folder(self):
        original_file = library._ARTWORK_PENDING_FILE
        original_pending = library._artwork_pending
        original_loaded = library._artwork_pending_loaded
        original_terminal_file = library._ARTWORK_TERMINAL_FILE
        original_terminal = library._artwork_terminal
        original_terminal_loaded = library._artwork_terminal_loaded
        original_backfill = library._backfill_artwork
        original_fetch = library._fetch_cover
        original_notify = actor_scraper.notify_emby_folder
        notified = []
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "202608" / "DLDSS-509"
            target.mkdir(parents=True)
            (target / "DLDSS-509-poster.jpg").write_bytes(b"poster")
            library._ARTWORK_PENDING_FILE = root / "pending.json"
            library._artwork_pending = {}
            library._artwork_pending_loaded = True
            library._ARTWORK_TERMINAL_FILE = root / "terminal.json"
            library._artwork_terminal = {}
            library._artwork_terminal_loaded = True

            async def fake_backfill(movie, *_args, **_kwargs):
                movie["samples"] = ["https://source/fanart.jpg"]
                return movie

            async def fake_fetch(*_args, **_kwargs):
                return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
                        + (1280).to_bytes(4, "big") + (720).to_bytes(4, "big"))

            async def fake_notify(folder, _config):
                notified.append(folder)
                return {"triggered": True, "message": "ok"}

            library._backfill_artwork = fake_backfill
            library._fetch_cover = fake_fetch
            actor_scraper.notify_emby_folder = fake_notify
            try:
                config = {"scrape_output_dir": str(root),
                          "scrape_artwork_fallback_limit": 2,
                          "emby_actor_sync_enabled": True}
                self.assertTrue(library._queue_artwork_backfill(
                    target, "DLDSS-509", {
                        "cover": "https://source/poster.jpg",
                        "_artwork_pending_sources": ["_download"],
                    }, config))
                library._artwork_pending[str(target)]["next_attempt"] = 0
                result = asyncio.run(library._run_pending_artwork(config))
            finally:
                library._ARTWORK_PENDING_FILE = original_file
                library._artwork_pending = original_pending
                library._artwork_pending_loaded = original_loaded
                library._ARTWORK_TERMINAL_FILE = original_terminal_file
                library._artwork_terminal = original_terminal
                library._artwork_terminal_loaded = original_terminal_loaded
                library._backfill_artwork = original_backfill
                library._fetch_cover = original_fetch
                actor_scraper.notify_emby_folder = original_notify
            self.assertEqual(result, 1)
            self.assertTrue(library._is_fanart_image(
                (target / "DLDSS-509-fanart.jpg").read_bytes()))
            self.assertEqual(notified, [target])

    def test_confirmed_no_fanart_does_not_enter_persistent_queue(self):
        original_file = library._ARTWORK_PENDING_FILE
        original_pending = library._artwork_pending
        original_loaded = library._artwork_pending_loaded
        original_terminal_file = library._ARTWORK_TERMINAL_FILE
        original_terminal = library._artwork_terminal
        original_terminal_loaded = library._artwork_terminal_loaded
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "DLDSS-509"
            target.mkdir()
            (target / "DLDSS-509-poster.jpg").write_bytes(b"poster")
            library._ARTWORK_PENDING_FILE = root / "pending.json"
            library._artwork_pending = {}
            library._artwork_pending_loaded = True
            library._ARTWORK_TERMINAL_FILE = root / "terminal.json"
            library._artwork_terminal = {}
            library._artwork_terminal_loaded = True
            try:
                queued = library._queue_artwork_backfill(
                    target, "DLDSS-509", {
                        "cover": "https://source/poster.jpg", "samples": [],
                        "_artwork_pending_sources": [],
                    }, {"scrape_artwork_fallback_limit": 2})
                self.assertFalse(queued)
                self.assertEqual(library._artwork_pending, {})
                self.assertIn(str(target), library._artwork_terminal)
                self.assertFalse(library._queue_artwork_backfill(
                    target, "DLDSS-509", None,
                    {"scrape_artwork_fallback_limit": 2}))
                self.assertTrue(library._queue_artwork_backfill(
                    target, "DLDSS-509", None,
                    {"scrape_artwork_fallback_limit": 2,
                     "sources": ["javbus", "javdb", "jav321"]}))
            finally:
                library._ARTWORK_PENDING_FILE = original_file
                library._artwork_pending = original_pending
                library._artwork_pending_loaded = original_loaded
                library._ARTWORK_TERMINAL_FILE = original_terminal_file
                library._artwork_terminal = original_terminal
                library._artwork_terminal_loaded = original_terminal_loaded

    def test_transient_artwork_task_stops_after_three_retries(self):
        original_file = library._ARTWORK_PENDING_FILE
        original_pending = library._artwork_pending
        original_loaded = library._artwork_pending_loaded
        original_terminal_file = library._ARTWORK_TERMINAL_FILE
        original_terminal = library._artwork_terminal
        original_terminal_loaded = library._artwork_terminal_loaded
        original_backfill = library._backfill_artwork
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "DLDSS-509"
            target.mkdir()
            (target / "DLDSS-509-poster.jpg").write_bytes(b"poster")
            key = str(target)
            library._ARTWORK_PENDING_FILE = root / "pending.json"
            library._artwork_pending = {key: {
                "target_dir": key, "code": "DLDSS-509", "attempts": 2,
                "next_attempt": 0,
                "movie": {"cover": "https://source/poster.jpg",
                          "_artwork_pending_sources": ["javdb"]},
            }}
            library._artwork_pending_loaded = True
            library._ARTWORK_TERMINAL_FILE = root / "terminal.json"
            library._artwork_terminal = {}
            library._artwork_terminal_loaded = True

            async def still_timeout(movie, *_args, **_kwargs):
                movie["_artwork_pending_sources"] = ["javdb"]
                return movie

            library._backfill_artwork = still_timeout
            try:
                asyncio.run(library._run_pending_artwork({
                    "scrape_output_dir": str(root),
                    "scrape_artwork_fallback_limit": 2,
                }))
                self.assertNotIn(key, library._artwork_pending)
            finally:
                library._ARTWORK_PENDING_FILE = original_file
                library._artwork_pending = original_pending
                library._artwork_pending_loaded = original_loaded
                library._ARTWORK_TERMINAL_FILE = original_terminal_file
                library._artwork_terminal = original_terminal
                library._artwork_terminal_loaded = original_terminal_loaded
                library._backfill_artwork = original_backfill

    def test_archive_index_finds_exact_hardlink_target_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "downloads" / "DLDSS-509.H265.mp4"
            target = root / "archive" / "202608" / "DLDSS-509" / "DLDSS-509.mp4"
            source.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            source.write_bytes(b"video")
            os.link(source, target)
            idx = library._build_archive_index(str(root / "archive"))
            found = library._archived_target_for_source(
                source, source.stat().st_size, "DLDSS-509", idx)
        self.assertEqual(found, target.parent)


if __name__ == "__main__":
    unittest.main()

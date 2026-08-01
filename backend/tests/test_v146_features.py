import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import library
import actor_scraper
import emby
import qbittorrent
import transmission
import main
from scrapers import _javu_base, _javbus_base
from config_manager import DEFAULT_CONFIG


class VideoClassificationTests(unittest.TestCase):
    def test_emby_batch_refreshes_waits_uploads_and_verifies_people(self):
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
            refresh_calls = 0
            uploads = []
            searches = {}

            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, url, **kwargs):
                if url.endswith("/Library/Refresh"):
                    Client.refresh_calls += 1
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
                ], poll_delays=(0,)))
        finally:
            emby.httpx.AsyncClient = original_client
            emby.asyncio.sleep = original_sleep
        self.assertTrue(result["refresh_triggered"])
        self.assertEqual(Client.refresh_calls, 1)
        self.assertEqual(len(Client.uploads), 2)
        self.assertTrue(all(item["updated"] for item in result["results"]))

    def test_actor_nfo_is_written_before_emby_library_refresh(self):
        original_sync = actor_scraper.emby.sync_person_images

        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            nfo = folder / "ABC-123.nfo"
            nfo.write_text("<movie><uniqueid>ABC-123</uniqueid></movie>", encoding="utf-8")
            tree = actor_scraper.ET.parse(nfo)
            actor_dir = folder / "actors"
            actor_dir.mkdir()
            (actor_dir / "Actor A.jpg").write_bytes(b"x" * 2048)

            async def fake_sync(_url, _key, portraits):
                current = nfo.read_text(encoding="utf-8")
                self.assertIn("Actor A", current)
                self.assertEqual([item["name"] for item in portraits], ["Actor A"])
                return {"refresh_triggered": True, "results": [
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


if __name__ == "__main__":
    unittest.main()

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import library
import actor_scraper
import qbittorrent
import transmission
import main
from config_manager import DEFAULT_CONFIG


class VideoClassificationTests(unittest.TestCase):
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
            ["javdb", "javbus"])

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

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import library
import qbittorrent


class VideoClassificationTests(unittest.TestCase):
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
            self.assertEqual((root / ".actors" / "ActorA.jpg").read_bytes(), b"actor-image")

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
            self.assertTrue((target / ".actors" / "ActorA.jpg").exists())


if __name__ == "__main__":
    unittest.main()

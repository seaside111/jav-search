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


if __name__ == "__main__":
    unittest.main()

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import actor_scraper
import config_manager
import library
import main


class EmbyDirectoryTests(unittest.TestCase):
    def config(self, root):
        return {"scrape_multi_enabled": True, "emby_media_root": "/old-single-path",
                "scrape_output_dir": str(root / "old-single-output"),
                "scrape_directory_pairs": [
                    {"watch_dir": str(root / "downloads-a"), "output_dir": str(root / "a"),
                     "emby_media_root": "/media/a"},
                    {"watch_dir": str(root / "downloads-b"), "output_dir": str(root / "b"),
                     "emby_media_root": "/media/b"}]}

    def test_distinct_roots_map_folder_video_and_nfo(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self.config(root)
            rows = library._directory_configs(config)
            self.assertEqual([row["emby_media_root"] for row in rows], ["/media/a", "/media/b"])
            for group in ("a", "b"):
                folder = root / group / "202611" / "ABC-123"
                folder.mkdir(parents=True)
                (folder / "ABC-123.mp4").write_bytes(b"movie")
                (folder / "ABC-123.nfo").write_text("<movie/>", encoding="utf-8")
                paths, error = actor_scraper._emby_media_paths(folder, config)
                self.assertEqual(error, "")
                prefix = f"/media/{group}/202611/ABC-123"
                self.assertEqual(paths, [prefix, prefix + "/ABC-123.mp4", prefix + "/ABC-123.nfo"])

    def test_blank_group_does_not_inherit_single_mode_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self.config(root)
            config["scrape_directory_pairs"][1].pop("emby_media_root")
            folder = root / "b" / "ABC-123"
            folder.mkdir(parents=True)
            self.assertEqual(library._directory_configs(config)[1]["emby_media_root"], "")
            self.assertEqual(actor_scraper._emby_media_paths(folder, config), ([str(folder.resolve())], ""))
            unknown, error = actor_scraper._emby_media_paths(root / "outside", config)
            self.assertEqual(unknown, [])
            self.assertIn("无法映射", error)

    def test_nested_archive_root_uses_most_specific_mapping(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self.config(root)
            config["scrape_directory_pairs"][1]["output_dir"] = str(root / "a" / "special")
            folder = root / "a" / "special" / "ABC-123"
            folder.mkdir(parents=True)
            self.assertEqual(actor_scraper._emby_media_paths(folder, config), (["/media/b/ABC-123"], ""))

    def test_shared_archive_requires_consistent_emby_mapping(self):
        with tempfile.TemporaryDirectory() as raw:
            config = self.config(Path(raw))
            config["scrape_directory_pairs"][1]["output_dir"] = config["scrape_directory_pairs"][0]["output_dir"]
            with self.assertRaisesRegex(ValueError, "Emby"):
                library._directory_configs(config)
            config["scrape_directory_pairs"][1]["emby_media_root"] = "/media/a/"
            self.assertEqual(len(library._directory_configs(config)), 2)
            config["scrape_directory_pairs"][1]["emby_media_root"] = ""
            with self.assertRaises(ValueError):
                library._directory_configs(config)

    def test_config_save_and_reload_preserves_each_group(self):
        with tempfile.TemporaryDirectory() as raw:
            config = self.config(Path(raw))
            with mock.patch.object(config_manager, "CONFIG_PATH", Path(raw) / "settings.json"), \
                    mock.patch.object(library, "ensure_monitor"):
                response = asyncio.run(main.api_set_config(main.ConfigUpdateRequest(**config)))
                self.assertTrue(response["success"])
                saved = config_manager.load()
            self.assertEqual(saved["scrape_directory_pairs"], config["scrape_directory_pairs"])
            self.assertEqual(saved["emby_media_root"], "/old-single-path")

    def test_notify_and_persisted_retry_use_group_paths_and_local_portrait_bytes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = {**self.config(root), "actor_scrape_cache_dir": str(root / "cache"),
                      "emby_actor_sync_enabled": True, "emby_url": "http://emby", "emby_api_key": "test"}
            folder = root / "b" / "ABC-123"
            actors = folder / "actors"
            actors.mkdir(parents=True)
            portrait_bytes = b"downloaded local portrait" * 100
            (actors / "Actor.jpg").write_bytes(portrait_bytes)
            (folder / "ABC-123.mp4").write_bytes(b"movie")
            with mock.patch.object(actor_scraper.emby, "notify_media_paths", mock.AsyncMock()) as notify:
                asyncio.run(actor_scraper.notify_emby_folder(folder, config))
                self.assertEqual(notify.await_args.args[2], ["/media/b/ABC-123"])

            pending_result = {"retryable": True, "pending_names": ["Actor"],
                              "results": [{"name": "Actor", "updated": False}]}
            with mock.patch.object(actor_scraper.emby, "sync_person_images", mock.AsyncMock(
                    return_value=pending_result)) as sync:
                asyncio.run(actor_scraper.sync_emby_folder(folder, library._directory_configs(config)[1],
                    "ABC-123", [{"name": "Actor", "avatar": "https://source/remote.jpg"}]))
                self.assertEqual(sync.await_args.args[2][0]["image"], portrait_bytes)
                self.assertEqual(sync.await_args.kwargs["media_paths"][0], "/media/b/ABC-123")

            pending = actor_scraper._load_emby_pending(config)
            pending[str(folder.resolve())]["next_attempt"] = 0
            actor_scraper._save_emby_pending(config, pending)
            # A restart loads global settings, and the user may have corrected
            # the path since the initial attempt. Use the current group mapping.
            config["scrape_directory_pairs"][1]["emby_media_root"] = "/corrected/b"
            success = {"retryable": False, "results": [{"name": "Actor", "updated": True}]}
            with mock.patch.object(actor_scraper, "load_config", return_value=config), \
                    mock.patch.object(actor_scraper.emby, "sync_person_images", mock.AsyncMock(
                        return_value=success)) as sync:
                result = asyncio.run(actor_scraper.run_emby_pending_once())
            self.assertEqual(result["emby_updated"], 1)
            self.assertEqual(sync.await_args.kwargs["media_paths"][0], "/corrected/b/ABC-123")
            self.assertFalse(sync.await_args.kwargs["notify_media"])
            self.assertEqual(sync.await_args.args[2][0]["image"], portrait_bytes)
            self.assertEqual(actor_scraper._load_emby_pending(config), {})


if __name__ == "__main__":
    unittest.main()

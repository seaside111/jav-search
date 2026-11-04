import asyncio
import errno
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config_manager
import library
import main


class ScrapeDirectoryTests(unittest.TestCase):
    def test_master_switch_defaults_off_and_persists(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
                config_manager, "CONFIG_PATH", Path(raw) / "settings.json"):
            config = config_manager.load()
            self.assertFalse(library._monitor_should_run(config))
            config["scrape_enabled"] = True
            self.assertTrue(config_manager.save(config))
            self.assertTrue(library._monitor_should_run(config_manager.load()))
            config["scrape_enabled"] = False
            config_manager.save(config)
            self.assertFalse(library._monitor_should_run(config_manager.load()))

    def test_multi_mapping_shared_output_and_validation(self):
        config = {"scrape_multi_enabled": True, "scrape_directory_pairs": [
            {"watch_dir": "/downloads/a", "output_dir": "/archive"},
            {"watch_dir": "/downloads/b", "output_dir": "/archive"}]}
        rows = library._directory_configs(config)
        self.assertEqual([row["scrape_watch_dir"] for row in rows], ["/downloads/a", "/downloads/b"])
        for invalid in [[], [{"watch_dir": "/a", "output_dir": ""}],
                        [{"watch_dir": "/a", "output_dir": "/a"}],
                        [{"watch_dir": "/a", "output_dir": "/out"},
                         {"watch_dir": "/a/nested", "output_dir": "/out"}],
                        [{"watch_dir": "/a", "output_dir": "/out"},
                         {"watch_dir": "/out/nested", "output_dir": "/elsewhere"}]]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                library._directory_configs({**config, "scrape_directory_pairs": invalid})

    def test_multi_scan_passes_own_paths_and_excludes_all_archives(self):
        config = {"scrape_multi_enabled": True, "scrape_directory_pairs": [
            {"watch_dir": "/a", "output_dir": "/a/archive"},
            {"watch_dir": "/b", "output_dir": "/shared"}]}
        with mock.patch.object(library, "_scan_once", mock.AsyncMock(side_effect=[2, 3])) as scan:
            self.assertEqual(asyncio.run(library._scan_serialized(config)), 5)
        calls = scan.await_args_list
        self.assertEqual(calls[0].args[0]["scrape_output_dir"], "/a/archive")
        self.assertEqual(calls[1].args[0]["scrape_watch_dir"], "/b")
        self.assertEqual(calls[0].args[0]["_exclude_outputs"], ["/a/archive", "/shared"])

    def test_force_endpoint_selects_only_requested_mapping(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            a, b = root / "a", root / "b"
            a.mkdir()
            b.mkdir()
            config = {"scrape_multi_enabled": True, "scrape_directory_pairs": [
                {"watch_dir": str(a), "output_dir": str(root / "out")},
                {"watch_dir": str(b), "output_dir": str(root / "out")}]}
            with mock.patch.object(library, "load_config", return_value=config), mock.patch.object(
                    library, "_scan_serialized", mock.AsyncMock(return_value=2)) as scan:
                result = asyncio.run(library.api_run_once(mapping_index=1, force=True))
            selected = scan.await_args.args[0]
            self.assertEqual(selected["scrape_watch_dir"], str(b))
            self.assertFalse(selected["scrape_multi_enabled"])
            self.assertTrue(selected["_force_rescrape"])
            self.assertEqual(result["processed"], 2)

    def test_hardlink_probe_checks_real_paths_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as raw:
            a, b = Path(raw) / "a", Path(raw) / "b"
            a.mkdir()
            b.mkdir()
            self.assertTrue(library._test_hardlink(str(a), str(b))["supported"])
            self.assertEqual(list(a.iterdir()) + list(b.iterdir()), [])
            with mock.patch.object(library.os, "link", side_effect=OSError(errno.EXDEV, "cross-device")):
                result = library._test_hardlink(str(a), str(b))
            self.assertFalse(result["supported"])
            self.assertIn("复制", result["message"])
            self.assertEqual(list(a.iterdir()) + list(b.iterdir()), [])

    def test_force_bypasses_dedupe_but_not_downloading_gate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "ABC-123.mp4"
            video.write_bytes(b"movie")
            config = {"scrape_watch_dir": raw, "scrape_min_size_mb": 0,
                      "_force_rescrape": True, "archive_enabled": False}
            with mock.patch.object(library, "_load_processed"), mock.patch.object(library, "_load_tasks"), \
                    mock.patch.object(library, "_load_retries"), \
                    mock.patch.object(library, "_processed", {str(video)}), \
                    mock.patch.object(library, "_processed_sig", {library._file_sig(video, 5): 1}), \
                    mock.patch.object(library, "_retry_states", {}), \
                    mock.patch.object(library, "_repair_recorded_nfo_title", return_value=[]), \
                    mock.patch.object(library, "_build_archive_index", return_value={"present": True}), \
                    mock.patch.object(library, "_is_already_archived", return_value=True) as dedupe, \
                    mock.patch.object(library, "_task_update"), mock.patch.object(library, "_record_recent"), \
                    mock.patch.object(library, "_set_retry_state"), \
                    mock.patch.object(library, "_download_state_snapshot", mock.AsyncMock(return_value=(True, []))), \
                    mock.patch.object(library, "_file_download_state", return_value=("completed", {"completed": True})), \
                    mock.patch.object(library, "_process_completed_file", mock.AsyncMock(
                        return_value={"scrape_ok": True, "note": "disabled"})) as process:
                self.assertEqual(asyncio.run(library._scan_once(config, force_retry=True)), 1)
                process.assert_awaited_once()
                dedupe.assert_not_called()
                process.reset_mock()
                with mock.patch.object(library, "_file_download_state", return_value=("downloading", {"progress": .5})):
                    self.assertEqual(asyncio.run(library._scan_once(config, force_retry=True)), 0)
                process.assert_not_awaited()
                with mock.patch.object(library, "_file_download_state", return_value=("unmatched", None)), \
                        mock.patch.object(library, "_size_history", {}), \
                        mock.patch.object(library, "_folder_history", {}):
                    self.assertEqual(asyncio.run(library._scan_once(config, force_retry=True)), 0)
                process.assert_not_awaited()
                with mock.patch.object(library, "load_config", return_value={"scrape_enabled": False}):
                    self.assertEqual(asyncio.run(library._scan_once({**config, "_automatic": True})), 0)
                process.assert_not_awaited()

    def test_force_requests_metadata_overwrite(self):
        config = {"scrape_meta_enabled": True, "archive_enabled": False, "_force_rescrape": True}
        with mock.patch.object(library, "_scrape_one", mock.AsyncMock(return_value={"success": True})) as scrape:
            asyncio.run(library._process_completed_file(Path("ABC-123.mp4"), config))
        scrape.assert_awaited_once_with("ABC-123.mp4", overwrite=True, config=config)

    def test_force_updates_existing_sidecars_without_duplicate_video(self):
        for mode in ("hardlink", "copy"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                watch, output = root / "watch", root / "output"
                watch.mkdir()
                video = watch / "download.ABC-123.mp4"
                video.write_bytes(b"video contents")
                for name in ("ABC-123.nfo", "poster.jpg", "fanart.jpg"):
                    (watch / name).write_bytes(b"old metadata")
                kwargs = dict(mode=mode, by_month=False, watch_dir=str(watch), min_bytes=0)
                first = library._archive_file(video, str(output), "ABC-123", **kwargs)
                self.assertTrue(first["archived"])
                for name in ("ABC-123.nfo", "poster.jpg", "fanart.jpg"):
                    (watch / name).write_bytes(b"fresh metadata")
                result = library._archive_file(video, str(output), "ABC-123", refresh_sidecars=True, **kwargs)
                self.assertTrue(result["archived"])
                target = Path(result["target_dir"])
                self.assertEqual(len(list(target.glob("*.mp4"))), 1)
                self.assertEqual((target / "ABC-123.nfo").read_bytes(), b"fresh metadata")
                self.assertEqual((target / "poster.jpg").read_bytes(), b"fresh metadata")
                self.assertEqual(video.read_bytes(), b"video contents")

    def test_manual_save_can_defer_monitor_refresh(self):
        with mock.patch.object(main, "load_config", return_value={}), \
                mock.patch.object(main, "save_config", return_value=True), \
                mock.patch.object(library, "ensure_monitor") as ensure:
            asyncio.run(main.api_set_config(main.ConfigUpdateRequest(scrape_enabled=True), refresh_monitor=False))
        ensure.assert_not_called()

    def test_stop_wakes_monitor_without_waiting_for_poll_interval(self):
        async def run():
            config = {"scrape_enabled": True, "scrape_interval": 3600}

            async def scan(_config):
                config["scrape_enabled"] = False
                library.ensure_monitor()
                return 0

            with mock.patch.object(library, "load_config", side_effect=lambda: dict(config)), \
                    mock.patch.object(library, "_monitor_wakeup", asyncio.Event()), \
                    mock.patch.object(library, "_scan_serialized", side_effect=scan), \
                    mock.patch.dict(library._monitor_state):
                await asyncio.wait_for(library._monitor_loop(), timeout=0.5)
                self.assertFalse(library._monitor_state["running"])

        asyncio.run(run())

    def test_http_config_validation_and_hardlink_endpoint(self):
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
                config_manager, "CONFIG_PATH", Path(raw) / "settings.json"), \
                mock.patch.object(main.auth, "AUTH_ENABLED", False), \
                mock.patch.object(library, "ensure_monitor"):
            client = TestClient(main.app)
            response = client.post("/api/config", json={"scrape_multi_enabled": True,
                                                       "scrape_directory_pairs": []})
            self.assertEqual(response.status_code, 400)
            self.assertFalse(config_manager.CONFIG_PATH.exists())
            response = client.post("/api/config", json={"scrape_enabled": False})
            self.assertTrue(response.json()["success"])
            response = client.post("/api/library/scrape/test-hardlink", json={
                "watch_dir": raw, "output_dir": raw})
            self.assertTrue(response.json()["supported"])
            response = client.post("/api/library/scrape/run-once?mapping_index=99&force=true")
            self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()

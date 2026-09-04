import asyncio
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import platform_paths
import library
import windows_integration
import windows_updater


class WindowsPathTests(unittest.TestCase):
    def test_config_dir_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CONFIG_DIR": tmp}):
            self.assertEqual(platform_paths.app_data_dir(), Path(tmp))

    def test_windows_default_uses_local_app_data(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False), \
                patch.object(sys, "platform", "win32"):
            os.environ.pop("CONFIG_DIR", None)
            self.assertEqual(platform_paths.app_data_dir(), Path(tmp) / "JAV Search")

    def test_local_file_config_converts_hardlink_and_accepts_absolute_paths(self):
        update, invalid = windows_integration.normalize_local_file_config({
            "archive_mode": "hardlink",
            "scrape_watch_dir": r"D:\Downloads\JAV",
            "scrape_output_dir": r"E:\Media\JAV",
        })
        self.assertEqual(update["archive_mode"], "copy")
        self.assertEqual(invalid, [])

    def test_local_file_config_rejects_relative_paths(self):
        _update, invalid = windows_integration.normalize_local_file_config({
            "scrape_watch_dir": r"downloads\JAV",
            "actor_scrape_cache_dir": r"cache\actors",
        })
        self.assertEqual(invalid, ["scrape_watch_dir", "actor_scrape_cache_dir"])

    def test_windows_move_archives_by_copy_before_source_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "downloads" / "ABC-123.mp4"
            video.parent.mkdir()
            video.write_bytes(b"source movie")
            scraped = {
                "success": True,
                "code": "ABC-123",
                "filepath": str(video),
                "sidecar_dir": str(video.parent / "ABC-123"),
                "title_original": "",
                "folder_title": "",
                "actors": [],
            }
            with patch.object(library, "_scrape_one",
                              AsyncMock(return_value=scraped)), \
                    patch.object(library, "_archive_file", return_value={
                        "archived": False, "moved_original": False,
                        "error": "test stop", "target_dir": "",
                    }) as archive:
                asyncio.run(library._process_completed_file(video, {
                    "scrape_output_dir": str(root / "archive"),
                    "scrape_watch_dir": str(video.parent),
                    "scrape_meta_enabled": True,
                    "scrape_organize_enabled": True,
                    "archive_enabled": True,
                    "archive_mode": "move",
                }))
            self.assertEqual(archive.call_args.kwargs["mode"], "copy")
            self.assertTrue(archive.call_args.kwargs["require_sidecars"])
            self.assertTrue(video.exists())


class WindowsDownloaderTests(unittest.TestCase):
    def test_configured_qbittorrent_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(sys, "platform", "win32"):
            executable = Path(tmp) / "qbittorrent.exe"
            executable.write_bytes(b"test")
            self.assertEqual(windows_integration.find_client("qb", str(executable)),
                             str(executable.resolve()))

    def test_rejects_non_magnet_urls(self):
        with patch.object(sys, "platform", "win32"):
            result = windows_integration.open_download("https://example.test/a.torrent", "system", {})
        self.assertFalse(result["success"])
        self.assertIn("magnet", result["error"])

    def test_launches_explicit_qbittorrent_without_shell(self):
        with patch.object(sys, "platform", "win32"), \
                patch.object(windows_integration, "find_client", return_value=r"C:\Apps\qbittorrent.exe"), \
                patch.object(windows_integration.subprocess, "Popen") as popen:
            magnet = "magnet:?xt=urn:btih:abc"
            result = windows_integration.open_download(magnet, "qb", {})
        self.assertTrue(result["success"])
        popen.assert_called_once_with([r"C:\Apps\qbittorrent.exe", magnet], close_fds=True)

    def test_optional_flaresolverr_detection(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(sys, "platform", "win32"), \
                patch.dict(os.environ, {"ProgramFiles": tmp}, clear=False):
            executable = Path(tmp) / "FlareSolverr" / "flaresolverr.exe"
            executable.parent.mkdir()
            executable.write_bytes(b"test")
            self.assertEqual(windows_integration.find_optional_tool("flaresolverr"),
                             str(executable.resolve()))

    def test_ffprobe_status_reports_missing(self):
        with patch.object(windows_integration, "bundled_tool", return_value=None), \
                patch.object(windows_integration.shutil, "which", return_value=None):
            self.assertEqual(windows_integration.ffprobe_status()["available"], False)


class WindowsUpdaterTests(unittest.TestCase):
    def test_selects_matching_installer_and_checksum(self):
        release = {"assets": [
            {"name": "JAV-Search-v2.0-Windows-x64-Setup.exe",
             "browser_download_url": "https://github.com/a/b.exe"},
            {"name": "JAV-Search-v2.0-Windows-x64-Setup.exe.sha256",
             "browser_download_url": "https://github.com/a/b.sha256"},
        ]}
        selected = windows_updater.select_windows_assets(release)
        self.assertTrue(selected["available"])
        self.assertEqual(selected["installer"]["name"], release["assets"][0]["name"])

    def test_checksum_requires_matching_filename(self):
        digest = "a" * 64
        self.assertEqual(windows_updater.parse_sha256(
            f"{digest}  JAV-Search-v2.0-Windows-x64-Setup.exe",
            "JAV-Search-v2.0-Windows-x64-Setup.exe"), digest)
        with self.assertRaises(ValueError):
            windows_updater.parse_sha256(f"{digest}  other.exe", "expected.exe")

    def test_rejects_non_github_download(self):
        with self.assertRaises(ValueError):
            windows_updater._safe_download_url("https://example.test/update.exe")

    def test_hashes_installer_without_loading_whole_file(self):
        with tempfile.TemporaryDirectory() as folder:
            installer = Path(folder) / "setup.exe"
            installer.write_bytes(b"verified installer")
            self.assertEqual(
                windows_updater._sha256_file(installer),
                hashlib.sha256(b"verified installer").hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()

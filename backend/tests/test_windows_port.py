import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import platform_paths
import windows_integration


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


if __name__ == "__main__":
    unittest.main()


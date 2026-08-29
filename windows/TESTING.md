# Windows desktop verification

The supported client systems are Windows 10 x64 and Windows 11 x64. The Windows
desktop build is independent from the Docker release.

## Automated gates

- Python unit tests, including Windows paths, local downloader launching, tool
  detection, release asset selection, trusted download hosts and checksum parsing.
- PyInstaller `onedir` freeze with WebView2, tray integration and bundled ffprobe.
- Frozen backend startup/health/shutdown smoke test.
- Inno Setup compilation and installer SHA-256 verification.
- CI build on the GitHub Windows Server 2022 runner for Windows API compatibility.

## Manual client matrix

Run these checks on a clean Windows 10 x64 machine and a clean Windows 11 x64
machine before publishing the first stable Windows release:

1. Install without administrator privileges; verify Start menu and optional desktop shortcut.
2. Launch the WebView2 window; search each enabled source and open a detail page.
3. Refresh Home latest and confirm the current saved source configuration is used.
4. Open a magnet with system default, qBittorrent and Transmission when installed.
5. Scan a video in a Chinese and long path; confirm duration/geometry from bundled ffprobe.
6. Close to tray, restore, then Exit; confirm app-owned WebView2 processes terminate.
7. Enable optional startup and confirm it launches once after sign-in.
8. Upgrade over an older build; confirm `%LOCALAPPDATA%\JAV Search` settings remain.
9. Trigger a deliberately bad installer checksum; confirm the current version stays running.
10. Uninstall; confirm program files and shortcuts are removed while user data remains.

Record OS build, WebView2 runtime version, downloader version and result for each run.

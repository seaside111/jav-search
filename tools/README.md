# Bundled tools

Windows release builds place `ffprobe.exe` in this directory before running PyInstaller.
The executable is included in the installed `tools` directory and is preferred over `PATH`.

Run `powershell -ExecutionPolicy Bypass -File windows/prepare-tools.ps1` to download the
pinned Windows x64 LGPL build from BtbN/FFmpeg-Builds. The script verifies the archive
against the release `checksums.sha256` file and extracts only ffprobe plus its license.

FlareSolverr and Jackett are optional external services. They are detected locally and
linked from the Windows UI, but are not bundled into or automatically started by JAV Search.


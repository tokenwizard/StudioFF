# Linux support

This fork is [Icey321/StudioFF](https://github.com/Icey321/StudioFF) with Linux support added. Upstream doesn't take
pull requests, so the changes live here. Windows keeps its existing code paths behind `sys.platform` checks; the UI was
already Qt, so the changes are at the edges, mostly in `ffstudio/environment.py`.

## What changed

- **Subprocesses:** `CREATE_NO_WINDOW` is `0` outside Windows. Python raises `ValueError` for any nonzero `creationflags` on Linux, which broke detection, ffprobe/ffmpeg calls and the Setup page.
- **Environments:** Python detection, conda lookup and the installer handle `bin/python` and run `conda` directly (no `cmd.exe /c`).
- **FaceFusion's environment:** StudioFF sets `ENVIRONMENT_READY`, which skips FaceFusion's `setup_linux`, so the app now sets what that step would: conda `lib` and `tensorrt_libs` on `LD_LIBRARY_PATH`, plus `MALLOC_ARENA_MAX=4`. It also drops the library path OpenCV adds to the UI process on import, which ended in an empty entry (the current directory).
- **Stop:** kills the process tree through `/proc` instead of `taskkill`.
- **Open Folder:** selects the file through `org.freedesktop.FileManager1`, and falls back to opening the folder.
- **Benchmark:** CPU and memory come from `/proc`. **Setup** shows the distribution and kernel.
- **Acceleration:** MIGraphX (AMD/ROCm) is offered instead of DirectML on Linux.
- **Self-update:** stays Windows-only (the helper is PowerShell). Linux copies update with `git pull`.
- **Data folder:** `~/.config/StudioFF` (or `$XDG_CONFIG_HOME`). Fonts fall back to `sans-serif`.
- **New:** `install.sh` and `StudioFF.sh`, plus an app launcher entry. The README covers both platforms.

## Tested

On CachyOS (Wayland, niri), NVIDIA RTX 4090 (24GB) GPU with CUDA 13, conda env with Python 3.13, against FaceFusion v4 at the tested commit `a24893c`:

- Setup detects the FaceFusion folder and conda envs, and loads the options (95 options, 11 processors)
- Studio preview and full runs, face swap on CUDA
- Faces and Mask overlays (the face probe process)
- Benchmark (~237 FPS at 240p)
- Stop kills FaceFusion and all its ffmpeg children
- System monitor in the title bar, Open Folder, the portal file picker
- `install.sh` and the launcher entry, which passes `desktop-file-validate`
- The existing unit tests (32/32) pass

Not tested on Windows. The Windows code paths are unchanged.

## File picker

Qt picks its file dialog from `QT_QPA_PLATFORMTHEME`. With `gtk3` it shows GTK's own dialog and never asks the
xdg-desktop-portal, so a file manager registered as the portal FileChooser is skipped. To use the portal picker:

```bash
QT_QPA_PLATFORMTHEME=xdgdesktopportal ./StudioFF.sh
```

## Updating from upstream

`origin` points at Icey321/StudioFF and `fork` at this repository. To bring in a new upstream release:

```bash
git fetch origin
git merge origin/main
git push fork linux-support
```

Setup's StudioFF updates card checks upstream's releases, so it tells you when there is one to merge.

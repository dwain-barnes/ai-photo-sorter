# Installer Builds

The user-facing installers are built from this folder. They are not `.bat` launchers.

The GitHub Actions workflow at `.github/workflows/build-installers.yml` builds the Windows setup executable and the macOS package/DMG artifacts on the correct operating systems. Use it for release builds when a local Mac is not available.

## Windows

Builds a normal Windows setup executable:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\installer\windows\build_windows_installer.ps1
```

Output:

```text
release\AIPhotoSorterSetup-0.2.2.exe
```

The installer:

- installs the bundled app into the user's local Programs folder
- creates Start Menu and optional Desktop shortcuts
- closes any running `AIPhotoSorter.exe` before upgrade
- replaces old app files cleanly
- launches AI Photo Sorter after install
- shows a setup message and downloads:

- llama.cpp runtime binaries
- `gemma-4-E4B-it-Q4_K_S.gguf`
- `mmproj-F16.gguf`
- CUDA llama.cpp runtime automatically when an NVIDIA GPU is detected; otherwise CPU runtime

If local AI setup is interrupted, run the installer again to retry the automatic setup.

## macOS

Builds a macOS product package plus an installer DMG:

```sh
sh installer/macos/build_macos_installer.sh
```

Output:

```text
release/AIPhotoSorter.pkg
release/AIPhotoSorterInstaller.dmg
```

The package and installer DMG install `AI Photo Sorter.app` into `/Applications`, show a note that the local AI download is about 5.8 GB, and run the setup script that downloads llama.cpp plus the pinned GGUF model files.

The macOS app bundle is built as universal2, and the package uses the Apple Silicon llama.cpp runtime on arm64 Macs and the x64 CPU runtime on Intel Macs. A drag-to-Applications DMG is intentionally not built by default because that flow cannot run the local AI setup script during copy.

For public distribution, sign and notarize the `.app`, `.pkg`, and `.dmg` with an Apple Developer ID.

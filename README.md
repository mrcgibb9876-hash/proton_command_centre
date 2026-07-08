# Proton Command Center

[![AUR version](https://img.shields.io/aur/version/proton-command-center?logo=archlinux&label=AUR)](https://aur.archlinux.org/packages/proton-command-center)
[![AUR votes](https://img.shields.io/aur/votes/proton-command-center?label=votes)](https://aur.archlinux.org/packages/proton-command-center)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Install on Arch / CachyOS / EndeavourOS:** `yay -S proton-command-center` — [AUR package page](https://aur.archlinux.org/packages/proton-command-center)


# Proton Command Center

An open-source, local web-based controller to launch and configure your Linux Steam library without loading the Steam Client UI. Features an automated per-game profiling engine that cross-references local files against Valve's hardware reports, ProtonDB tiers, and the umu-protonfixes database.

## Key Features

* **Game Profiling (Auto-tune):** Automatically detects game engines (Unreal 4/5, Unity, RE Engine, Source/Source 2, Godot, GameMaker) and graphics API targets (DX11 vs DX12) directly from game binaries.
* **Hardware Awareness:** Reads VRAM from active display drivers to apply safe memory allocation headroom caps on 8GB VRAM class GPUs. Identifies handheld form factors via DMI strings to apply desktop/handheld targeting overrides dynamically.
* **Data Synthesis:** Aggregates local configurations by checking current ProtonDB tiers against Valve's official Steam Deck verification records. Warns user when public community reports conflict with verified database files.
* **Launch Argument Builder:** GUI toggles for Wayland native rendering, Window Manager decorations, Steam Input bypass, HDR, NTSync, NVAPI, and runtime telemetry overlays (MangoHud). Automatically closes Steam gracefully prior to writing VDF changes to prevent save collisions.
* **NVIDIA DLL Management:** Explicit local library tracker for DLSS Super Resolution, Frame Generation, and Ray Reconstruction binaries. Fetches upstream updates directly from official distribution branches with one-click rollbacks and original artifact backups.
* **Shader Pipeline Automation:** Manages Fossilize shader compilation configurations. Decouples system pipeline recordings from binary shader caches, allowing quick driver cache purges without erasing base pipeline history records.


MIT licensed. Copyright (c) 2026 Marc Gibb.


# VastAIReciever 🚀

Pure native Windows Desktop Application (`VastRenderHub.exe` built with CustomTkinter + Pillow — no web browser required) & GPU render pipeline between **Vast.ai** and your **PC**, locked to **Blender 5.2 LTS** with automatic script execution and real-time frame streaming.

---

## ⚡ Quick Start (Native Windows Desktop App)

1. Double-click **`VastRenderHub.exe`** (or [`START_RECEIVER.bat`](START_RECEIVER.bat) / `python receiver.py`).
2. Under **⚙ Cost Protection & Vast.ai Key**, paste your Vast.ai API Key and click **Save & Refresh Instance**.
3. Click **🎮 Rent / Switch GPU** to pick an RTX 5090, RTX 4090, RTX 3090, or custom GPU (or manage existing instances).
4. Choose your project (**Google Drive**, **Local PC** `.blend` / `.zip` / Folder, or **Vast Local**) and optional Blender add-ons.
5. Configure **Render Settings** (Full Anim / Range / Single, Samples, Auto-Run All Scripts, OptiX/OIDN Denoise, Persistent Data, Simplify Max Subdiv) and click **🚀 Start Render on Vast.ai**.

*(Prefer the legacy terminal interactive mode? Run `python receiver.py --cli`.)*

---

## 🎯 Key Guarantees

1. **Native `.exe` Desktop UI (Low Memory)**: Decodes and displays only the active frame with a bounded 24-frame LRU cache—safe even on 1,000+ frame jobs with zero browser/Edge overhead.
2. **Enforced Blender 5.2 LTS**: Automatically checks installed `blender --version` on Vast.ai; if missing or an older version is present, downloads the latest official `5.2.x` Linux x64 LTS release from `download.blender.org` and symlinks `/usr/local/bin/blender`.
3. **Automatic Script & Driver Execution**:
   - Runs Blender with `-y` (`--enable-autoexec`) and persists `use_scripts_auto_execute = True` into `userpref.blend` for both `root` and `user` (Cloud Desktop GUI).
   - Executes embedded `.blend` Python scripts (`bpy.data.texts`) without double-executing registered scripts, auto-registers a Camera Shakify fallback shim when needed, re-enables invalid drivers, and refreshes Blender 5.2's evaluated depsgraph.
   - Supports bundling custom `.zip`/`.py` add-ons directly from the GUI.
4. **Real-Time Frame Streaming & Cost Protection**:
   - Streams rendered frames immediately to `renders/` with a native frame player, scrubber, and live console.
   - Runs a per-session `X-Worker-Token`-protected Cloudflare tunnel (`127.0.0.1:8888`).
   - Supports **Auto-Stop on Finish** and **Idle Auto-Stop** watchdog protection.

---

## Building `VastRenderHub.exe`

```powershell
python build_exe.py
```

Outputs `VastRenderHub.exe` in the repository root.

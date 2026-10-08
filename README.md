# VastAIReciever 🚀

Fast, zero-config P2P rendering pipeline between **Vast.ai** (Linux Desktop / Jupyter) and your **PC**.

Automatically auto-detects `.blend` files on Vast.ai, enables OptiX / CUDA GPU acceleration for Cycles so credits aren't wasted, and streams each rendered frame directly to your PC in real time.

---

## ⚡ Quick Start

### 1. On your PC (The Receiver)
1. Double-click [`START_RECEIVER.bat`](START_RECEIVER.bat) (or run `python receiver.py`).
2. Paste your Vast.ai SSH command (from the **Connect** button on Vast.ai, e.g. `ssh -p 12345 root@192.0.2.1 ...`).
3. The receiver creates the encrypted tunnel and begins waiting for rendered frames.

### 2. On Vast.ai (Jupyter Terminal)
Paste and run this **one-line command** in the Jupyter terminal:

```bash
curl -sSL https://raw.githubusercontent.com/hamkaaaa/VastAIReciever/main/worker.sh | bash
```

*(Or if you prefer git)*:
```bash
git clone https://github.com/hamkaaaa/VastAIReciever.git /workspace/VastAIReciever && cd /workspace/VastAIReciever && python3 worker.py
```

---

## 🎯 What Happens Automatically

1. **Auto-finds your `.blend` file** in `/workspace`, Desktop, or current directory.
2. **Auto-checks Blender** (downloads official portable Blender 4.2 LTS if not installed).
3. **Forces GPU Cycles compute** (auto-activates OptiX / CUDA devices for maximum RTX render speeds).
4. **Real-time frame streaming**: As each frame finishes rendering, it is instantly uploaded to your PC's `renders/` directory. If the instance terminates or credits run out, you keep all rendered frames.

---

## Options (Optional)

Specify a specific `.blend` file or frame range on Vast.ai:
```bash
python3 worker.py --blend /path/to/my_file.blend --start 1 --end 100
```
Or for a single test frame:
```bash
python3 worker.py --single 1
```

#!/usr/bin/env python3
"""
Vast.ai Worker Script
Runs on Vast.ai Linux environment (Jupyter terminal or shell).
Locates the .blend file, enables GPU, renders, and streams finished frames to your PC receiver in real-time.
"""

import os
import sys
import glob
import time
import argparse
import subprocess
import threading
import urllib.request
import urllib.parse
from pathlib import Path

def find_blender():
    candidates = [
        "blender",
        "/workspace/blender/blender",
        "/usr/bin/blender",
        "/opt/blender/blender",
    ]
    for c in candidates:
        if subprocess.run(["which", c], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return c
        if Path(c).is_file() and os.access(c, os.X_OK):
            return str(Path(c).resolve())

    # Download portable Blender 4.2 if not found
    print("[*] Blender not found. Downloading portable Blender 4.2 LTS to /workspace/blender...")
    dl_cmd = """
    mkdir -p /workspace && cd /workspace && \
    wget -q --show-progress https://download.blender.org/release/Blender4.2/blender-4.2.3-linux-x64.tar.xz -O blender.tar.xz && \
    tar -xf blender.tar.xz && \
    mv blender-4.2.* blender && \
    rm blender.tar.xz
    """
    ret = subprocess.run(dl_cmd, shell=True)
    if ret.returncode == 0 and Path("/workspace/blender/blender").exists():
        return "/workspace/blender/blender"

    print("[!] Failed to obtain Blender. Please install Blender or check disk space.")
    sys.exit(1)

def find_blend_file(explicit_path=None):
    if explicit_path and Path(explicit_path).is_file():
        return str(Path(explicit_path).resolve())

    search_dirs = [
        Path.cwd(),
        Path("/workspace"),
        Path.home() / "Desktop",
        Path.home(),
    ]

    all_blends = []
    for d in search_dirs:
        if d.exists():
            all_blends.extend(list(d.glob("*.blend")))
            all_blends.extend(list(d.glob("**/*.blend")))

    # Deduplicate and sort by modification time (most recent first)
    unique_blends = sorted(list(set(all_blends)), key=lambda p: p.stat().st_mtime, reverse=True)

    if not unique_blends:
        print("[!] No .blend files found in /workspace or home folder.")
        sys.exit(1)

    print(f"\n[+] Found {len(unique_blends)} .blend file(s):")
    for i, b in enumerate(unique_blends[:5]):
        print(f"  [{i+1}] {b} ({b.stat().st_size / (1024*1024):.1f} MB)")

    selected = unique_blends[0]
    print(f"[*] Auto-selecting most recent: {selected.name}")
    return str(selected.resolve())

def upload_frame_to_pc(file_path, server_url):
    """Uploads a single completed frame to the PC receiver"""
    file_path = Path(file_path)
    ret = subprocess.run([
        "curl", "-s",
        "-H", f"X-Filename: {file_path.name}",
        "--data-binary", f"@{file_path}",
        f"{server_url}/upload_frame"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return ret.returncode == 0

def frame_uploader_daemon(output_dir, server_url, stop_event):
    """Monitors output directory and streams rendered frames to PC in real time"""
    uploaded = set()
    while not stop_event.is_set() or len(uploaded) < len(list(Path(output_dir).glob("*.*"))):
        current_files = sorted(list(Path(output_dir).glob("*.*")))
        for f in current_files:
            if f.suffix.lower() in [".png", ".jpg", ".jpeg", ".exr", ".mp4", ".mov"]:
                if f.name not in uploaded:
                    # Check if file has finished writing
                    prev_size = f.stat().st_size
                    time.sleep(0.5)
                    if f.stat().st_size == prev_size and prev_size > 0:
                        success = upload_frame_to_pc(f, server_url)
                        if success:
                            uploaded.add(f.name)
                            print(f"[PC Stream] -> Uploaded {f.name} ({f.stat().st_size / 1024:.1f} KB) to PC receiver")
        time.sleep(1)

def notify_pc_status(server_url, status_msg):
    try:
        subprocess.run(["curl", "-s", "-d", status_msg, f"{server_url}/status"], stdout=subprocess.DEVNULL)
    except Exception:
        pass

def main():
    parser = argparse.ArgumentParser(description="Vast.ai Blender Render Worker")
    parser.add_argument("--blend", help="Path to .blend file")
    parser.add_argument("--server", default="http://localhost:8888", help="PC receiver URL (default: http://localhost:8888)")
    parser.add_argument("--start", type=int, help="Start frame")
    parser.add_argument("--end", type=int, help="End frame")
    parser.add_argument("--single", type=int, help="Single frame number")
    args = parser.parse_args()

    server_url = args.server.rstrip("/")
    print("=" * 60)
    print(" 🚀 VAST.AI BLENDER RENDER WORKER")
    print(f" Target Receiver: {server_url}")
    print("=" * 60)

    # Test receiver connection
    print(f"[*] Testing connection to PC receiver at {server_url}...")
    test_res = subprocess.run(["curl", "-s", "-m", "5", f"{server_url}/ping"], stdout=subprocess.PIPE, text=True)
    if "PONG" not in test_res.stdout:
        print(f"[!] Warning: Could not reach PC receiver at {server_url}.")
        print("    Ensure START_RECEIVER.bat is running on your PC with SSH tunnel connected.")
    else:
        print("[✓] Connected to PC receiver!")

    blender_bin = find_blender()
    print(f"[✓] Using Blender executable: {blender_bin}")

    blend_file = find_blend_file(args.blend)
    print(f"[✓] Scene: {blend_file}")

    output_dir = Path("/workspace/render_output") / Path(blend_file).stem
    output_dir.mkdir(parents=True, exist_ok=True)

    gpu_script = Path(__file__).parent / "enable_gpu.py"
    if not gpu_script.exists():
        # Fallback inline creation
        gpu_script = Path("/workspace/enable_gpu.py")
        gpu_script.write_text("""
import bpy
try:
    bpy.context.scene.render.engine = 'CYCLES'
    cprefs = bpy.context.preferences.addons['cycles'].preferences
    for dev in ('OPTIX', 'CUDA'):
        try:
            cprefs.compute_device_type = dev
            cprefs.get_devices()
            for d in cprefs.devices:
                if d.type == dev:
                    d.use = True
                    bpy.context.scene.cycles.device = 'GPU'
            break
        except:
            pass
except Exception as e:
    print(e)
""")

    # Frame flags
    frame_args = []
    if args.single is not None:
        frame_args = ["-f", str(args.single)]
    elif args.start is not None and args.end is not None:
        frame_args = ["-s", str(args.start), "-e", str(args.end), "-a"]
    else:
        frame_args = ["-a"]

    # Start background frame uploader thread
    stop_event = threading.Event()
    uploader_thread = threading.Thread(target=frame_uploader_daemon, args=(output_dir, server_url, stop_event), daemon=True)
    uploader_thread.start()

    print("\n" + "=" * 60)
    print(" 🔥 RENDERING FRAMES (GPU ACCELERATED)...")
    print("=" * 60)
    notify_pc_status(server_url, f"Rendering started for {Path(blend_file).name}")

    render_cmd = [
        blender_bin,
        "-b", blend_file,
        "-P", str(gpu_script.resolve()),
        "-o", f"{str(output_dir)}/frame_#####",
    ] + frame_args

    print("Running:", " ".join(render_cmd))
    proc = subprocess.run(render_cmd)

    # Let uploader finish remaining frames
    time.sleep(2)
    stop_event.set()
    uploader_thread.join(timeout=10)

    notify_pc_status(server_url, f"COMPLETED {Path(blend_file).name}")
    print("\n" + "=" * 60)
    print(" [✓] RENDER COMPLETED! ALL FRAMES SENT TO YOUR PC.")
    print("=" * 60)

if __name__ == "__main__":
    main()

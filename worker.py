#!/usr/bin/env python3
"""
Vast.ai Worker Script
Runs on Vast.ai Linux environment (Jupyter terminal or shell).
Locates the .blend file, enables GPU, renders, and streams newly rendered frames to your PC receiver in real-time.
Guarantees each frame is uploaded only once, and creates a fresh isolated folder for each render session.
"""

import os
import sys
import re
import glob
import time
import argparse
import subprocess
import threading
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

    print("[!] Failed to obtain Blender. Please check disk space.")
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

def upload_frame_to_pc(file_path, server_url, job_id):
    """Uploads a single completed frame to the PC receiver"""
    file_path = Path(file_path)
    res = subprocess.run([
        "curl", "-s",
        "-o", "/dev/null",
        "-w", "%{http_code}",
        "-H", f"X-Filename: {file_path.name}",
        "-H", f"X-Job: {job_id}",
        "--data-binary", f"@{file_path}",
        f"{server_url}/upload_frame"
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    
    code = res.stdout.strip()
    if res.returncode == 0 and code == "200":
        return True
    else:
        print(f"\n[!] Upload failed for {file_path.name}: HTTP {code} ({res.stderr.strip()})")
        return False

def frame_uploader_daemon(output_dir, server_url, job_id, job_start_time, stop_event):
    """Monitors the current job output directory and streams rendered frames to PC exactly once"""
    uploaded = set()
    while not stop_event.is_set():
        if output_dir.exists():
            for f in sorted(list(output_dir.glob("*.*"))):
                if f.suffix.lower() in [".png", ".jpg", ".jpeg", ".exr", ".mp4", ".mov", ".tga"]:
                    if f.name not in uploaded and f.stat().st_mtime >= (job_start_time - 2):
                        # Verify file has finished writing
                        prev_size = f.stat().st_size
                        time.sleep(0.5)
                        if f.stat().st_size == prev_size and prev_size > 0:
                            print(f"[PC Stream] 🚀 Uploading {f.name} ({f.stat().st_size / 1024:.1f} KB)...")
                            if upload_frame_to_pc(f, server_url, job_id):
                                uploaded.add(f.name)
                                print(f"[PC Stream] ✓ {f.name} delivered to PC (saved once)")
        time.sleep(1)

def notify_pc_status(server_url, status_msg):
    try:
        subprocess.run(["curl", "-s", "-d", status_msg, f"{server_url}/status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    print(f" Target PC Receiver: {server_url}")
    print("=" * 60)

    # Test receiver connection
    print(f"[*] Testing connection to PC receiver at {server_url}...")
    test_res = subprocess.run(["curl", "-s", "-m", "10", f"{server_url}/ping"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if "PONG" not in test_res.stdout:
        print(f"[!] Warning: Could not reach PC receiver at {server_url}.")
        print(f"    Check that receiver.py is running on your PC.")
    else:
        print("[✓] Connected to PC receiver!")

    blender_bin = find_blender()
    print(f"[✓] Using Blender: {blender_bin}")

    blend_file = find_blend_file(args.blend)
    print(f"[✓] Scene: {blend_file}")

    # Generate isolated job ID and fresh directory for this specific render run
    clean_stem = re.sub(r'[^\w\-\.]', '_', Path(blend_file).stem)
    job_id = f"{clean_stem}_{time.strftime('%Y%m%d_%H%M%S')}"
    job_start_time = time.time()

    output_dir = Path("/workspace/render_output") / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    gpu_script = Path(__file__).parent / "enable_gpu.py"
    if not gpu_script.exists():
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

    frame_args = []
    if args.single is not None:
        frame_args = ["-f", str(args.single)]
    elif args.start is not None and args.end is not None:
        frame_args = ["-s", str(args.start), "-e", str(args.end), "-a"]
    else:
        frame_args = ["-a"]

    # Start background uploader monitoring ONLY the fresh job directory
    stop_event = threading.Event()
    uploader_thread = threading.Thread(
        target=frame_uploader_daemon,
        args=(output_dir, server_url, job_id, job_start_time, stop_event),
        daemon=True
    )
    uploader_thread.start()

    print("\n" + "=" * 60)
    print(" 🔥 RENDERING IN PROGRESS (GPU OPTIX/CUDA)...")
    print(f" Job ID: {job_id}")
    print(f" Fresh Output Folder: {output_dir}")
    print("=" * 60)
    notify_pc_status(server_url, f"Rendering job {job_id} started")

    render_cmd = [
        blender_bin,
        "-b", blend_file,
        "-P", str(gpu_script.resolve()),
        "-o", f"{str(output_dir)}/frame_#####",
    ] + frame_args

    print("Executing:", " ".join(render_cmd))
    try:
        proc = subprocess.run(render_cmd)
        if proc.returncode != 0:
            print(f"\n[!] Blender process exited with code {proc.returncode}")
        else:
            print("\n[✓] Blender finished rendering successfully!")
    except KeyboardInterrupt:
        print("\n[!] Render interrupted by user.")
    finally:
        time.sleep(2)
        stop_event.set()
        uploader_thread.join(timeout=15)
        notify_pc_status(server_url, f"Job {job_id} finished")
        
        # Trigger PC Auto-Stop integration if enabled
        try:
            subprocess.run(["curl", "-s", "-d", f"Job {job_id} completed successfully", f"{server_url}/finish_job"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        print("\n" + "=" * 60)
        print(f" [✓] JOB {job_id} COMPLETED.")
        print("=" * 60)

if __name__ == "__main__":
    main()

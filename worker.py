#!/usr/bin/env python3
"""
Vast.ai Worker Script
Runs on Vast.ai Linux environment (Jupyter terminal or shell).
Locates the .blend file, enables GPU, renders, and streams newly rendered frames to your PC receiver in real-time.
Supports dynamic frame ranges, custom samples, and OptiX AI denoising configured directly from your PC dashboard.
"""

import os
import sys
import re
import json
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

    print("[*] Blender tidak ditemukan. Mengunduh portable Blender 5.2 LTS resmi...")
    dl_cmd = """
    mkdir -p /workspace && cd /workspace && \
    (curl -L https://download.blender.org/release/Blender5.2/blender-5.2.2-linux-x64.tar.xz -o blender.tar.xz || \
     wget -q --show-progress https://download.blender.org/release/Blender5.2/blender-5.2.2-linux-x64.tar.xz -O blender.tar.xz || \
     curl -L https://download.blender.org/release/Blender5.2/blender-5.2.0-linux-x64.tar.xz -o blender.tar.xz) && \
    tar -xf blender.tar.xz && \
    (mv blender-5.2.* blender 2>/dev/null || mv blender-5.* blender 2>/dev/null || true) && \
    rm -f blender.tar.xz
    """
    ret = subprocess.run(dl_cmd, shell=True)
    if ret.returncode == 0 and Path("/workspace/blender/blender").exists():
        return "/workspace/blender/blender"

    print("[!] Gagal mengunduh Blender. Silakan periksa koneksi internet atau disk space.")
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

def check_receiver_online(server_url):
    """Pings the PC receiver to test connectivity"""
    try:
        res = subprocess.run([
            "curl", "-s", "--connect-timeout", "4", "-m", "5",
            f"{server_url}/ping"
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        return "PONG" in res.stdout
    except Exception:
        return False

def upload_frame_to_pc(file_path, server_url, job_id):
    """Uploads a single completed frame to the PC receiver"""
    file_path = Path(file_path)
    try:
        res = subprocess.run([
            "curl", "-s",
            "-o", "/dev/null",
            "--connect-timeout", "10",
            "-m", "60",
            "-w", "%{http_code}",
            "-H", f"X-Filename: {file_path.name}",
            "-H", f"X-Job: {job_id}",
            "--data-binary", f"@{file_path}",
            f"{server_url}/upload_frame"
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        code = res.stdout.strip()
        if res.returncode == 0 and code == "200":
            return True, code
        return False, code or str(res.returncode)
    except Exception as e:
        return False, str(e)

def frame_uploader_daemon(output_dir, server_url, job_id, job_start_time, render_finished_event):
    """
    Monitors the current job output directory and streams rendered frames to PC in chronological order.
    If the network drops: pauses uploads, keeps buffering on disk, periodically checks /ping,
    and resumes streaming automatically once PC reconnects without losing a single frame.
    """
    uploaded = set()
    is_offline = False

    while True:
        # Collect all eligible frames rendered since job start
        eligible_frames = []
        if output_dir.exists():
            for f in sorted(list(output_dir.glob("*.*"))):
                if f.suffix.lower() in [".png", ".jpg", ".jpeg", ".exr", ".mp4", ".mov", ".tga"]:
                    if f.stat().st_mtime >= (job_start_time - 2):
                        eligible_frames.append(f)

        pending = [f for f in eligible_frames if f.name not in uploaded]

        if pending:
            # Always upload in chronological frame order
            target_frame = pending[0]

            # Verify Blender has finished writing this frame
            try:
                prev_size = target_frame.stat().st_size
                time.sleep(0.5)
                current_size = target_frame.stat().st_size
            except Exception:
                time.sleep(0.5)
                continue

            if current_size != prev_size or current_size == 0:
                # Still being written by Blender
                time.sleep(0.5)
                continue

            # If we were previously offline, test receiver ping first
            if is_offline:
                if not check_receiver_online(server_url):
                    time.sleep(3)
                    continue
                print(f"\n[PC Stream] 🟢 PC connection restored! Resuming frame delivery...")
                is_offline = False

            print(f"[PC Stream] 🚀 Uploading {target_frame.name} ({current_size / 1024:.1f} KB)...")
            success, code = upload_frame_to_pc(target_frame, server_url, job_id)
            if success:
                uploaded.add(target_frame.name)
                print(f"[PC Stream] ✓ {target_frame.name} delivered to PC")
            else:
                if not is_offline:
                    print(f"\n[PC Stream] ⚠️ Upload failed (HTTP {code}). PC or internet may be disconnected.")
                    print(f"[PC Stream] ⏳ Blender will continue rendering safely in background. Pausing upload stream until PC reconnects...")
                    is_offline = True
                time.sleep(3)
                continue

        else:
            # No pending frames right now
            if render_finished_event.is_set():
                # Blender finished rendering! Double check if any new frames appeared
                time.sleep(1)
                all_rendered = [f for f in output_dir.glob("*.*") if f.suffix.lower() in [".png", ".jpg", ".jpeg", ".exr", ".mp4", ".mov", ".tga"] and f.stat().st_mtime >= (job_start_time - 2)]
                still_pending = [f for f in all_rendered if f.name not in uploaded]
                if not still_pending:
                    print(f"\n[PC Stream] 🏁 All {len(uploaded)} rendered frames delivered to PC!")
                    break

            time.sleep(1)

def notify_pc_status(server_url, status_msg):
    try:
        subprocess.run(["curl", "-s", "-m", "4", "-d", status_msg, f"{server_url}/status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def fetch_job_config(server_url):
    try:
        res = subprocess.run(["curl", "-s", "-m", "5", f"{server_url}/api/job_config"], stdout=subprocess.PIPE, text=True)
        if res.stdout.strip().startswith("{"):
            return json.loads(res.stdout)
    except Exception:
        pass
    return {}

def run_single_render_job(blend_file, frame_args, server_url, blender_bin, gpu_script):
    clean_stem = re.sub(r'[^\w\-\.]', '_', Path(blend_file).stem)
    job_id = f"{clean_stem}_{time.strftime('%Y%m%d_%H%M%S')}"
    job_start_time = time.time()

    output_dir = Path("/workspace/render_output") / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    render_finished_event = threading.Event()
    uploader_thread = threading.Thread(
        target=frame_uploader_daemon,
        args=(output_dir, server_url, job_id, job_start_time, render_finished_event),
        daemon=True
    )
    uploader_thread.start()

    print("\n" + "=" * 60)
    print(" 🔥 RENDERING IN PROGRESS (GPU OPTIX/CUDA)...")
    print(f" Job ID: {job_id}")
    print(f" Output Folder: {output_dir}")
    print("=" * 60)
    notify_pc_status(server_url, f"Rendering job {job_id} started")

    render_cmd = [
        blender_bin,
        "-y", # Enable automatic Python script execution
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
            print("\n[✓] Blender finished rendering all frames!")
    except KeyboardInterrupt:
        print("\n[!] Render interrupted by user.")
    finally:
        # Signal the uploader thread that Blender is finished
        render_finished_event.set()
        print("[*] Waiting for all rendered frames to be safely delivered to PC...")
        uploader_thread.join()
        
        # Trigger PC completion handler (IDR cost calculation & auto-stop)
        print("[*] Notifying PC receiver that render job has completed...")
        for attempt in range(20):
            try:
                res = subprocess.run([
                    "curl", "-s", "-m", "5",
                    "-d", f"Job {job_id} completed successfully",
                    f"{server_url}/finish_job"
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if res.returncode == 0:
                    break
            except Exception:
                pass
            time.sleep(3)

        print("\n" + "=" * 60)
        print(f" [✓] JOB {job_id} COMPLETED SUCCESSFULLY.")
        print("=" * 60)

def main():
    parser = argparse.ArgumentParser(description="Vast.ai Blender Render Worker")
    parser.add_argument("--blend", help="Path to .blend file")
    parser.add_argument("--server", default="http://localhost:8888", help="PC receiver URL (default: http://localhost:8888)")
    parser.add_argument("--start", type=int, help="Start frame")
    parser.add_argument("--end", type=int, help="End frame")
    parser.add_argument("--single", type=int, help="Single frame number")
    parser.add_argument("--daemon", action="store_true", help="Stay running as daemon and listen for render commands from PC")
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

    # Fetch configuration sent from PC receiver
    dash_cfg = fetch_job_config(server_url)
    if dash_cfg:
        try:
            Path("/workspace/render_job_settings.json").write_text(json.dumps(dash_cfg))
        except Exception:
            pass
        print(f"[+] Loaded Configuration from PC: {dash_cfg}")

    blend_file = None
    gdrive_url = dash_cfg.get("gdrive_url")
    if gdrive_url:
        print("\n" + "=" * 60)
        print(" 📥 MENGUNDUH PROYEK DARI GOOGLE DRIVE (GIGABIT SPEED)...")
        print(f" URL: {gdrive_url}")
        print("=" * 60)
        notify_pc_status(server_url, "Downloading project from Google Drive at high speed...")

        try:
            import gdown
        except ImportError:
            print("[*] Menginstall utilitas gdown...")
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "gdown"])
            import gdown

        dest_dir = Path("/workspace")
        before_files = set(dest_dir.glob("*"))

        # Extract file ID
        m = re.search(r'/d/([a-zA-Z0-9_-]+)', gdrive_url) or re.search(r'id=([a-zA-Z0-9_-]+)', gdrive_url)
        file_id = m.group(1) if m else None
        direct_url = f"https://drive.google.com/uc?id={file_id}" if file_id else gdrive_url

        downloaded = None

        # Strategy 1: Python gdown.download (no fuzzy keyword)
        try:
            downloaded = gdown.download(direct_url, output=str(dest_dir) + "/", quiet=False)
        except Exception as e:
            print(f"[!] Warning gdown: {e}")

        # Check if downloaded
        if not downloaded or not Path(downloaded).exists():
            new_f = list(set(dest_dir.glob("*")) - before_files)
            if new_f:
                downloaded = str(new_f[0])

        # Strategy 2: CLI gdown directly with original URL
        if not downloaded or not Path(downloaded).exists():
            print("[*] Mencoba download via gdown CLI...")
            subprocess.run(["gdown", gdrive_url, "-O", str(dest_dir) + "/"])
            new_f = list(set(dest_dir.glob("*")) - before_files)
            if new_f:
                downloaded = str(new_f[0])

        # Strategy 3: CLI gdown with --fuzzy flag
        if not downloaded or not Path(downloaded).exists():
            print("[*] Mencoba download via gdown CLI --fuzzy...")
            subprocess.run(["gdown", "--fuzzy", gdrive_url, "-O", str(dest_dir) + "/"])
            new_f = list(set(dest_dir.glob("*")) - before_files)
            if new_f:
                downloaded = str(new_f[0])

        # Strategy 4: Direct curl fallback
        if not downloaded or not Path(downloaded).exists() and file_id:
            print("[*] Mencoba download via direct curl...")
            curl_target = str(dest_dir / "gdrive_project.zip")
            curl_cmd = f"curl -sSL -c /tmp/gcookies.txt 'https://drive.google.com/uc?export=download&id={file_id}' > /tmp/gresp.html && " \
                       f"CONFIRM=$(grep -o 'confirm=[a-zA-Z0-9_-]*' /tmp/gresp.html | head -n 1 | cut -d= -f2) && " \
                       f"curl -sSL -b /tmp/gcookies.txt 'https://drive.google.com/uc?export=download&confirm='\"$CONFIRM\"'&id={file_id}' -o '{curl_target}'"
            subprocess.run(curl_cmd, shell=True)
            if Path(curl_target).exists() and Path(curl_target).stat().st_size > 1000:
                downloaded = curl_target

        if not downloaded or not Path(downloaded).exists():
            print("[!] Error fatal: Gagal mengunduh file dari Google Drive! Pastikan link diset 'Anyone with link can view'.")
            sys.exit(1)

        dl_path = Path(downloaded)
        size_mb = dl_path.stat().st_size / (1024 * 1024)
        print(f"[✓] File berhasil diunduh dari Google Drive: {dl_path.name} ({size_mb:.1f} MB)!")

        is_zip = dl_path.suffix.lower() == ".zip"
        if not is_zip:
            try:
                import zipfile
                is_zip = zipfile.is_zipfile(dl_path)
            except Exception:
                pass

        if is_zip:
            extract_dir = Path("/workspace/project")
            extract_dir.mkdir(parents=True, exist_ok=True)
            print(f"[📦 Project Unpack] Mengekstrak {dl_path.name} ke {extract_dir}...")
            if subprocess.run(["which", "unzip"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
                subprocess.run("apt-get update -qq && apt-get install -y -qq unzip", shell=True)
            subprocess.run(["unzip", "-o", str(dl_path), "-d", str(extract_dir)])

            candidates = list(extract_dir.glob("*.blend")) + list(extract_dir.glob("**/*.blend"))
            if not candidates:
                print(f"[!] Error: Tidak ditemukan file .blend di dalam {dl_path.name}!")
                sys.exit(1)
            candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
            blend_file = str(candidates[0].resolve())
            print(f"[✓] Scene .blend ditemukan di dalam zip: {blend_file}")
        else:
            blend_file = str(dl_path.resolve())
            print(f"[✓] Scene .blend siap: {blend_file}")

    elif dash_cfg.get("upload_project"):
        proj_name = dash_cfg.get("project_name", "project.blend")
        proj_type = dash_cfg.get("project_type", "blend")
        file_size = dash_cfg.get("file_size", 0)
        size_mb = file_size / (1024 * 1024) if file_size else 0

        dest_file = Path("/workspace") / proj_name
        print("\n" + "=" * 60)
        print(f" 📥 DOWNLOADING PROJECT FROM PC: {proj_name} ({size_mb:.1f} MB)...")
        print("=" * 60)
        notify_pc_status(server_url, f"Downloading project {proj_name} from PC...")

        # Remove incomplete download if file size differs
        if dest_file.exists() and file_size and dest_file.stat().st_size != file_size:
            try:
                dest_file.unlink()
            except Exception:
                pass

        dl_cmd = [
            "curl", "-L",
            "-C", "-",
            "--retry", "5",
            "--retry-delay", "2",
            "--connect-timeout", "15",
            "-o", str(dest_file),
            f"{server_url}/download_project"
        ]
        subprocess.run(dl_cmd)

        if not dest_file.exists() or dest_file.stat().st_size == 0:
            print("[!] Download with resume failed. Retrying fresh download...")
            subprocess.run(["curl", "-L", "-o", str(dest_file), f"{server_url}/download_project"])

        if not dest_file.exists() or dest_file.stat().st_size == 0:
            print("[!] Fatal: Failed to download project file from PC!")
            sys.exit(1)

        print(f"[✓] Project downloaded successfully ({dest_file.stat().st_size / (1024*1024):.1f} MB)!")

        if proj_type == "zip" or dest_file.suffix.lower() == ".zip":
            extract_dir = Path("/workspace/project")
            extract_dir.mkdir(parents=True, exist_ok=True)
            print(f"[📦 Project Unpack] Unpacking {dest_file.name} to {extract_dir}...")
            if subprocess.run(["which", "unzip"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
                print("[*] Installing unzip...")
                subprocess.run("apt-get update -qq && apt-get install -y -qq unzip", shell=True)
            subprocess.run(["unzip", "-o", str(dest_file), "-d", str(extract_dir)])
            
            candidates = list(extract_dir.glob("*.blend")) + list(extract_dir.glob("**/*.blend"))
            if not candidates:
                print(f"[!] Error: No .blend files found inside {dest_file.name}!")
                sys.exit(1)
            candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
            blend_file = str(candidates[0].resolve())
            print(f"[✓] Scene .blend found inside zip: {blend_file}")
        else:
            blend_file = str(dest_file.resolve())
            print(f"[✓] Scene .blend ready: {blend_file}")
    else:
        blend_file = find_blend_file(args.blend)
        print(f"[✓] Scene: {blend_file}")

    gpu_script = Path(__file__).parent / "enable_gpu.py"
    if not gpu_script.exists():
        gpu_script = Path("/workspace/enable_gpu.py")

    # Determine frame args
    start_val = args.start if args.start is not None else dash_cfg.get("start_frame")
    end_val = args.end if args.end is not None else dash_cfg.get("end_frame")
    single_val = args.single if args.single is not None else dash_cfg.get("single_frame")

    frame_args = []
    if single_val is not None and str(single_val).strip() != "":
        frame_args = ["-f", str(single_val)]
        print(f"[✓] Configured for Single Frame: {single_val}")
    elif start_val is not None and end_val is not None and str(start_val).strip() != "" and str(end_val).strip() != "":
        frame_args = ["-s", str(start_val), "-e", str(end_val), "-a"]
        print(f"[✓] Configured for Frame Range: {start_val} to {end_val}")
    else:
        frame_args = ["-a"]
        print("[✓] Configured for Full Animation (-a)")

    if args.daemon:
        print("\n" + "=" * 60)
        print(" 🛰 DAEMON MODE ACTIVE: Listening for jobs from PC dashboard...")
        print("=" * 60)
        notify_pc_status(server_url, "Daemon worker active and listening")
        # Run initial job if configured, then poll
        run_single_render_job(blend_file, frame_args, server_url, blender_bin, gpu_script)
        
        while True:
            time.sleep(3)
            # Poll for new jobs
            poll_res = subprocess.run(["curl", "-s", "-m", "5", f"{server_url}/api/daemon_poll"], stdout=subprocess.PIPE, text=True)
            if poll_res.stdout.strip().startswith("{"):
                try:
                    job_req = json.loads(poll_res.stdout)
                    if job_req.get("active"):
                        print(f"\n[+] Received New Job from Dashboard: {job_req}")
                        Path("/workspace/render_job_settings.json").write_text(json.dumps(job_req))
                        new_f_args = []
                        if job_req.get("single_frame"):
                            new_f_args = ["-f", str(job_req["single_frame"])]
                        elif job_req.get("start_frame") and job_req.get("end_frame"):
                            new_f_args = ["-s", str(job_req["start_frame"]), "-e", str(job_req["end_frame"]), "-a"]
                        else:
                            new_f_args = ["-a"]
                        run_single_render_job(blend_file, new_f_args, server_url, blender_bin, gpu_script)
                except Exception as e:
                    print(f"[!] Daemon poll error: {e}")
    else:
        run_single_render_job(blend_file, frame_args, server_url, blender_bin, gpu_script)

if __name__ == "__main__":
    main()

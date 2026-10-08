#!/usr/bin/env python3
"""
PC Receiver & Web Hub for Vast.ai Blender Renders
Includes Live Frame-by-Frame Video Player & Automated Cost Optimization (Auto-Stop Instance).
"""

import os
import sys
import re
import json
import time
import shutil
import argparse
import threading
import subprocess
import webbrowser
import urllib.parse
import urllib.request
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "renders"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = BASE_DIR / ".vast_config.json"
DASHBOARD_FILE = BASE_DIR / "dashboard.html"

opened_jobs = set()
job_frame_counts = {}
last_activity_time = time.time()

def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {
        "api_key": "",
        "instance_id": "54816424",
        "auto_stop": True,
        "idle_stop": True
    }

def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

def call_vast_api(endpoint, method="GET", data=None):
    cfg = load_config()
    api_key = cfg.get("api_key", "").strip()
    if not api_key:
        return {"error": "No Vast.ai API key configured"}

    url = f"https://console.vast.ai/api/v0/{endpoint.lstrip('/')}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    req_data = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        req_data = json.dumps(data).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()}"}
    except Exception as e:
        return {"error": str(e)}

def stop_instance_api():
    cfg = load_config()
    inst_id = cfg.get("instance_id", "").strip()
    if not inst_id:
        return
    print(f"\n[💰 Cost Saver] Triggering Auto-Stop for Vast.ai Instance {inst_id}...")
    res = call_vast_api(f"instances/{inst_id}/", method="PUT", data={"state": "stopped"})
    print(f"[💰 Cost Saver] Stop Response: {res}")

class RenderReceiverHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global last_activity_time
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Web Dashboard
        if path in ("/", "/dashboard", "/index.html"):
            if DASHBOARD_FILE.exists():
                content = DASHBOARD_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

        elif path == "/ping":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"PONG")
            return

        elif path == "/api/jobs":
            # List job directories in renders/
            jobs = []
            if OUTPUT_DIR.exists():
                for p in sorted(OUTPUT_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                    if p.is_dir() and not p.name.startswith("."):
                        jobs.append(p.name)
            self.send_json(jobs)
            return

        elif path == "/api/frames":
            params = urllib.parse.parse_qs(parsed.query)
            job = params.get("job", [""])[0]
            frames = []
            job_path = OUTPUT_DIR / job
            if job and job_path.is_dir():
                for f in sorted(job_path.glob("*.*")):
                    if f.suffix.lower() in [".png", ".jpg", ".jpeg", ".exr", ".mp4", ".mov", ".tga"]:
                        frames.append(f.name)
            self.send_json(frames)
            return

        elif path.startswith("/renders/"):
            # Serve render images
            rel_path = path[len("/renders/"):]
            file_path = OUTPUT_DIR / urllib.parse.unquote(rel_path)
            if file_path.is_file():
                ext = file_path.suffix.lower()
                mime = "image/png" if ext == ".png" else "image/jpeg"
                content = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(content)
                return
            self.send_response(404)
            self.end_headers()
            return

        elif path == "/api/config":
            self.send_json(load_config())
            return

        elif path == "/api/instance_status":
            cfg = load_config()
            inst_id = cfg.get("instance_id")
            res = call_vast_api(f"instances/{inst_id}/") if inst_id else {}
            if "instances" in res:
                inst_data = res.get("instances", {})
                self.send_json(inst_data)
            else:
                self.send_json(res)
            return

        elif path == "/api/open_folder":
            params = urllib.parse.parse_qs(parsed.query)
            job = params.get("job", [""])[0]
            target = (OUTPUT_DIR / job) if job else OUTPUT_DIR
            try:
                os.system(f'explorer "{target.resolve()}"')
            except Exception:
                pass
            self.send_json({"ok": True})
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        global opened_jobs, job_frame_counts, last_activity_time
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        last_activity_time = time.time()

        if path == "/api/config":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode())
            cur = load_config()
            cur.update(data)
            save_config(cur)
            self.send_json({"status": "saved"})
            return

        elif path == "/api/instance_action":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode())
            action = data.get("action")  # 'start' or 'stop'
            cfg = load_config()
            if action == "stop":
                res = call_vast_api(f"instances/{inst_id}/", method="PUT", data={"state": "stopped"})
                if "error" in res or res.get("success") is False:
                    res = call_vast_api(f"instances/{inst_id}/stop/", method="POST")
            else:
                res = call_vast_api(f"instances/{inst_id}/", method="PUT", data={"state": "running"})
                if "error" in res or res.get("success") is False:
                    res = call_vast_api(f"instances/{inst_id}/start/", method="POST")
            self.send_json({"result": res, "message": f"Instance set to {action}"})
            return

        elif path == "/finish_job":
            length = int(self.headers.get("Content-Length", 0))
            msg = self.rfile.read(length).decode("utf-8", errors="ignore")
            print(f"\n[Vast.ai Render Complete] {msg}")

            # Check Auto-Stop config
            cfg = load_config()
            if cfg.get("auto_stop", True):
                print("[💰 Cost Saver] Auto-Stop is ENABLED. Powering down Vast.ai instance to save credits...")
                stop_instance_api()

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return

        elif path == "/status":
            length = int(self.headers.get('Content-Length', 0))
            msg = self.rfile.read(length).decode('utf-8', errors='ignore')
            print(f"\n[Vast.ai Status] {msg}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return

        elif path == "/upload_frame":
            content_length = int(self.headers.get('Content-Length', 0))
            
            # Extract and sanitize Job ID
            raw_job = self.headers.get('X-Job', 'default_job')
            job_name = re.sub(r'[\r\n\t\x00]', '', raw_job).strip().strip('"\'')
            job_name = re.sub(r'[^\w\-\.]', '_', job_name) or "render_job"

            target_job_dir = OUTPUT_DIR / job_name
            target_job_dir.mkdir(parents=True, exist_ok=True)

            raw_filename = self.headers.get('X-Filename', '')
            if raw_filename:
                filename = re.sub(r'[\r\n\t\x00]', '', raw_filename).strip().strip('"\'')
            else:
                query_params = urllib.parse.parse_qs(parsed.query)
                filename = query_params.get('filename', [''])[0]
                filename = re.sub(r'[\r\n\t\x00]', '', filename).strip().strip('"\'')
            
            if not filename:
                filename = f"frame_{int(time.time()*1000)}.png"

            filename = os.path.basename(filename)
            target_path = target_job_dir / filename
            temp_path = target_job_dir / f".tmp_{filename}"

            bytes_left = content_length
            chunk_size = 65536
            with open(temp_path, "wb") as f:
                while bytes_left > 0:
                    read_amount = min(chunk_size, bytes_left)
                    chunk = self.rfile.read(read_amount)
                    if not chunk:
                        break
                    f.write(chunk)
                    bytes_left -= len(chunk)

            try:
                if target_path.exists():
                    try:
                        target_path.unlink()
                    except Exception:
                        pass
                temp_path.replace(target_path)
            except Exception:
                try:
                    shutil.copy2(temp_path, target_path)
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass

            count = job_frame_counts.get(job_name, 0) + 1
            job_frame_counts[job_name] = count
            size_kb = target_path.stat().st_size / 1024
            print(f"[✓ FRAME RECEIVED] {job_name}/{filename} ({size_kb:.1f} KB) [Total in job: {count}]")

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return

        self.send_response(404)
        self.end_headers()

def start_cloudflare_tunnel():
    cloudflared_bin = BASE_DIR / "cloudflared.exe"
    if not cloudflared_bin.exists():
        cloudflared_bin = "cloudflared"

    print("[*] Starting high-speed Cloudflare Tunnel...")
    cmd = [str(cloudflared_bin), "tunnel", "--url", "http://localhost:8888"]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    tunnel_url = None
    start_time = time.time()

    for line in iter(proc.stdout.readline, ''):
        match = re.search(r'(https://[a-zA-Z0-9\-\.]+\.trycloudflare\.com)', line)
        if match:
            tunnel_url = match.group(1)
            break
        if time.time() - start_time > 20:
            break

    return proc, tunnel_url

def idle_watcher_thread():
    """Powers down instance if completely idle for 10 minutes"""
    global last_activity_time
    while True:
        time.sleep(30)
        cfg = load_config()
        if cfg.get("idle_stop", True) and cfg.get("api_key"):
            if time.time() - last_activity_time > 600: # 10 minutes
                # Check if running first
                inst_id = cfg.get("instance_id")
                st = call_vast_api(f"instances/{inst_id}/")
                if st.get("actual_status") == "running":
                    print("\n[⏰ Idle Timeout] Instance idle for >10 mins with no renders. Auto-stopping to save credits!")
                    stop_instance_api()
                    last_activity_time = time.time()

def main():
    print("=" * 70)
    print(" 🚀 VAST.AI RENDER HUB & LIVE FRAME VIEWER")
    print("=" * 70)

    server = HTTPServer(("0.0.0.0", 8888), RenderReceiverHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print("[✓] Local Web Dashboard active: http://localhost:8888")

    # Start idle watcher daemon
    threading.Thread(target=idle_watcher_thread, daemon=True).start()

    # Automatically open Dashboard in browser
    try:
        webbrowser.open("http://localhost:8888")
    except Exception:
        pass

    tunnel_proc, tunnel_url = start_cloudflare_tunnel()

    if not tunnel_url:
        print("[!] Tunnel setup timed out. Please ensure internet access.")
        sys.exit(1)

    print("\n" + "=" * 70)
    print(" 🎯 COPY & RUN THIS ONE LINE IN YOUR JUPYTER TERMINAL:")
    print("=" * 70)
    print(f"\ncurl -sSL https://raw.githubusercontent.com/hamkaaaa/VastAIReciever/main/worker.sh | bash -s -- --server {tunnel_url}\n")
    print("=" * 70)
    print("[*] Dashboard open at http://localhost:8888")
    print("[*] Listening for renders... (Press Ctrl+C to stop)\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping hub...")
        if tunnel_proc:
            tunnel_proc.terminate()
        server.shutdown()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
PC Receiver for Vast.ai Blender Renders
Compatible with Python 3.8 through Python 3.14+.
Creates a new dedicated folder for every render job and prevents duplicate frame deliveries.
"""

import os
import sys
import re
import time
import shutil
import argparse
import threading
import subprocess
import urllib.parse
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "renders"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

opened_jobs = set()
job_frame_counts = {}

class RenderReceiverHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/ping":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"PONG")
            return
        
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"PC Receiver Online\n")

    def do_POST(self):
        global opened_jobs, job_frame_counts
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/status":
            length = int(self.headers.get('Content-Length', 0))
            msg = self.rfile.read(length).decode('utf-8', errors='ignore')
            print(f"\n[Vast.ai Status] {msg}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return

        elif parsed.path == "/upload_frame":
            content_length = int(self.headers.get('Content-Length', 0))
            
            # Extract and sanitize Job ID
            raw_job = self.headers.get('X-Job', 'default_job')
            job_name = re.sub(r'[\r\n\t\x00]', '', raw_job).strip().strip('"\'')
            job_name = re.sub(r'[^\w\-\.]', '_', job_name) or "render_job"

            # Create new folder for this specific render job
            target_job_dir = OUTPUT_DIR / job_name
            target_job_dir.mkdir(parents=True, exist_ok=True)

            # Extract and sanitize Filename
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

            # Stream binary body directly to disk
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

            # Safe replace on Windows
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
            print(f"[✓ FRAME RECEIVED] {job_name}/{filename} ({size_kb:.1f} KB) [Frames in job: {count}]")

            # Pop open folder in explorer once for this new job
            if job_name not in opened_jobs:
                opened_jobs.add(job_name)
                try:
                    os.system(f'explorer "{target_job_dir.resolve()}"')
                except Exception:
                    pass

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

def main():
    print("=" * 70)
    print(" 📡 VAST.AI PC RENDER RECEIVER")
    print(f" Output directory: {OUTPUT_DIR.resolve()}")
    print("=" * 70)

    server = HTTPServer(("0.0.0.0", 8888), RenderReceiverHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print("[✓] Local receiver active on port 8888")

    tunnel_proc, tunnel_url = start_cloudflare_tunnel()

    if not tunnel_url:
        print("[!] Tunnel setup timed out. Please ensure internet access.")
        sys.exit(1)

    print("\n" + "=" * 70)
    print(" 🎯 ONE COMMAND TO RUN IN VAST.AI JUPYTER TERMINAL:")
    print("=" * 70)
    print(f"\ncurl -sSL https://raw.githubusercontent.com/hamkaaaa/VastAIReciever/main/worker.sh | bash -s -- --server {tunnel_url}\n")
    print("=" * 70)
    print("[*] Listening for incoming renders... (Press Ctrl+C to stop)\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping receiver...")
        if tunnel_proc:
            tunnel_proc.terminate()
        server.shutdown()

if __name__ == "__main__":
    main()

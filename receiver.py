#!/usr/bin/env python3
"""
PC Receiver for Vast.ai Blender Renders
Compatible with Python 3.8 through Python 3.14+.
Uses Cloudflare Tunnel for high-speed, reliable, unrestricted frame delivery to your PC.
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

first_frame_received = False
total_frames = 0

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
        global first_frame_received, total_frames
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
            target_path = OUTPUT_DIR / filename
            temp_path = OUTPUT_DIR / f".tmp_{filename}"

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

            # Safe rename on Windows
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

            total_frames += 1
            size_kb = target_path.stat().st_size / 1024
            print(f"[✓ FRAME RECEIVED] {filename} ({size_kb:.1f} KB) saved to renders/ (Total: {total_frames})")

            if not first_frame_received:
                first_frame_received = True
                try:
                    os.system(f'explorer "{OUTPUT_DIR.resolve()}"')
                except Exception:
                    pass

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return

        self.send_response(404)
        self.end_headers()

def start_cloudflare_tunnel():
    """Starts Cloudflare Tunnel using local cloudflared.exe"""
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
    print(" 📡 VAST.AI PC RENDER RECEIVER (CLOUDFLARE ACCELERATED)")
    print(f" Saves renders to: {OUTPUT_DIR.resolve()}")
    print("=" * 70)

    # Start local HTTP server on port 8888
    server = HTTPServer(("0.0.0.0", 8888), RenderReceiverHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print("[✓] Local receiver active on port 8888")

    # Start Cloudflare tunnel
    tunnel_proc, tunnel_url = start_cloudflare_tunnel()

    if not tunnel_url:
        print("[!] Tunnel setup timed out. Please ensure internet access.")
        sys.exit(1)

    print("\n" + "=" * 70)
    print(" 🎯 COPY & RUN THIS ONE LINE IN YOUR JUPYTER TERMINAL:")
    print("=" * 70)
    print(f"\ncurl -sSL https://raw.githubusercontent.com/hamkaaaa/VastAIReciever/main/worker.sh | bash -s -- --server {tunnel_url}\n")
    print("=" * 70)
    print("[*] Receiver listening! Renders will stream directly to your PC...")
    print("[*] (Do not close this window)\n")

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

#!/usr/bin/env python3
"""
PC Receiver for Vast.ai Blender Renders
Compatible with Python 3.8 through Python 3.14+.
Automatically creates a zero-config secure tunnel so Vast.ai can connect to your PC without SSH keys.
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

OUTPUT_DIR = Path(__file__).parent / "renders"
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
            
            filename = self.headers.get('X-Filename')
            if not filename:
                query_params = urllib.parse.parse_qs(parsed.query)
                filename = query_params.get('filename', [None])[0]
            if not filename:
                filename = f"frame_{int(time.time()*1000)}.png"

            filename = os.path.basename(filename)
            target_path = OUTPUT_DIR / filename

            # Stream binary body directly to disk
            bytes_left = content_length
            chunk_size = 65536
            with open(target_path, "wb") as f:
                while bytes_left > 0:
                    read_amount = min(chunk_size, bytes_left)
                    chunk = self.rfile.read(read_amount)
                    if not chunk:
                        break
                    f.write(chunk)
                    bytes_left -= len(chunk)

            total_frames += 1
            size_kb = target_path.stat().st_size / 1024
            print(f"[✓ Frame #{total_frames}] {filename} ({size_kb:.1f} KB) -> Saved to renders/")

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

def start_auto_tunnel():
    """
    Spawns localhost.run reverse tunnel via native Windows ssh.
    Requires NO signup, NO software installation, NO keys.
    """
    print("[*] Generating instant secure tunnel for Vast.ai...")
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-R", "80:localhost:8888",
        "nokey@localhost.run"
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    tunnel_url = None
    start_time = time.time()

    # Read output until URL is found
    for line in iter(proc.stdout.readline, ''):
        match = re.search(r'(https://[a-zA-Z0-9\-\.]+\.lhr\.life)', line)
        if match:
            tunnel_url = match.group(1)
            break
        if time.time() - start_time > 15:
            break

    return proc, tunnel_url

def main():
    print("=" * 70)
    print(" 📡 VAST.AI PC RENDER RECEIVER")
    print(f" Saves renders to: {OUTPUT_DIR.resolve()}")
    print("=" * 70)

    # Start local HTTP server on port 8888
    server = HTTPServer(("0.0.0.0", 8888), RenderReceiverHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print("[✓] Local receiver active on port 8888")

    # Start instant tunnel
    tunnel_proc, tunnel_url = start_auto_tunnel()

    if not tunnel_url:
        print("[!] Tunnel did not respond with a domain within 15s.")
        print("    You can still use local URL if on same network or port-forwarded: http://localhost:8888")
        tunnel_url = "http://localhost:8888"

    print("\n" + "=" * 70)
    print(" 🎯 COPY & RUN THIS ONE LINE IN YOUR JUPYTER TERMINAL:")
    print("=" * 70)
    print(f"\ncurl -sSL https://raw.githubusercontent.com/hamkaaaa/VastAIReciever/main/worker.sh | bash -s -- --server {tunnel_url}\n")
    print("=" * 70)
    print("[*] Waiting for Vast.ai to start rendering... (Press Ctrl+C to stop)\n")

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

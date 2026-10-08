#!/usr/bin/env python3
"""
PC Receiver for Vast.ai Blender Renders
Listens on port 8888, connects reverse tunnel to Vast.ai, and receives frames in real-time.
"""

import os
import sys
import re
import cgi
import time
import shutil
import argparse
import threading
import subprocess
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

OUTPUT_DIR = Path(__file__).parent / "renders"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

first_frame_received = False

class RenderReceiverHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress routine access logs for cleaner output
        pass

    def do_GET(self):
        if self.path == "/ping":
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
        global first_frame_received
        if self.path == "/status":
            length = int(self.headers.get('Content-Length', 0))
            msg = self.rfile.read(length).decode('utf-8', errors='ignore')
            print(f"\n[Vast.ai Status] {msg}")
            self.send_response(200)
            self.end_headers()
            return

        elif self.path == "/upload_frame":
            content_type = self.headers.get('Content-Type')
            if not content_type or 'multipart/form-data' not in content_type:
                self.send_response(400)
                self.end_headers()
                return

            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    'REQUEST_METHOD': 'POST',
                    'CONTENT_TYPE': self.headers['Content-Type'],
                }
            )

            file_item = form['file']
            filename = form.getvalue('filename', 'frame.png')
            # Sanitize filename
            filename = os.path.basename(filename)

            if file_item.file:
                target_path = OUTPUT_DIR / filename
                with open(target_path, 'wb') as f:
                    shutil.copyfileobj(file_item.file, f)

                size_kb = target_path.stat().st_size / 1024
                print(f"[✓ Received Frame] {filename} ({size_kb:.1f} KB) -> Saved")

                if not first_frame_received:
                    first_frame_received = True
                    # Open Explorer once
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

def parse_ssh_string(ssh_str):
    ssh_str = ssh_str.strip()
    if ssh_str.startswith("ssh "):
        ssh_str = ssh_str[4:].strip()

    port = "22"
    port_match = re.search(r'-p\s+(\d+)', ssh_str)
    if port_match:
        port = port_match.group(1)
        ssh_str = re.sub(r'-p\s+\d+', '', ssh_str)

    identity = None
    id_match = re.search(r'-i\s+([^\s]+)', ssh_str)
    if id_match:
        identity = id_match.group(1)
        ssh_str = re.sub(r'-i\s+[^\s]+', '', ssh_str)

    ssh_str = re.sub(r'-[LDR]\s+[^\s]+', '', ssh_str)
    tokens = [t.strip() for t in ssh_str.split() if t.strip() and not t.startswith('-')]
    target = tokens[0] if tokens else "root@localhost"
    return target, port, identity

def main():
    print("=" * 60)
    print(" 📡 VAST.AI PC RENDER RECEIVER")
    print(f" Saves incoming renders to: {OUTPUT_DIR.resolve()}")
    print("=" * 60)

    cache_file = Path(__file__).parent / ".last_vast_ssh.txt"
    cached_ssh = cache_file.read_text().strip() if cache_file.exists() else ""

    parser = argparse.ArgumentParser(description="PC Receiver for Vast.ai")
    parser.add_argument("--ssh", help="Vast.ai SSH command")
    parser.add_argument("--no-tunnel", action="store_true", help="Don't open SSH tunnel (if using cloudflare or port forward)")
    args = parser.parse_args()

    # Start local HTTP receiver server
    server = HTTPServer(("0.0.0.0", 8888), RenderReceiverHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print("[+] Local HTTP server listening on port 8888")

    tunnel_proc = None
    if not args.no_tunnel:
        ssh_raw = args.ssh
        if not ssh_raw:
            prompt_txt = f"Paste Vast.ai SSH command [{cached_ssh}]: " if cached_ssh else "Paste Vast.ai SSH command (from Vast.ai Connect button): "
            ssh_input = input(prompt_txt).strip()
            ssh_raw = ssh_input if ssh_input else cached_ssh

        if ssh_raw:
            cache_file.write_text(ssh_raw)
            target, port, identity = parse_ssh_string(ssh_raw)
            print(f"[+] Setting up secure reverse tunnel to {target}:{port}...")

            tunnel_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-N", "-R", "8888:localhost:8888", "-p", str(port)]
            if identity:
                tunnel_cmd.extend(["-i", identity])
            tunnel_cmd.append(target)

            tunnel_proc = subprocess.Popen(tunnel_cmd)
            time.sleep(2)
            if tunnel_proc.poll() is not None:
                print("[!] Warning: SSH reverse tunnel exited. Check SSH credentials or port.")
            else:
                print("[✓] Reverse tunnel active!")

    print("\n" + "=" * 60)
    print(" 🎯 ONE COMMAND TO RUN IN VAST.AI JUPYTER TERMINAL:")
    print("=" * 60)
    print("\n   curl -sSL https://raw.githubusercontent.com/hamkaaaa/VastAIReciever/main/worker.sh | bash\n")
    print("=" * 60)
    print("[*] Waiting for renders... (Press Ctrl+C to stop)")

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

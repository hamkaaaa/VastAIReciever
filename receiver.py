#!/usr/bin/env python3
"""
Vast.ai Blender Render Receiver CLI
Interactive terminal application for managing renders, streaming frames live, and auto-stopping your instance.
"""

import os
import sys
import re
import json
import time
import shutil
import zipfile
import argparse
import threading
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

try:
    import msvcrt
except ImportError:
    msvcrt = None

class ThreadedReceiverServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Silently absorb socket disconnections from client/tunnel
        exc_type, _, _ = sys.exc_info()
        if exc_type in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError, OSError):
            return
        super().handle_error(request, client_address)

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "renders"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = BASE_DIR / ".vast_config.json"

total_frames_received = 0
active_job_name = ""
last_activity_time = time.time()
vast_boot_time = None
server_running = True

current_project_file = None
current_project_type = None
current_gdrive_url = ""

def package_folder_to_zip(folder_path):
    cache_dir = BASE_DIR / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cache_dir / f"{folder_path.name}.zip"
    print(f"\n[*] Mengompres folder '{folder_path.name}' menjadi '{zip_path.name}'...")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(folder_path):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".vscode")]
            for file in files:
                file_p = Path(root) / file
                arcname = file_p.relative_to(folder_path)
                zipf.write(file_p, arcname)
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"[✓] Kompresi selesai: {zip_path.name} ({size_mb:.1f} MB)")
    return zip_path

def record_vast_boot_time(ts=None):
    global vast_boot_time
    if ts is None:
        ts = time.time()
    vast_boot_time = ts
    cfg = load_config()
    cfg["last_boot_time"] = ts
    save_config(cfg)
    boot_str = time.strftime("%H:%M:%S", time.localtime(ts))
    print(f"[*] ⏱️ Vast.ai session timer started at {boot_str}")

def clear_vast_boot_time():
    global vast_boot_time
    vast_boot_time = None
    cfg = load_config()
    cfg.pop("last_boot_time", None)
    save_config(cfg)

current_job_config = {
    "start_frame": "",
    "end_frame": "",
    "single_frame": "",
    "samples": 128,
    "denoise": True
}
current_tunnel_url = ""

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

    clean_endpoint = endpoint.lstrip('/')
    if clean_endpoint.startswith("v1/"):
        url = f"https://console.vast.ai/api/{clean_endpoint}"
    else:
        url = f"https://console.vast.ai/api/v0/{clean_endpoint}"

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

def get_all_user_instances():
    """Fetches all instances belonging to user's account via /api/v1/instances/"""
    res = call_vast_api("v1/instances/")
    if isinstance(res, dict) and "instances" in res:
        return res["instances"] or []
    return []

def get_instance_info():
    cfg = load_config()
    api_key = cfg.get("api_key")
    if not api_key:
        return None

    inst_id = cfg.get("instance_id")
    # 1. Try checking the configured instance_id
    if inst_id:
        res = call_vast_api(f"instances/{inst_id}/")
        info = None
        if isinstance(res, dict):
            if isinstance(res.get("instances"), dict):
                info = res["instances"]
            elif "actual_status" in res:
                info = res
        if info and info.get("actual_status"):
            return info

    # 2. Auto-discover active instances from the user's account
    all_instances = get_all_user_instances()
    if all_instances:
        running_inst = [i for i in all_instances if str(i.get("actual_status", "")).lower() == "running"]
        chosen = running_inst[0] if running_inst else all_instances[0]
        chosen_id = str(chosen["id"])
        gpu_name = chosen.get("gpu_name", "GPU")
        status_name = chosen.get("actual_status", "").upper()
        print(f"[+] 🔍 Terdeteksi instance aktif di akun Vast.ai: #{chosen_id} ({gpu_name} - 🟢 {status_name})")
        cfg["instance_id"] = chosen_id
        save_config(cfg)
        return chosen

    return None

def stop_instance_api():
    cfg = load_config()
    inst_id = cfg.get("instance_id", "").strip()
    if not inst_id:
        return
    print(f"\n[💰 Cost Saver] Triggering shutdown for Vast.ai Instance {inst_id}...")
    res = call_vast_api(f"instances/{inst_id}/", method="PUT", data={"state": "stopped"})
    if "error" in res or res.get("success") is False:
        res = call_vast_api(f"instances/{inst_id}/stop/", method="POST")
    print(f"[💰 Cost Saver] Instance {inst_id} has been STOPPED. GPU billing ceased.")
    clear_vast_boot_time()

def start_instance_api():
    cfg = load_config()
    inst_id = cfg.get("instance_id", "").strip()
    if not inst_id:
        return
    print(f"\n[*] Starting Vast.ai Instance {inst_id}...")
    record_vast_boot_time(time.time())
    res = call_vast_api(f"instances/{inst_id}/", method="PUT", data={"state": "running"})
    if "error" in res or res.get("success") is False:
        res = call_vast_api(f"instances/{inst_id}/start/", method="POST")
    print(f"[✓] Start command sent to Vast.ai (Booting GPU container...)")

def ensure_ssh_key_registered():
    """Ensures local ed25519 SSH key exists and is uploaded to Vast.ai account"""
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    key_path = ssh_dir / "id_ed25519"
    pub_path = ssh_dir / "id_ed25519.pub"

    if not key_path.exists():
        print("[*] Membuat SSH key lokal untuk koneksi otomatis...")
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if pub_path.exists():
        pub_key = pub_path.read_text().strip()
        call_vast_api("ssh/", method="POST", data={"ssh_key": pub_key})

def search_available_gpus(gpu_names=("RTX 4090", "RTX 3090", "RTX 5090", "RTX 4080"), max_results=6):
    """Searches available, verified, immediately rentable GPUs on Vast.ai"""
    query_payload = {
        "rentable": {"eq": True},
        "verified": {"eq": True},
        "num_gpus": {"eq": 1},
        "gpu_name": {"in": list(gpu_names)}
    }
    encoded_q = urllib.parse.quote(json.dumps(query_payload))
    res = call_vast_api(f"bundles/?q={encoded_q}")
    if isinstance(res, dict) and "offers" in res:
        offers = res["offers"]
        offers.sort(key=lambda o: (o.get("dph_total", 999), -o.get("reliability2", 0)))
        return offers[:max_results]
    return []

def display_and_rent_gpu_flow():
    """Displays immediately rentable GPUs, lets user pick or auto-selects, rents it with auto-retry, and updates config"""
    print("\n" + "=" * 75)
    print(" 🔍 MENCARI GPU TERBAIK YANG LANGSUNG BISA DISEWA DI VAST.AI...")
    print("=" * 75)
    offers = search_available_gpus(max_results=8)
    if not offers:
        print("[!] Tidak ada GPU yang cocok ditemukan saat ini.")
        return None

    print(f"\n{'#':<4} {'GPU':<12} {'Harga/Jam':<12} {'Internet':<12} {'Lokasi':<18} {'Reliability':<12}")
    print("-" * 75)
    for idx, o in enumerate(offers):
        gpu = o.get("gpu_name", "GPU")
        dph = o.get("dph_total", 0)
        inet = f"{int(o.get('inet_down', 0))} Mbps"
        loc = (o.get("geolocation") or "Unknown")[:17]
        rel = f"{float(o.get('reliability2', 0))*100:.1f}%"
        badge = " [🔥 Rekomendasi #1]" if idx == 0 else ""
        print(f"[{idx+1}]  {gpu:<12} ${dph:<10.3f} {inet:<12} {loc:<18} {rel:<12}{badge}")
    print("-" * 75)

    choice = input("\nPilih nomor GPU untuk disewa [Default: 1]: ").strip() or "1"
    try:
        selected_idx = int(choice) - 1
        if not (0 <= selected_idx < len(offers)):
            selected_idx = 0
    except ValueError:
        selected_idx = 0

    ensure_ssh_key_registered()

    create_payload = {
        "image": "vastai/linux-desktop:cuda-12.9-ubuntu24.04-2026-06-16",
        "disk": 32,
        "runtype": "jupyter_direc ssh_direc ssh_proxy",
        "template_hash_id": "07fd4c405aef10347e8cac7b04453021"
    }

    # Queue candidates to try starting with selected choice
    queue = [offers[selected_idx]] + [o for i, o in enumerate(offers) if i != selected_idx]
    new_inst_id = None
    chosen_offer = None

    for attempt_idx, candidate in enumerate(queue):
        cand_id = candidate["id"]
        cand_gpu = candidate.get("gpu_name", "GPU")
        cand_dph = candidate.get("dph_total", 0.409)
        print(f"\n[*] [{attempt_idx + 1}/{len(queue)}] Menyewa {cand_gpu} (Offer #{cand_id} | ${cand_dph:.3f}/jam)...")
        res = call_vast_api(f"asks/{cand_id}/", method="PUT", data=create_payload)
        new_inst_id = res.get("new_contract") or res.get("id")

        if new_inst_id:
            chosen_offer = candidate
            print(f"[✓] SUKSES! Berhasil menyewa {cand_gpu} (Instance ID: {new_inst_id})!")
            break
        else:
            err_msg = res.get("error") or str(res)
            print(f"[!] Offer #{cand_id} tidak tersedia / diambil pengguna lain: {err_msg}")
            print("[*] 🔄 Otomatis mencoba opsi GPU terbaik berikutnya...")
            time.sleep(1)

    # If all in initial queue failed, refresh offers once and retry
    if not new_inst_id:
        print("\n[*] Menyegarkan penawaran GPU terbaru dari Vast.ai...")
        fresh_offers = search_available_gpus(max_results=6)
        for candidate in fresh_offers:
            cand_id = candidate["id"]
            cand_gpu = candidate.get("gpu_name", "GPU")
            cand_dph = candidate.get("dph_total", 0.409)
            print(f"[*] Mencoba menyewa {cand_gpu} (Offer #{cand_id} | ${cand_dph:.3f}/jam)...")
            res = call_vast_api(f"asks/{cand_id}/", method="PUT", data=create_payload)
            new_inst_id = res.get("new_contract") or res.get("id")
            if new_inst_id:
                chosen_offer = candidate
                print(f"[✓] SUKSES! Berhasil menyewa {cand_gpu} (Instance ID: {new_inst_id})!")
                break
            time.sleep(1)

    if not new_inst_id:
        print("[!] Semua penawaran GPU saat ini gagal disewa. Silakan coba kembali beberapa saat lagi.")
        return None

    cfg = load_config()
    old_id = cfg.get("instance_id")
    if old_id and str(old_id) != str(new_inst_id):
        del_choice = input(f"Hapus instance lama ({old_id}) yang terkunci agar tidak kena biaya disk? [Y/n]: ").strip().lower()
        if del_choice != "n":
            call_vast_api(f"instances/{old_id}/", method="DELETE")
            print(f"[✓] Instance lama {old_id} berhasil dihapus.")

    cfg["instance_id"] = str(new_inst_id)
    save_config(cfg)
    return str(new_inst_id)

def wait_for_instance_running(inst_id, timeout=180):
    """Waits until the instance status is RUNNING and SSH is ready"""
    print(f"[*] Menunggu instance #{inst_id} booting dan siap...")
    start_t = time.time()
    while time.time() - start_t < timeout:
        res = call_vast_api(f"instances/{inst_id}/")
        info = {}
        if isinstance(res, dict):
            if isinstance(res.get("instances"), dict):
                info = res["instances"]
            elif "actual_status" in res:
                info = res

        if not info:
            print(f"\n[!] Instance #{inst_id} tidak ditemukan atau sudah dihapus di Vast.ai.")
            rent_in = input("Sewa GPU baru yang langsung KOSONG & SIAP PAKAI sekarang? [Y/n]: ").strip().lower()
            if rent_in != "n":
                new_id = display_and_rent_gpu_flow()
                if new_id:
                    return wait_for_instance_running(new_id, timeout=timeout)
            return None

        status = str(info.get("actual_status", "")).lower()
        cur_state = str(info.get("cur_state", "")).lower()
        status_msg = str(info.get("status_msg", "")).lower()
        ssh_host = info.get("ssh_host")
        ssh_port = info.get("ssh_port")

        if "scheduling" in status or "scheduling" in cur_state or "in use" in status_msg:
            print("\n" + "!" * 75)
            print(f" ⚠️ INSTANCE #{inst_id} TERKUNCI (SCHEDULING / GPU SEDANG DIPAKAI ORANG LAIN)!")
            print(" Vast.ai tidak bisa menyalakan mesin ini sekarang.")
            print("!" * 75)
            rent_in = input("\nSewa GPU baru yang langsung KOSONG & SIAP PAKAI sekarang? [Y/n]: ").strip().lower()
            if rent_in != "n":
                new_id = display_and_rent_gpu_flow()
                if new_id:
                    return wait_for_instance_running(new_id, timeout=timeout)
            return None
        
        if status == "running" and ssh_host and ssh_port:
            print(f"[✓] Instance 🟢 RUNNING di {ssh_host}:{ssh_port}!")
            return info
        time.sleep(4)
    print("[!] Waktu tunggu habis. Instance mungkin masih initializing.")
    return None

def check_skip_pressed():
    if msvcrt and msvcrt.kbhit():
        ch = msvcrt.getch()
        try:
            key = ch.decode("utf-8", errors="ignore").lower()
        except Exception:
            key = ""
        if key in ("p", "s", "q", "\r", "\n", " "):
            return True
    return False

def auto_execute_command_via_ssh(ssh_host, ssh_port, cmd_string, max_retries=6):
    """Automatically connects via SSH and executes render command, or skips immediately if 'p' key is pressed"""
    print(f"\n[*] 🔌 Menghubungkan SSH otomatis ke root@{ssh_host}:{ssh_port}...")
    print("    👉 [TEKAN TOMBOL 'P' KAPAN SAJA UNTUK LANGSUNG SKIP KE MANUAL / JUPYTER]")

    for attempt in range(max_retries):
        if check_skip_pressed():
            print("\n[⏩ SKIP] Tombol 'p' ditekan! Beralih langsung ke perintah manual...")
            return None

        try:
            test_res = subprocess.run([
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=NUL",
                "-o", "ConnectTimeout=3",
                "-p", str(ssh_port),
                f"root@{ssh_host}",
                "echo READY"
            ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=4)

            if "READY" in test_res.stdout:
                print("[✓] SSH terhubung! Menjalankan setup Blender & render secara otomatis di Vast.ai...")
                proc = subprocess.Popen([
                    "ssh",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=NUL",
                    "-p", str(ssh_port),
                    f"root@{ssh_host}",
                    cmd_string
                ])
                return proc
        except Exception:
            pass

        # Check for keypress during pause
        for _ in range(8):
            if check_skip_pressed():
                print("\n[⏩ SKIP] Tombol 'p' ditekan! Beralih langsung ke perintah manual...")
                return None
            time.sleep(0.2)

    print("[!] SSH otomatis belum terhubung.")
    return None

def get_usd_to_idr_rate():
    """Fetches real-time USD/IDR exchange rate with fallback"""
    try:
        req = urllib.request.Request(
            "https://open.er-api.com/v6/latest/USD",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            return float(data.get("rates", {}).get("IDR", 17800.0))
    except Exception:
        return 17800.0

def calculate_and_display_cost(elapsed_seconds, boot_time=None, stop_time=None):
    """Calculates runtime, USD cost, and IDR cost from instance power-on to power-off"""
    inst_info = get_instance_info()
    hourly_rate = 0.409
    if inst_info and "dph_total" in inst_info:
        try:
            val = float(inst_info["dph_total"])
            if val > 0:
                hourly_rate = val
        except Exception:
            pass

    cost_usd = (elapsed_seconds / 3600.0) * hourly_rate
    idr_rate = get_usd_to_idr_rate()
    cost_idr = cost_usd * idr_rate

    hrs = int(elapsed_seconds // 3600)
    mins = int((elapsed_seconds % 3600) // 60)
    secs = int(elapsed_seconds % 60)

    if hrs > 0:
        time_str = f"{hrs}h {mins}m {secs}s"
    elif mins > 0:
        time_str = f"{mins}m {secs}s"
    else:
        time_str = f"{secs}s"

    formatted_idr = f"Rp {int(round(cost_idr)):,}".replace(",", ".")
    target_folder = (OUTPUT_DIR / active_job_name).resolve() if active_job_name else OUTPUT_DIR.resolve()

    boot_str = time.strftime("%H:%M:%S", time.localtime(boot_time)) if boot_time else "Unknown"
    stop_str = time.strftime("%H:%M:%S", time.localtime(stop_time)) if stop_time else time.strftime("%H:%M:%S")

    print("\n" + "=" * 65)
    print(" 🎉 RENDER COMPLETED ON VAST.AI!")
    print("=" * 65)
    print(f" 📁 Saved to:        {target_folder}")
    print(f" 🖼️  Total Frames:    {total_frames_received} frames")
    print(f" ⏱️  Total Run Time:  {time_str} ({(elapsed_seconds/3600.0):.3f} hours)")
    print(f"    - Vast.ai Boot:  {boot_str}")
    print(f"    - Vast.ai Stop:  {stop_str}")
    print(f" 💵 Vast.ai Rate:    ${hourly_rate:.3f}/hour")
    print(f" 💵 Total Cost (USD): ${cost_usd:.4f}")
    print(f" 🇮🇩 Total Cost (IDR): {formatted_idr} (Rate: ~Rp {int(idr_rate):,}/USD)")
    print("=" * 65)

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
        global last_activity_time, current_project_file, current_project_type
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/ping":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"PONG")
            return

        elif path == "/download_project":
            if not current_project_file or not current_project_file.exists():
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"No project file prepared")
                return

            file_size = current_project_file.stat().st_size
            filename = current_project_file.name

            range_header = self.headers.get("Range")
            start_byte = 0
            end_byte = file_size - 1

            if range_header and range_header.startswith("bytes="):
                try:
                    ranges = range_header[6:].split("-")
                    start_byte = int(ranges[0]) if ranges[0] else 0
                    if len(ranges) > 1 and ranges[1]:
                        end_byte = int(ranges[1])
                except Exception:
                    pass

            content_length = end_byte - start_byte + 1

            if range_header:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start_byte}-{end_byte}/{file_size}")
            else:
                self.send_response(200)

            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(content_length))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

            print(f"\n[📤 PC Upload] Mengirim {filename} ke Vast.ai ({content_length / (1024*1024):.1f} MB)...")
            chunk_size = 256 * 1024
            bytes_sent = 0
            with open(current_project_file, "rb") as f:
                f.seek(start_byte)
                remaining = content_length
                while remaining > 0:
                    read_len = min(chunk_size, remaining)
                    data = f.read(read_len)
                    if not data:
                        break
                    try:
                        self.wfile.write(data)
                        remaining -= len(data)
                        bytes_sent += len(data)
                    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                        print(f"[!] Upload {filename} terputus oleh jaringan.")
                        return

            print(f"[✓] {filename} berhasil dikirim ke Vast.ai!")
            return

        elif path == "/api/job_config":
            resp = dict(current_job_config)
            resp["tunnel_url"] = current_tunnel_url
            resp["gdrive_url"] = current_gdrive_url or ""
            if current_gdrive_url:
                resp["source_type"] = "gdrive"
                resp["upload_project"] = False
            elif current_project_file and current_project_file.exists():
                resp["source_type"] = "pc_upload"
                resp["upload_project"] = True
                resp["project_name"] = current_project_file.name
                resp["project_type"] = current_project_type or ("zip" if current_project_file.suffix.lower() == ".zip" else "blend")
                resp["file_size"] = current_project_file.stat().st_size
            else:
                resp["source_type"] = "vast_local"
                resp["upload_project"] = False
            self.send_json(resp)
            return

        elif path.startswith("/renders/"):
            rel_path = path[len("/renders/"):]
            file_path = OUTPUT_DIR / urllib.parse.unquote(rel_path)
            if file_path.is_file():
                ext = file_path.suffix.lower()
                mime = "image/png" if ext == ".png" else "image/jpeg"
                content = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        global total_frames_received, active_job_name, last_activity_time, vast_boot_time
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        last_activity_time = time.time()

        if not vast_boot_time:
            saved_bt = load_config().get("last_boot_time")
            if saved_bt:
                vast_boot_time = float(saved_bt)
            else:
                record_vast_boot_time(time.time())

        if path == "/status":
            length = int(self.headers.get('Content-Length', 0))
            msg = self.rfile.read(length).decode('utf-8', errors='ignore')
            print(f"\n[Vast.ai Status] {msg}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return

        elif path == "/finish_job":
            length = int(self.headers.get('Content-Length', 0))
            msg = self.rfile.read(length).decode("utf-8", errors="ignore")
            
            stop_time = time.time()
            saved_cfg = load_config()
            boot_t = vast_boot_time or (float(saved_cfg.get("last_boot_time", stop_time)))
            elapsed = max(1.0, stop_time - float(boot_t))
            
            calculate_and_display_cost(elapsed, boot_time=float(boot_t), stop_time=stop_time)

            if saved_cfg.get("auto_stop", True):
                stop_instance_api()

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return

        elif path == "/upload_frame":
            content_length = int(self.headers.get('Content-Length', 0))
            
            raw_job = self.headers.get('X-Job', 'default_job')
            job_name = re.sub(r'[\r\n\t\x00]', '', raw_job).strip().strip('"\'')
            job_name = re.sub(r'[^\w\-\.]', '_', job_name) or "render_job"
            active_job_name = job_name

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

            # If transfer was cut short by internet disconnect, discard truncated file
            if bytes_left > 0:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                print(f"[!] Warning: {filename} transfer interrupted by network drop. Waiting for auto-retry...")
                return

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

            total_frames_received += 1
            size_kb = target_path.stat().st_size / 1024
            timestamp_str = time.strftime("%H:%M:%S")
            print(f"[{timestamp_str}] ✓ [{total_frames_received:03d}] {job_name}/{filename} ({size_kb:.1f} KB) -> Saved")

            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                pass
            return

        self.send_response(404)
        self.end_headers()

def drain_pipe(pipe):
    try:
        for _ in iter(pipe.readline, ''):
            pass
    except Exception:
        pass

def start_cloudflare_tunnel():
    global current_tunnel_url
    cloudflared_bin = BASE_DIR / "cloudflared.exe"
    if not cloudflared_bin.exists():
        cloudflared_bin = "cloudflared"

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

    # Drain remaining output in background so cloudflared stdout buffer never blocks on Windows
    threading.Thread(target=drain_pipe, args=(proc.stdout,), daemon=True).start()

    current_tunnel_url = tunnel_url or ""
    return proc, tunnel_url

def main():
    global current_job_config, vast_boot_time, current_project_file, current_project_type, current_gdrive_url
    print("=" * 70)
    print(" 🚀 VAST.AI BLENDER RENDER RECEIVER (CLI)")
    print("=" * 70)

    cfg = load_config()

    # 1. Pilih Proyek Blender
    print("\n--- 📁 1. PILIH SUMBER PROYEK BLENDER ---")
    print("[1] Google Drive Link (HEMAT BIAYA: Upload di PC saat GPU mati, lalu paste link)")
    print("[2] Upload langsung dari PC via Tunnel (.blend / .zip lokal)")
    print("[3] Gunakan proyek yang sudah ada di Vast.ai")
    
    proj_choice = input("Pilih sumber proyek [default: 1]: ").strip() or "1"
    
    upload_project_needed = False
    current_gdrive_url = ""
    current_project_file = None
    current_project_type = None

    if proj_choice == "1":
        while True:
            raw_url = input("\nMasukkan Link Share Google Drive (.blend atau .zip):\n(Pastikan link diset 'Anyone with the link can view')\n> ").strip().strip('"\'')
            if not raw_url:
                print("[!] Link Google Drive tidak boleh kosong.")
                continue
            if "drive.google.com" not in raw_url and "http" not in raw_url:
                print("[!] Format URL tidak valid. Pastikan link Google Drive yang benar.")
                continue
            current_gdrive_url = raw_url
            print(f"[✓] Google Drive link tersimpan: {current_gdrive_url}")
            break

    elif proj_choice == "2":
        while True:
            raw_path = input("\nMasukkan path file (.blend / .zip) atau folder proyek di PC:\n(Tips: Drag & drop file/folder langsung ke terminal ini)\n> ").strip().strip('"\'')
            if not raw_path:
                found_blends = list(BASE_DIR.glob("*.blend"))
                if found_blends:
                    raw_path = str(found_blends[0])
                    print(f"[*] Menggunakan file .blend lokal: {raw_path}")
                else:
                    print("[!] Path tidak boleh kosong.")
                    continue

            p = Path(raw_path)
            if not p.exists():
                print(f"[!] File atau folder tidak ditemukan: {p}")
                continue

            if p.is_dir():
                blends_in_dir = list(p.glob("*.blend")) + list(p.glob("**/*.blend"))
                if not blends_in_dir:
                    print(f"[!] Peringatan: Tidak ditemukan file .blend di dalam folder {p.name}!")
                    confirm = input("Tetap kompres dan kirim folder ini? [y/N]: ").strip().lower()
                    if confirm != "y":
                        continue
                current_project_file = package_folder_to_zip(p)
                current_project_type = "zip"
            elif p.suffix.lower() == ".zip":
                current_project_file = p
                current_project_type = "zip"
            elif p.suffix.lower() == ".blend":
                current_project_file = p
                current_project_type = "blend"
            else:
                print(f"[!] Format '{p.suffix}' tidak didukung. Gunakan .blend, .zip, atau folder.")
                continue

            size_mb = current_project_file.stat().st_size / (1024 * 1024)
            print(f"[✓] Proyek siap dikirim: {current_project_file.name} ({size_mb:.1f} MB)")
            upload_project_needed = True
            break
    else:
        print("[*] Menggunakan file proyek yang sudah tersimpan di Vast.ai.")

    # 2. Check and display instance status
    inst_data = get_instance_info()
    current_status = (inst_data.get("actual_status", "") if inst_data else "").lower()
    status_msg = (inst_data.get("status_msg", "") if inst_data else "").lower()
    gpu_name = inst_data.get("gpu_name", "GPU") if inst_data else "GPU"
    dph = float(inst_data.get("dph_total", 0.409)) if inst_data else 0.409

    is_stuck = ("scheduling" in current_status or "in use" in status_msg or "offline" in current_status)
    if is_stuck:
        print("\n" + "!" * 75)
        print(f" ⚠️ PERINGATAN: Instance #{cfg.get('instance_id')} berstatus SCHEDULING / GPU DIGUNAKAN ORANG LAIN!")
        print(" Vast.ai tidak bisa menyalakan mesin ini sekarang sampai GPU bebas (bisa berjam-jam/berminggu-minggu).")
        print("!" * 75)
        rent_in = input("\nSewa GPU baru yang langsung KOSONG & SIAP PAKAI sekarang? [Y/n]: ").strip().lower()
        if rent_in != "n":
            new_id = display_and_rent_gpu_flow()
            if new_id:
                record_vast_boot_time(time.time())
                inst_data = wait_for_instance_running(new_id)
    elif inst_data:
        print(f"\n[Vast.ai Status] Instance #{cfg.get('instance_id')}: {current_status.upper()} ({gpu_name} | Rate: ${dph:.3f}/hr)")
        if current_status == "running":
            if cfg.get("last_boot_time"):
                vast_boot_time = float(cfg["last_boot_time"])
                b_str = time.strftime("%H:%M:%S", time.localtime(vast_boot_time))
                print(f"[*] ⏱️ Session timer active (Booted at {b_str})")
            else:
                record_vast_boot_time(time.time())
        else:
            print("[1] Nyalakan instance yang sudah ada")
            print("[2] Cari & sewa GPU baru yang ready")
            opt = input("Pilih opsi [default: 1]: ").strip() or "1"
            if opt == "2":
                new_id = display_and_rent_gpu_flow()
                if new_id:
                    record_vast_boot_time(time.time())
                    inst_data = wait_for_instance_running(new_id)
            else:
                if current_gdrive_url:
                    print("\n[*] 🚀 Proyek sudah siap di Google Drive! Menyalakan instance Vast.ai secara instan...")
                    start_instance_api()
                    inst_data = wait_for_instance_running(cfg.get("instance_id"))
                else:
                    boot_in = input("\nInstance saat ini STOPPED. Nyalakan sekarang? [Y/n]: ").strip().lower()
                    if boot_in != "n":
                        start_instance_api()
                        inst_data = wait_for_instance_running(cfg.get("instance_id"))
    else:
        print("\n[Vast.ai Status] Belum ada instance yang terkonfigurasi.")
        rent_in = input("Cari & sewa GPU baru sekarang? [Y/n]: ").strip().lower()
        if rent_in != "n":
            new_id = display_and_rent_gpu_flow()
            if new_id:
                record_vast_boot_time(time.time())
                inst_data = wait_for_instance_running(new_id)

    # 3. Interactive Render Configuration
    print("\n--- ⚙️  2. PENGATURAN RENDER ---")
    print("[1] Render Full Animation (seluruh frame)")
    print("[2] Render Rentang Frame Tertentu (contoh: 1 sampai 120)")
    print("[3] Render 1 Frame Uji Coba (contoh: frame 10)")
    
    choice = input("Pilih mode [default: 1]: ").strip() or "1"
    
    start_f = ""
    end_f = ""
    single_f = ""

    if choice == "2":
        start_f = input("Start frame [1]: ").strip() or "1"
        end_f = input("End frame [120]: ").strip() or "120"
    elif choice == "3":
        single_f = input("Frame number to render [1]: ").strip() or "1"

    samples_in = input("Cycles Samples [128]: ").strip() or "128"
    denoise_in = input("Aktifkan AI Denoising (OptiX / OpenImageDenoise)? [Y/n]: ").strip().lower()
    denoise_val = denoise_in != "n"

    autostop_in = input("Otomatis matikan Vast.ai setelah selesai render? [Y/n]: ").strip().lower()
    cfg["auto_stop"] = (autostop_in != "n")
    save_config(cfg)

    current_job_config = {
        "upload_project": upload_project_needed,
        "gdrive_url": current_gdrive_url,
        "project_name": current_project_file.name if current_project_file else "",
        "project_type": current_project_type or "",
        "file_size": current_project_file.stat().st_size if current_project_file else 0,
        "start_frame": start_f,
        "end_frame": end_f,
        "single_frame": single_f,
        "samples": samples_in,
        "denoise": denoise_val
    }

    # 4. Start local receiver & Cloudflare tunnel
    print("\n[*] Starting local receiver on port 8888...")
    server = ThreadedReceiverServer(("0.0.0.0", 8888), RenderReceiverHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print("[*] Establishing high-speed Cloudflare tunnel...")
    tunnel_proc, tunnel_url = start_cloudflare_tunnel()

    if not tunnel_url:
        print("[!] Tunnel error. Using localhost.")
        tunnel_url = "http://localhost:8888"

    # Build command
    extra_flags = ""
    if single_f:
        extra_flags += f" --single {single_f}"
    elif start_f and end_f:
        extra_flags += f" --start {start_f} --end {end_f}"

    vast_cmd = f"curl -sSL https://raw.githubusercontent.com/hamkaaaa/VastAIReciever/main/worker.sh | bash -s -- --server {tunnel_url}{extra_flags}"

    # 5. Otomatis hubungkan SSH & jalankan render di Vast.ai
    ssh_host = inst_data.get("ssh_host") if inst_data else None
    ssh_port = inst_data.get("ssh_port") if inst_data else None

    cfg = load_config()
    current_inst_id = cfg.get("instance_id")
    if not ssh_host or not ssh_port or (inst_data and inst_data.get("actual_status") != "running"):
        inst_data = wait_for_instance_running(current_inst_id)
        if inst_data:
            ssh_host = inst_data.get("ssh_host")
            ssh_port = inst_data.get("ssh_port")

    ssh_proc = None
    if ssh_host and ssh_port:
        ssh_proc = auto_execute_command_via_ssh(ssh_host, ssh_port, vast_cmd)

    if not ssh_proc:
        copied_banner = ""
        try:
            p_clip = subprocess.Popen(["clip"], stdin=subprocess.PIPE)
            p_clip.communicate(vast_cmd.encode("utf-8"))
            copied_banner = " [📋 DISALIN KE CLIPBOARD - TINGGAL PASTE / CTRL+V DI TERMINAL]"
        except Exception:
            pass

        print("\n" + "=" * 78)
        print(f" 🎯 JALANKAN PERINTAH INI DI TERMINAL JUPYTER VAST.AI{copied_banner}:")
        print("=" * 78)
        print(f"\n{vast_cmd}\n")
        print("=" * 78)

    print(f"[*] Optimizations: Persistent Data ON | Auto-Scripts ON (-y) | Samples: {samples_in}")
    print(f"[*] Auto-Stop on Completion: {'YES (Will shut down instance)' if cfg['auto_stop'] else 'NO'}")
    if not vast_boot_time:
        record_vast_boot_time(time.time())
    print("\n[*] Listening for incoming frames... (Press Ctrl+C to stop)\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping CLI receiver...")
        if tunnel_proc:
            tunnel_proc.terminate()
        server.shutdown()
        if vast_boot_time:
            now_t = time.time()
            elapsed = max(1.0, now_t - vast_boot_time)
            calculate_and_display_cost(elapsed, boot_time=vast_boot_time, stop_time=now_t)

if __name__ == "__main__":
    main()

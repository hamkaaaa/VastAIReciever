#!/usr/bin/env python3
"""
Vast.ai Blender 5.2 LTS Render Hub (Desktop GUI & CLI Receiver)
Splits the public Cloudflare-tunneled worker receiver (port 8888, token-gated)
from the strictly local GUI server (127.0.0.1:8889), provides GPU marketplace
rent/start/stop/destroy APIs, native Windows file pickers, live log streaming,
and idle auto-stop protection.
"""

import os
import sys

if __name__ == "__main__":
    sys.modules["receiver"] = sys.modules[__name__]
import re
import io
import json
import time
import shutil
import secrets
import zipfile
import argparse
import threading
import subprocess
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

try:
    import msvcrt
except ImportError:
    msvcrt = None

APP_VERSION = "VastRenderHub 2.0 (Blender 5.2 LTS)"

IS_FROZEN = getattr(sys, "frozen", False)
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).resolve().parent if IS_FROZEN else Path(__file__).resolve().parent

OUTPUT_DIR = APP_DIR / "renders"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = APP_DIR / ".vast_config.json"
CACHE_DIR = APP_DIR / ".cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = APP_DIR / "vast_render_hub.log"

SUBPROC_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# Ensure stdio is never None in --noconsole PyInstaller builds
if sys.stdout is None:
    sys.stdout = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
if sys.stderr is None:
    sys.stderr = sys.stdout

WORKER_TOKEN = secrets.token_hex(16)

# Thread-safe state and locks
state_lock = threading.Lock()
log_lock = threading.Lock()
browse_lock = threading.Lock()
tunnel_lock = threading.Lock()
shutdown_event = threading.Event()
launch_cancel_event = threading.Event()

log_buffer = deque(maxlen=1000)
log_seq = 0

total_frames_received = 0
active_job_name = ""
last_activity_time = time.time()
last_gui_heartbeat = time.time()
gui_ever_connected = False
vast_boot_time = None

current_tunnel_url = ""
tunnel_proc = None
tunnel_starting = False
worker_connected = False
worker_status_text = "Idle"
render_active = False
launch_in_progress = False
active_ssh_proc = None

current_project_file = None
current_project_type = None
current_addons_zip = None
pending_daemon_job = None

current_job_config = {
    "source_type": "gdrive",
    "gdrive_url": "",
    "project_path": "",
    "blend_in_zip": "",
    "zip_blend_files": [],
    "vast_blend_path": "",
    "addon_paths": [],
    "render_mode": "all",
    "start_frame": "",
    "end_frame": "",
    "single_frame": "",
    "samples": "",
    "denoise": True,
    "persistent_data": True,
    "use_simplify": False,
    "simplify_subdiv": 2,
    "exec_all_scripts": True,
    "force_cycles": True,
    "project_revision": 0,
}

cached_instance_info = {}
cached_idr_rate = {"value": 17800.0, "ts": 0.0}


def add_log(msg, source="system"):
    global log_seq
    ts = time.strftime("%H:%M:%S")
    lines = str(msg).splitlines() or [""]
    with log_lock:
        for line in lines:
            log_seq += 1
            entry = {"id": log_seq, "ts": ts, "source": source, "text": line}
            log_buffer.append(entry)
    try:
        print(f"[{ts}] [{source}] {msg}", flush=True)
    except Exception:
        pass


class ThreadedReceiverServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc_type, _, _ = sys.exc_info()
        if exc_type in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError, OSError):
            return
        super().handle_error(request, client_address)


def load_config():
    defaults = {
        "api_key": "",
        "instance_id": "",
        "auto_stop": True,
        "idle_stop": True,
        "idle_minutes": 10,
    }
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                defaults.update(data)
        except Exception:
            pass
    return defaults


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def record_vast_boot_time(ts=None):
    global vast_boot_time
    if ts is None:
        ts = time.time()
    vast_boot_time = ts
    cfg = load_config()
    cfg["last_boot_time"] = ts
    save_config(cfg)
    boot_str = time.strftime("%H:%M:%S", time.localtime(ts))
    add_log(f"Vast.ai session timer started at {boot_str}", "cost")


def clear_vast_boot_time():
    global vast_boot_time
    vast_boot_time = None
    cfg = load_config()
    cfg.pop("last_boot_time", None)
    save_config(cfg)


def call_vast_api(endpoint, method="GET", data=None):
    cfg = load_config()
    api_key = cfg.get("api_key", "").strip()
    if not api_key:
        return {"error": "No Vast.ai API key configured"}

    clean_endpoint = endpoint.lstrip("/")
    if clean_endpoint.startswith("v1/"):
        url = f"https://console.vast.ai/api/{clean_endpoint}"
    else:
        url = f"https://console.vast.ai/api/v0/{clean_endpoint}"

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    req_data = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        req_data = json.dumps(data).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {"success": True}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}"}
    except Exception as e:
        return {"error": str(e)}


def get_all_user_instances():
    res = call_vast_api("v1/instances/")
    if isinstance(res, dict) and "instances" in res:
        return res["instances"] or []
    return []


def get_instance_info(auto_discover=True):
    global cached_instance_info
    cfg = load_config()
    if not cfg.get("api_key", "").strip():
        return None

    inst_id = str(cfg.get("instance_id", "")).strip()
    if inst_id:
        res = call_vast_api(f"instances/{inst_id}/")
        info = None
        if isinstance(res, dict):
            if isinstance(res.get("instances"), dict):
                info = res["instances"]
            elif "actual_status" in res or "id" in res:
                info = res
        if info and (info.get("actual_status") or info.get("cur_state")):
            cached_instance_info = info
            return info

    if auto_discover:
        all_instances = get_all_user_instances()
        if all_instances:
            running_inst = [i for i in all_instances if str(i.get("actual_status", "")).lower() == "running"]
            chosen = running_inst[0] if running_inst else all_instances[0]
            chosen_id = str(chosen["id"])
            gpu_name = chosen.get("gpu_name", "GPU")
            status_name = str(chosen.get("actual_status", "")).upper()
            add_log(f"Auto-selected Vast.ai instance #{chosen_id} ({gpu_name} - {status_name})", "vast")
            cfg["instance_id"] = chosen_id
            save_config(cfg)
            cached_instance_info = chosen
            return chosen

    return None


def stop_instance_api(instance_id=None):
    cfg = load_config()
    inst_id = str(instance_id or cfg.get("instance_id", "")).strip()
    if not inst_id:
        return {"error": "No instance ID configured"}
    add_log(f"Stopping Vast.ai Instance #{inst_id}...", "vast")
    res = call_vast_api(f"instances/{inst_id}/", method="PUT", data={"state": "stopped"})
    if "error" in res or res.get("success") is False:
        res = call_vast_api(f"instances/{inst_id}/stop/", method="POST")
    clear_vast_boot_time()
    add_log(f"Instance #{inst_id} STOPPED (GPU billing ceased).", "vast")
    return res


def start_instance_api(instance_id=None):
    cfg = load_config()
    inst_id = str(instance_id or cfg.get("instance_id", "")).strip()
    if not inst_id:
        return {"error": "No instance ID configured"}
    add_log(f"Starting Vast.ai Instance #{inst_id}...", "vast")
    record_vast_boot_time(time.time())
    res = call_vast_api(f"instances/{inst_id}/", method="PUT", data={"state": "running"})
    if "error" in res or res.get("success") is False:
        res = call_vast_api(f"instances/{inst_id}/start/", method="POST")
    return res


def destroy_instance_api(instance_id):
    inst_id = str(instance_id).strip()
    if not inst_id:
        return {"error": "No instance ID specified"}
    add_log(f"Destroying Vast.ai Instance #{inst_id}...", "vast")
    res = call_vast_api(f"instances/{inst_id}/", method="DELETE")
    cfg = load_config()
    if str(cfg.get("instance_id", "")) == inst_id:
        cfg["instance_id"] = ""
        save_config(cfg)
        clear_vast_boot_time()
    return res


def ensure_ssh_key_registered():
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    key_path = ssh_dir / "id_ed25519"
    pub_path = ssh_dir / "id_ed25519.pub"

    if not key_path.exists():
        add_log("Generating local ed25519 SSH key for automated Vast.ai access...", "ssh")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=SUBPROC_FLAGS,
        )

    if pub_path.exists():
        pub_key = pub_path.read_text(encoding="utf-8", errors="ignore").strip()
        call_vast_api("ssh/", method="POST", data={"ssh_key": pub_key})


def search_available_gpus(gpu_names=("RTX 5090", "RTX 4090", "RTX 3090", "RTX 4080"), max_results=20):
    query_payload = {
        "rentable": {"eq": True},
        "verified": {"eq": True},
        "num_gpus": {"eq": 1},
    }
    if gpu_names:
        query_payload["gpu_name"] = {"in": list(gpu_names)}
    encoded_q = urllib.parse.quote(json.dumps(query_payload))
    res = call_vast_api(f"bundles/?q={encoded_q}")
    if isinstance(res, dict) and "offers" in res:
        offers = res["offers"] or []
        offers.sort(key=lambda o: (o.get("dph_total", 999), -o.get("reliability2", 0)))
        return offers[:max_results]
    return []


def rent_gpu_offer(offer_id, fallback_ids=None, delete_old=False):
    ensure_ssh_key_registered()
    create_payload = {
        "image": "vastai/linux-desktop:cuda-12.9-ubuntu24.04-2026-06-16",
        "disk": 32,
        "runtype": "jupyter_direc ssh_direc ssh_proxy",
        "template_hash_id": "07fd4c405aef10347e8cac7b04453021",
    }
    queue = [offer_id] + [oid for oid in (fallback_ids or []) if str(oid) != str(offer_id)]
    last_err = "Offer unavailable"

    for cand_id in queue:
        add_log(f"Attempting to rent Vast.ai offer #{cand_id}...", "vast")
        res = call_vast_api(f"asks/{cand_id}/", method="PUT", data=create_payload)
        new_id = res.get("new_contract") or res.get("id")
        if new_id:
            cfg = load_config()
            old_id = str(cfg.get("instance_id", "")).strip()
            if delete_old and old_id and old_id != str(new_id):
                destroy_instance_api(old_id)
            cfg["instance_id"] = str(new_id)
            save_config(cfg)
            record_vast_boot_time(time.time())
            add_log(f"Successfully rented instance #{new_id}!", "vast")
            return {"success": True, "instance_id": str(new_id)}
        last_err = res.get("error") or str(res)
        add_log(f"Offer #{cand_id} unavailable: {last_err}", "vast")

    return {"success": False, "error": last_err}


def get_usd_to_idr_rate():
    now = time.time()
    if now - cached_idr_rate["ts"] < 1800 and cached_idr_rate["value"] > 0:
        return cached_idr_rate["value"]
    try:
        req = urllib.request.Request("https://open.er-api.com/v6/latest/USD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            rate = float(data.get("rates", {}).get("IDR", 17800.0))
            cached_idr_rate["value"] = rate
            cached_idr_rate["ts"] = now
            return rate
    except Exception:
        return cached_idr_rate["value"]


def compute_cost_metrics(elapsed_seconds):
    hourly_rate = 0.0
    if cached_instance_info and cached_instance_info.get("dph_total"):
        try:
            hourly_rate = float(cached_instance_info["dph_total"])
        except Exception:
            pass
    if hourly_rate <= 0:
        hourly_rate = 0.409
    cost_usd = (max(0.0, elapsed_seconds) / 3600.0) * hourly_rate
    idr_rate = get_usd_to_idr_rate()
    cost_idr = cost_usd * idr_rate
    return {
        "hourly_rate": hourly_rate,
        "idr_rate": idr_rate,
        "cost_usd": cost_usd,
        "cost_idr": cost_idr,
    }


def calculate_and_display_cost(elapsed_seconds, boot_time=None, stop_time=None):
    inst_info = get_instance_info(auto_discover=False)
    if inst_info:
        cached_instance_info.update(inst_info)
    metrics = compute_cost_metrics(elapsed_seconds)
    hrs = int(elapsed_seconds // 3600)
    mins = int((elapsed_seconds % 3600) // 60)
    secs = int(elapsed_seconds % 60)
    time_str = f"{hrs}h {mins}m {secs}s" if hrs > 0 else (f"{mins}m {secs}s" if mins > 0 else f"{secs}s")
    formatted_idr = f"Rp {int(round(metrics['cost_idr'])):,}".replace(",", ".")
    add_log(
        f"Job finished | Duration: {time_str} | Rate: ${metrics['hourly_rate']:.3f}/hr | "
        f"Cost: ${metrics['cost_usd']:.4f} ({formatted_idr})",
        "cost",
    )


def inspect_zip_blend_files(zip_path):
    blends = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if info.filename.lower().endswith(".blend") and "__MACOSX" not in info.filename and not Path(info.filename).name.startswith("._"):
                    blends.append({"name": info.filename, "size": info.file_size})
        blends.sort(key=lambda x: x["size"], reverse=True)
    except Exception:
        pass
    return [b["name"] for b in blends]


def package_folder_to_zip(folder_path):
    zip_path = CACHE_DIR / f"{folder_path.name}.zip"
    add_log(f"Compressing project folder '{folder_path.name}' to '{zip_path.name}'...", "project")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(folder_path):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".vscode")]
            for file in files:
                file_p = Path(root) / file
                arcname = file_p.relative_to(folder_path)
                zipf.write(file_p, arcname)
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    add_log(f"Compressed {zip_path.name} ({size_mb:.1f} MB)", "project")
    return zip_path


def prepare_local_bundles_from_config():
    global current_project_file, current_project_type, current_addons_zip
    with state_lock:
        cfg = dict(current_job_config)

    src_type = cfg.get("source_type", "gdrive")
    proj_path_str = (cfg.get("project_path") or "").strip().strip('"\'')

    if src_type == "gdrive" and not (cfg.get("gdrive_url") or "").strip():
        current_project_file = None
        current_project_type = None
        return False, "Please paste a Google Drive share link first."

    if src_type == "pc_upload":
        if not proj_path_str:
            current_project_file = None
            current_project_type = None
            return False, "Please select a local .blend, .zip, or project folder first."
        p = Path(proj_path_str)
        if not p.exists():
            return False, f"Project path not found: {p}"
        if p.is_dir():
            zip_p = package_folder_to_zip(p)
            current_project_file = zip_p
            current_project_type = "zip"
            with state_lock:
                current_job_config["zip_blend_files"] = inspect_zip_blend_files(zip_p)
        elif p.suffix.lower() == ".zip":
            current_project_file = p
            current_project_type = "zip"
            with state_lock:
                current_job_config["zip_blend_files"] = inspect_zip_blend_files(p)
        elif p.suffix.lower() == ".blend":
            current_project_file = p
            current_project_type = "blend"
        else:
            return False, f"Unsupported project format: {p.suffix}"
    elif src_type != "pc_upload":
        current_project_file = None
        current_project_type = None

    addon_paths = [Path(x) for x in (cfg.get("addon_paths") or []) if x and Path(x).is_file()]
    if addon_paths:
        bundle_zip = CACHE_DIR / "addons_bundle.zip"
        with zipfile.ZipFile(bundle_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for ap in addon_paths:
                zf.write(ap, ap.name)
        current_addons_zip = bundle_zip
        add_log(f"Packaged {len(addon_paths)} Blender add-on(s) for upload.", "project")
    else:
        current_addons_zip = None

    return True, ""


def build_vast_worker_command(daemon=False):
    with state_lock:
        cfg = dict(current_job_config)
    tunnel = current_tunnel_url or "http://localhost:8888"
    flags = f"--server {tunnel} --token {WORKER_TOKEN}"
    if daemon:
        flags = f"--daemon {flags}"
    mode = cfg.get("render_mode", "all")
    sf = str(cfg.get("single_frame", "")).strip()
    s = str(cfg.get("start_frame", "")).strip()
    e = str(cfg.get("end_frame", "")).strip()
    if mode == "single" and sf:
        flags += f" --single {sf}"
    elif mode == "range" and s and e:
        flags += f" --start {s} --end {e}"
    return f"curl -sSL https://raw.githubusercontent.com/hamkaaaa/VastAIReciever/main/worker.sh | bash -s -- {flags}"


def find_or_install_cloudflared():
    candidates = [
        APP_DIR / "cloudflared.exe",
        APP_DIR.parent / "cloudflared.exe",
        RESOURCE_DIR / "cloudflared.exe",
        shutil.which("cloudflared"),
        shutil.which("cloudflared.exe"),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return str(Path(c))
        if c and shutil.which(str(c)):
            return str(c)

    if os.name == "nt":
        target = APP_DIR / "cloudflared.exe"
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        add_log("cloudflared.exe not found locally. Downloading official Cloudflare Tunnel binary...", "tunnel")
        try:
            urllib.request.urlretrieve(url, target)
            if target.is_file() and target.stat().st_size > 1_000_000:
                add_log("Downloaded cloudflared.exe successfully.", "tunnel")
                return str(target)
        except Exception as e:
            add_log(f"Failed to download cloudflared.exe: {e}", "tunnel")
    return None


def start_cloudflare_tunnel():
    global current_tunnel_url, tunnel_proc, tunnel_starting
    with tunnel_lock:
        if current_tunnel_url or (tunnel_proc is not None and tunnel_proc.poll() is None):
            return tunnel_proc, current_tunnel_url
        tunnel_starting = True
        try:
            cf_bin = find_or_install_cloudflared()
            if not cf_bin:
                add_log("cloudflared binary unavailable; tunnel disabled.", "tunnel")
                return None, ""

            cmd = [cf_bin, "tunnel", "--url", "http://127.0.0.1:8888"]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=SUBPROC_FLAGS,
            )
            tunnel_proc = proc

            found_url = ""
            start_t = time.time()
            for line in iter(proc.stdout.readline, ""):
                m = re.search(r'(https://[a-zA-Z0-9\-\.]+\.trycloudflare\.com)', line)
                if m:
                    found_url = m.group(1)
                    break
                if time.time() - start_t > 25 or shutdown_event.is_set():
                    break

            def _drain():
                try:
                    for _ in iter(proc.stdout.readline, ""):
                        if shutdown_event.is_set():
                            break
                except Exception:
                    pass

            threading.Thread(target=_drain, daemon=True).start()
            current_tunnel_url = found_url
            if found_url:
                add_log(f"Cloudflare tunnel active: {found_url}", "tunnel")
            else:
                add_log("Cloudflare tunnel timed out.", "tunnel")
            return proc, found_url
        finally:
            tunnel_starting = False


def ensure_tunnel_async():
    if current_tunnel_url or tunnel_starting or (tunnel_proc is not None and tunnel_proc.poll() is None):
        return
    threading.Thread(target=start_cloudflare_tunnel, daemon=True).start()


def wait_for_instance_running(inst_id, timeout=180, cancel_event=None):
    add_log(f"Waiting for instance #{inst_id} to reach RUNNING + SSH ready...", "vast")
    start_t = time.time()
    while time.time() - start_t < timeout:
        if cancel_event and cancel_event.is_set():
            return None
        res = call_vast_api(f"instances/{inst_id}/")
        info = {}
        if isinstance(res, dict):
            if isinstance(res.get("instances"), dict):
                info = res["instances"]
            elif "actual_status" in res:
                info = res
        if info:
            status = str(info.get("actual_status", "")).lower()
            cur_state = str(info.get("cur_state", "")).lower()
            status_msg = str(info.get("status_msg", "")).lower()
            if "scheduling" in status or "scheduling" in cur_state or "in use" in status_msg:
                add_log(f"Instance #{inst_id} is stuck in SCHEDULING / GPU in use. Please rent another GPU.", "vast")
                return None
            ssh_host = info.get("ssh_host")
            ssh_port = info.get("ssh_port")
            if status == "running" and ssh_host and ssh_port:
                add_log(f"Instance #{inst_id} RUNNING at {ssh_host}:{ssh_port}", "vast")
                cached_instance_info.update(info)
                return info
        time.sleep(4)
    add_log("Timed out waiting for Vast.ai instance to boot.", "vast")
    return None


def launch_render_worker_thread():
    global launch_in_progress, render_active, active_ssh_proc, worker_status_text
    launch_cancel_event.clear()
    launch_in_progress = True
    worker_status_text = "Preparing Vast.ai instance..."
    try:
        # Wait up to 20s if tunnel is still connecting
        wait_t = time.time()
        while not current_tunnel_url and (time.time() - wait_t < 20):
            if launch_cancel_event.is_set():
                return
            time.sleep(0.5)

        if not current_tunnel_url:
            add_log("Cannot launch worker: Cloudflare tunnel is not ready.", "error")
            worker_status_text = "Tunnel Error"
            return

        cfg = load_config()
        inst_id = str(cfg.get("instance_id", "")).strip()
        if not inst_id:
            inst = get_instance_info(auto_discover=True)
            if inst:
                inst_id = str(inst["id"])
        if not inst_id:
            add_log("No Vast.ai instance selected. Please Rent a GPU or set Instance ID first.", "error")
            worker_status_text = "No Instance Selected"
            return

        inst_info = get_instance_info(auto_discover=False)
        status = str((inst_info or {}).get("actual_status", "")).lower()
        if status != "running":
            worker_status_text = "Booting Vast.ai GPU..."
            start_instance_api(inst_id)
            inst_info = wait_for_instance_running(inst_id, timeout=180, cancel_event=launch_cancel_event)
            if not inst_info:
                worker_status_text = "Boot Failed / Cancelled"
                return

        ssh_host = inst_info.get("ssh_host")
        ssh_port = inst_info.get("ssh_port")
        if not ssh_host or not ssh_port:
            inst_info = wait_for_instance_running(inst_id, timeout=60, cancel_event=launch_cancel_event)
            if not inst_info:
                worker_status_text = "SSH Info Missing"
                return
            ssh_host = inst_info.get("ssh_host")
            ssh_port = inst_info.get("ssh_port")

        vast_cmd = build_vast_worker_command(daemon=False)
        worker_status_text = f"Connecting SSH ({ssh_host}:{ssh_port})..."
        add_log(f"Connecting SSH to root@{ssh_host}:{ssh_port}...", "ssh")

        ssh_ready = False
        for attempt in range(1, 31):
            if launch_cancel_event.is_set() or shutdown_event.is_set():
                add_log("SSH launch cancelled by user.", "ssh")
                worker_status_text = "Cancelled"
                return
            try:
                test_res = subprocess.run(
                    [
                        "ssh",
                        "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=NUL" if os.name == "nt" else "/dev/null",
                        "-o", "ConnectTimeout=4",
                        "-p", str(ssh_port),
                        f"root@{ssh_host}",
                        "echo READY",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=6,
                    creationflags=SUBPROC_FLAGS,
                )
                if "READY" in test_res.stdout:
                    ssh_ready = True
                    break
            except Exception:
                pass
            if attempt % 5 == 0:
                add_log(f"Waiting for sshd on Vast.ai ({attempt}/30)...", "ssh")
            time.sleep(3)

        if not ssh_ready:
            add_log("Could not connect via SSH automatically. Copy the command from the UI and paste into Jupyter Terminal.", "ssh")
            worker_status_text = "SSH Failed (Use Manual Cmd)"
            return

        add_log("SSH connected! Starting Blender 5.2 LTS worker on Vast.ai...", "ssh")
        render_active = True
        worker_status_text = "Running Worker Setup..."
        proc = subprocess.Popen(
            [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=NUL" if os.name == "nt" else "/dev/null",
                "-p", str(ssh_port),
                f"root@{ssh_host}",
                vast_cmd,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            errors="replace",
            creationflags=SUBPROC_FLAGS,
        )
        active_ssh_proc = proc
        for line in iter(proc.stdout.readline, ""):
            clean = line.rstrip("\r\n")
            if clean:
                add_log(clean, "ssh")
            if shutdown_event.is_set():
                break
        proc.stdout.close()
        rc = proc.wait()
        active_ssh_proc = None
        add_log(f"SSH worker session exited (code {rc}).", "ssh")
        if render_active:
            render_active = False
            worker_status_text = "Completed" if rc == 0 else f"Exited ({rc})"
    finally:
        launch_in_progress = False
        active_ssh_proc = None


def idle_watchdog_loop():
    while not shutdown_event.wait(30):
        try:
            cfg = load_config()
            if not cfg.get("idle_stop", True):
                continue
            if not cfg.get("api_key", "").strip() or not str(cfg.get("instance_id", "")).strip():
                continue
            if render_active or launch_in_progress:
                continue

            idle_mins = max(1.0, float(cfg.get("idle_minutes", 10)))
            ref_time = max(last_activity_time, vast_boot_time or 0.0)
            idle_elapsed = time.time() - ref_time
            if idle_elapsed < idle_mins * 60:
                continue

            info = get_instance_info(auto_discover=False)
            if info and str(info.get("actual_status", "")).lower() == "running":
                add_log(f"Idle Watchdog: No render activity for {int(idle_elapsed // 60)}m; stopping instance.", "cost")
                stop_instance_api(info.get("id"))
        except Exception as e:
            add_log(f"Idle Watchdog notice: {e}", "system")


# ---------------------------------------------------------------------------
# HTTP Handler 1: Public Cloudflare-Tunneled Worker Receiver (Token-Gated)
# ---------------------------------------------------------------------------
class WorkerTunnelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _is_authorized(self, parsed):
        hdr_tok = (self.headers.get("X-Worker-Token") or "").strip()
        if hdr_tok and secrets.compare_digest(hdr_tok, WORKER_TOKEN):
            return True
        qs = urllib.parse.parse_qs(parsed.query)
        q_tok = (qs.get("token", [""])[0]).strip()
        if q_tok and secrets.compare_digest(q_tok, WORKER_TOKEN):
            return True
        return False

    def _send_json(self, data, code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, code=200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream_file(self, file_path):
        file_size = file_path.stat().st_size
        filename = file_path.name
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

        content_length = max(0, end_byte - start_byte + 1)
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

        add_log(f"Sending {filename} to Vast.ai worker ({content_length / (1024*1024):.1f} MB)...", "worker")
        chunk_size = 256 * 1024
        with open(file_path, "rb") as f:
            f.seek(start_byte)
            remaining = content_length
            while remaining > 0:
                data = f.read(min(chunk_size, remaining))
                if not data:
                    break
                try:
                    self.wfile.write(data)
                    remaining -= len(data)
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                    add_log(f"Upload of {filename} interrupted by network.", "worker")
                    return
        add_log(f"Delivered {filename} to Vast.ai worker.", "worker")

    def _build_worker_job_payload(self):
        with state_lock:
            resp = dict(current_job_config)
        src_type = resp.get("source_type", "gdrive")
        proj_path_str = (resp.get("project_path") or "").strip().strip('"\'')
        needs_prep = False

        if src_type == "pc_upload" and proj_path_str:
            p = Path(proj_path_str)
            if not current_project_file or not current_project_file.is_file():
                needs_prep = True
            elif p.is_file() and current_project_file.resolve() != p.resolve():
                needs_prep = True
            elif p.is_dir() and current_project_file.name != f"{p.name}.zip":
                needs_prep = True

        if resp.get("addon_paths") and (not current_addons_zip or not current_addons_zip.is_file()):
            needs_prep = True

        if needs_prep:
            prepare_local_bundles_from_config()

        resp["tunnel_url"] = current_tunnel_url
        resp["has_addons"] = bool(current_addons_zip and current_addons_zip.is_file())
        if src_type == "gdrive" and resp.get("gdrive_url"):
            resp["upload_project"] = False
        elif src_type == "pc_upload" and current_project_file and current_project_file.is_file():
            resp["gdrive_url"] = ""
            resp["upload_project"] = True
            resp["project_name"] = current_project_file.name
            resp["project_type"] = current_project_type or ("zip" if current_project_file.suffix.lower() == ".zip" else "blend")
            resp["file_size"] = current_project_file.stat().st_size
        else:
            resp["gdrive_url"] = ""
            resp["upload_project"] = False

        add_log(
            f"Serving /api/job_config -> source={src_type}, upload_project={resp['upload_project']}, "
            f"project={resp.get('project_name') or resp.get('gdrive_url') or 'vast_local'}",
            "worker",
        )
        return resp

    def do_GET(self):
        global last_activity_time, worker_connected, pending_daemon_job
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/ping":
            self._send_text("PONG", 200)
            return

        if not self._is_authorized(parsed):
            self._send_text("Unauthorized", 401)
            return

        last_activity_time = time.time()
        worker_connected = True

        if path == "/api/job_config":
            self._send_json(self._build_worker_job_payload())
            return

        if path == "/api/daemon_poll":
            with state_lock:
                job = pending_daemon_job
                pending_daemon_job = None
            if job:
                self._send_json(job)
            else:
                self._send_json({"active": False})
            return

        if path == "/download_project":
            if not current_project_file or not current_project_file.is_file():
                self._send_text("No project file prepared", 404)
                return
            self._stream_file(current_project_file)
            return

        if path == "/download_addons":
            if not current_addons_zip or not current_addons_zip.is_file():
                self._send_text("No add-ons bundle", 404)
                return
            self._stream_file(current_addons_zip)
            return

        self._send_text("Not Found", 404)

    def do_POST(self):
        global total_frames_received, active_job_name, last_activity_time
        global worker_connected, worker_status_text, render_active, vast_boot_time

        parsed = urllib.parse.urlparse(self.path)
        if not self._is_authorized(parsed):
            self._send_text("Unauthorized", 401)
            return

        path = parsed.path
        last_activity_time = time.time()
        worker_connected = True

        if not vast_boot_time:
            saved_bt = load_config().get("last_boot_time")
            if saved_bt:
                vast_boot_time = float(saved_bt)

        length = int(self.headers.get("Content-Length", 0))

        if path == "/status":
            msg = self.rfile.read(length).decode("utf-8", errors="replace").strip()
            worker_status_text = msg or "Running"
            render_active = True
            add_log(msg, "worker")
            self._send_text("OK")
            return

        if path == "/log":
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
                txt = data.get("text", "")
            except Exception:
                txt = raw
            if txt:
                add_log(txt, "blender")
            self._send_text("OK")
            return

        if path == "/finish_job":
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            success = True
            is_daemon = False
            rc = 0
            try:
                info = json.loads(raw)
                success = bool(info.get("success", True))
                is_daemon = bool(info.get("is_daemon", False))
                rc = int(info.get("returncode", 0))
            except Exception:
                pass

            render_active = False
            worker_status_text = "Completed" if success else f"Failed (code {rc})"
            stop_time = time.time()
            saved_cfg = load_config()
            boot_t = vast_boot_time or float(saved_cfg.get("last_boot_time", stop_time))
            elapsed = max(1.0, stop_time - float(boot_t))
            calculate_and_display_cost(elapsed, boot_time=float(boot_t), stop_time=stop_time)

            if saved_cfg.get("auto_stop", True) and not is_daemon:
                stop_instance_api()
            elif saved_cfg.get("auto_stop", True) and is_daemon:
                add_log("Daemon mode: keeping Vast.ai instance alive for queued jobs.", "vast")

            self._send_text("OK")
            return

        if path == "/upload_frame":
            raw_job = self.headers.get("X-Job", "default_job")
            job_name = re.sub(r'[\r\n\t\x00]', '', raw_job).strip().strip('"\'')
            job_name = re.sub(r'[^\w\-\.]', '_', job_name) or "render_job"
            active_job_name = job_name

            target_job_dir = OUTPUT_DIR / job_name
            target_job_dir.mkdir(parents=True, exist_ok=True)

            raw_filename = self.headers.get("X-Filename", "")
            if raw_filename:
                filename = re.sub(r'[\r\n\t\x00]', '', raw_filename).strip().strip('"\'')
            else:
                qp = urllib.parse.parse_qs(parsed.query)
                filename = re.sub(r'[\r\n\t\x00]', '', qp.get("filename", [""])[0]).strip().strip('"\'')
            if not filename:
                filename = f"frame_{int(time.time()*1000)}.png"

            filename = os.path.basename(filename)
            target_path = target_job_dir / filename
            temp_path = target_job_dir / f".tmp_{filename}"

            bytes_left = length
            chunk_size = 65536
            with open(temp_path, "wb") as f:
                while bytes_left > 0:
                    chunk = self.rfile.read(min(chunk_size, bytes_left))
                    if not chunk:
                        break
                    f.write(chunk)
                    bytes_left -= len(chunk)

            if bytes_left > 0:
                temp_path.unlink(missing_ok=True)
                add_log(f"{filename} transfer interrupted; awaiting worker retry.", "worker")
                self._send_text("Incomplete", 499)
                return

            try:
                if target_path.exists():
                    target_path.unlink(missing_ok=True)
                temp_path.replace(target_path)
            except Exception:
                shutil.copy2(temp_path, target_path)
                temp_path.unlink(missing_ok=True)

            total_frames_received += 1
            size_kb = target_path.stat().st_size / 1024.0
            add_log(f"Frame [{total_frames_received:03d}] {job_name}/{filename} ({size_kb:.1f} KB) saved.", "frame")
            self._send_text("OK")
            return

        self._send_text("Not Found", 404)


# ---------------------------------------------------------------------------
# Native Desktop App Launcher & Legacy CLI Fallback
# ---------------------------------------------------------------------------
def start_servers(worker_port=8888):
    worker_server = ThreadedReceiverServer(("127.0.0.1", worker_port), WorkerTunnelHandler)
    threading.Thread(target=worker_server.serve_forever, daemon=True).start()
    return worker_server


def cleanup_processes(worker_server, _unused=None):
    shutdown_event.set()
    if active_ssh_proc is not None:
        try:
            active_ssh_proc.terminate()
        except Exception:
            pass
    if tunnel_proc is not None:
        try:
            tunnel_proc.terminate()
        except Exception:
            pass
    if worker_server is not None:
        try:
            worker_server.shutdown()
            worker_server.server_close()
        except Exception:
            pass


def run_self_test():
    import customtkinter  # noqa: F401
    from PIL import ImageTk  # noqa: F401
    import desktop_app

    ico = desktop_app._resolve_icon_path()
    assert ico is not None and ico.is_file(), f"Missing app_icon.ico (RESOURCE_DIR={RESOURCE_DIR})"
    assert desktop_app.receiver is sys.modules[__name__], "desktop_app.receiver must alias __main__"
    assert desktop_app.receiver.WORKER_TOKEN == WORKER_TOKEN, "WORKER_TOKEN mismatch between modules"

    ws = start_servers(worker_port=0)
    wport = ws.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{wport}/ping", timeout=3) as r:
            assert r.read() == b"PONG"
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{wport}/api/job_config", timeout=3)
            raise AssertionError("Expected 401 without token")
        except urllib.error.HTTPError as e:
            assert e.code == 401

        desktop_app.receiver.current_job_config["source_type"] = "gdrive"
        desktop_app.receiver.current_job_config["gdrive_url"] = "https://drive.google.com/file/d/SELFTEST/view"

        req = urllib.request.Request(
            f"http://127.0.0.1:{wport}/api/job_config",
            headers={"X-Worker-Token": desktop_app.receiver.WORKER_TOKEN},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            assert r.status == 200
            payload = json.loads(r.read())
            assert payload.get("gdrive_url") == "https://drive.google.com/file/d/SELFTEST/view"

        app = desktop_app.VastRenderHubApp(worker_server=ws)
        app.withdraw()
        app.update_idletasks()
        app._on_close()
        print(f"SELF_TEST_OK worker_port={wport}")
    finally:
        cleanup_processes(ws)


def run_gui_mode():
    import desktop_app

    add_log(f"Starting {APP_VERSION} (Native Desktop)...", "system")
    worker_server = start_servers(worker_port=8888)
    add_log("Worker tunnel backend listening on 127.0.0.1:8888", "system")

    ensure_tunnel_async()
    threading.Thread(target=idle_watchdog_loop, daemon=True).start()

    try:
        desktop_app.run_desktop_app(worker_server)
    finally:
        cleanup_processes(worker_server)


def run_cli_mode():
    """Legacy interactive terminal mode."""
    global current_project_file, current_project_type
    print("=" * 70)
    print(f" 🚀 {APP_VERSION} - CLI MODE")
    print("=" * 70)
    cfg = load_config()
    if not cfg.get("api_key"):
        key = input("Masukkan Vast.ai API Key Anda: ").strip()
        if key:
            cfg["api_key"] = key
            save_config(cfg)

    print("\n--- 📁 1. PILIH SUMBER PROYEK BLENDER ---")
    print("[1] Google Drive Link")
    print("[2] File/Folder Lokal (.blend / .zip / folder)")
    print("[3] Proyek yang sudah ada di Vast.ai")
    p_choice = input("Pilih sumber [1]: ").strip() or "1"

    with state_lock:
        if p_choice == "1":
            current_job_config["source_type"] = "gdrive"
            current_job_config["gdrive_url"] = input("Link Google Drive: ").strip().strip('"\'')
        elif p_choice == "2":
            current_job_config["source_type"] = "pc_upload"
            current_job_config["project_path"] = input("Path file/folder lokal: ").strip().strip('"\'')
        else:
            current_job_config["source_type"] = "vast_local"

        print("\n--- ⚙️  2. PENGATURAN RENDER (BLENDER 5.2 LTS) ---")
        mode_in = input("Mode: [1] Full Anim, [2] Range, [3] Single Frame [1]: ").strip() or "1"
        if mode_in == "2":
            current_job_config["render_mode"] = "range"
            current_job_config["start_frame"] = input("Start frame [1]: ").strip() or "1"
            current_job_config["end_frame"] = input("End frame [120]: ").strip() or "120"
        elif mode_in == "3":
            current_job_config["render_mode"] = "single"
            current_job_config["single_frame"] = input("Frame number [1]: ").strip() or "1"
        else:
            current_job_config["render_mode"] = "all"

        current_job_config["samples"] = input("Samples [kosong = default .blend]: ").strip()
        current_job_config["denoise"] = input("OptiX/OIDN Denoise? [Y/n]: ").strip().lower() != "n"
        current_job_config["persistent_data"] = input("Persistent Data? [Y/n]: ").strip().lower() != "n"
        current_job_config["exec_all_scripts"] = True

    ok, err = prepare_local_bundles_from_config()
    if not ok:
        print(f"[!] Error: {err}")
        sys.exit(1)

    worker_server = start_servers(worker_port=8888)
    start_cloudflare_tunnel()
    threading.Thread(target=idle_watchdog_loop, daemon=True).start()
    threading.Thread(target=launch_render_worker_thread, daemon=True).start()

    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup_processes(worker_server)


def main():
    parser = argparse.ArgumentParser(description=APP_VERSION)
    parser.add_argument("--cli", action="store_true", help="Run interactive terminal CLI mode")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    parser.add_argument("--self-test", action="store_true", help="Run smoke test and exit")
    args = parser.parse_args()

    if args.version:
        print(APP_VERSION)
        return
    if args.self_test:
        run_self_test()
        return
    if args.cli:
        run_cli_mode()
    else:
        run_gui_mode()


if __name__ == "__main__":
    main()

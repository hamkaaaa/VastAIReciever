#!/usr/bin/env python3
"""
Vast.ai Worker Script
Runs on Vast.ai Linux environment (SSH / Jupyter terminal).
Enforces Blender 5.2 LTS, persists auto-execution of Python scripts across users,
installs bundled add-ons, configures OptiX/CUDA GPU rendering, and streams frames
and live logs back to the PC receiver.
"""

import os
import sys
import re
import json
import time
import shutil
import zipfile
import argparse
import subprocess
import threading
import urllib.request
from pathlib import Path

BLENDER_RELEASE_INDEX = "https://download.blender.org/release/Blender5.2/"
BLENDER_FALLBACK_PATCHES = ["5.2.4", "5.2.3", "5.2.2", "5.2.0"]


def _auth_headers(token):
    if token:
        return ["-H", f"X-Worker-Token: {token}"]
    return []


def get_blender_version(binary):
    """Returns Blender's `--version` output string, or empty string if unavailable."""
    try:
        res = subprocess.run(
            [binary, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        if res.returncode == 0:
            return (res.stdout or "").strip()
    except Exception:
        pass
    return ""


def discover_latest_blender_52_urls():
    """Fetches the official Blender 5.2 release index and orders newest patch builds first."""
    urls = []
    try:
        req = urllib.request.Request(BLENDER_RELEASE_INDEX, headers={"User-Agent": "VastBlenderWorker/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        matches = re.findall(r'blender-5\.2\.(\d+)-linux-x64\.tar\.xz', html)
        patches = sorted({int(p) for p in matches}, reverse=True)
        for p in patches:
            urls.append(f"{BLENDER_RELEASE_INDEX}blender-5.2.{p}-linux-x64.tar.xz")
    except Exception as e:
        print(f"[*] Could not query Blender 5.2 release index ({e}); using fallback URLs.")

    for ver in BLENDER_FALLBACK_PATCHES:
        u = f"{BLENDER_RELEASE_INDEX}blender-{ver}-linux-x64.tar.xz"
        if u not in urls:
            urls.append(u)
    return urls


def find_blender():
    """Locate an existing Blender 5.2 LTS binary or download the latest official 5.2.x LTS build."""
    raw_candidates = [
        "/workspace/blender/blender",
        shutil.which("blender"),
        "/usr/local/bin/blender",
        "/opt/blender/blender",
        "/usr/bin/blender",
    ]
    seen = set()
    candidates = []
    for c in raw_candidates:
        if not c:
            continue
        p = str(Path(c).resolve()) if Path(c).exists() else c
        if p not in seen:
            seen.add(p)
            candidates.append(c)

    for c in candidates:
        if not (Path(c).is_file() and os.access(c, os.X_OK)) and shutil.which(c) is None:
            continue
        ver_out = get_blender_version(c)
        if "Blender 5.2." in ver_out:
            first_line = ver_out.splitlines()[0] if ver_out else "Blender 5.2"
            print(f"[✓] Verified {first_line} at {c}")
            return str(Path(c).resolve()) if Path(c).exists() else c
        elif ver_out:
            first_line = ver_out.splitlines()[0]
            print(f"[*] Skipping {c} ({first_line}) — Blender 5.2 LTS is required.")

    print("[*] Blender 5.2 LTS not found. Downloading latest official Blender 5.2 LTS...")
    urls = discover_latest_blender_52_urls()
    tar_path = Path("/workspace/blender.tar.xz")
    extract_tmp = Path("/workspace/_blender_extract")
    install_dir = Path("/workspace/blender")

    Path("/workspace").mkdir(parents=True, exist_ok=True)
    downloaded = False
    for url in urls:
        print(f"[*] Downloading {url} ...")
        tar_path.unlink(missing_ok=True)
        res = subprocess.run(["curl", "-fSL", "--retry", "3", "--retry-delay", "2", "-o", str(tar_path), url])
        if res.returncode != 0 or not tar_path.is_file() or tar_path.stat().st_size < 10_000_000:
            res_w = subprocess.run(["wget", "-q", "--show-progress", "-O", str(tar_path), url])
            if res_w.returncode != 0 or not tar_path.is_file() or tar_path.stat().st_size < 10_000_000:
                continue
        downloaded = True
        break

    if not downloaded:
        print("[!] Fatal: Failed to download Blender 5.2 LTS. Check internet or disk space.")
        sys.exit(1)

    shutil.rmtree(extract_tmp, ignore_errors=True)
    extract_tmp.mkdir(parents=True, exist_ok=True)
    tar_res = subprocess.run(["tar", "-xf", str(tar_path), "-C", str(extract_tmp)])
    tar_path.unlink(missing_ok=True)
    if tar_res.returncode != 0:
        print("[!] Fatal: Failed to extract Blender 5.2 LTS archive.")
        sys.exit(1)

    extracted_dirs = [p for p in extract_tmp.iterdir() if p.is_dir() and p.name.startswith("blender-5.2")]
    if not extracted_dirs:
        extracted_dirs = [p for p in extract_tmp.iterdir() if p.is_dir()]
    if not extracted_dirs:
        print("[!] Fatal: Extracted archive did not contain a Blender directory.")
        sys.exit(1)

    shutil.rmtree(install_dir, ignore_errors=True)
    shutil.move(str(extracted_dirs[0]), str(install_dir))
    shutil.rmtree(extract_tmp, ignore_errors=True)

    blender_bin = install_dir / "blender"
    if not blender_bin.is_file():
        print("[!] Fatal: /workspace/blender/blender missing after extraction.")
        sys.exit(1)

    try:
        subprocess.run(["ln", "-sf", str(blender_bin), "/usr/local/bin/blender"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    ver_out = get_blender_version(str(blender_bin))
    if "Blender 5.2." not in ver_out:
        print(f"[!] Fatal: Expected Blender 5.2 LTS, got: {ver_out}")
        sys.exit(1)

    print(f"[✓] Installed {ver_out.splitlines()[0]} at {blender_bin}")
    return str(blender_bin)


def ensure_blender_autoexec_config(blender_bin):
    """
    Ensures Blender 5.2 LTS enables Python script auto-execution persistently for both root
    (SSH/headless renders) and `user` (Vast.ai Linux Desktop GUI).
    """
    blender_dir = Path(blender_bin).resolve().parent
    startup_dir = blender_dir / "5.2" / "scripts" / "startup"
    if startup_dir.is_dir():
        startup_hook = startup_dir / "vast_autoexec_init.py"
        hook_code = (
            "import bpy\n"
            "from bpy.app.handlers import persistent\n\n"
            "@persistent\n"
            "def _vast_enable_autoexec(_dummy=None):\n"
            "    try:\n"
            "        bpy.context.preferences.filepaths.use_scripts_auto_execute = True\n"
            "    except Exception:\n"
            "        pass\n\n"
            "def register():\n"
            "    _vast_enable_autoexec()\n"
            "    if _vast_enable_autoexec not in bpy.app.handlers.load_post:\n"
            "        bpy.app.handlers.load_post.append(_vast_enable_autoexec)\n\n"
            "def unregister():\n"
            "    if _vast_enable_autoexec in bpy.app.handlers.load_post:\n"
            "        bpy.app.handlers.load_post.remove(_vast_enable_autoexec)\n"
        )
        try:
            startup_hook.write_text(hook_code, encoding="utf-8")
            print(f"[✓] Installed startup autoexec hook: {startup_hook}")
        except Exception as e:
            print(f"[*] Notice: Could not write startup hook: {e}")

    try:
        subprocess.run(
            [
                blender_bin, "-b", "--python-expr",
                "import bpy; bpy.context.preferences.filepaths.use_scripts_auto_execute = True; bpy.ops.wm.save_userpref()"
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=25,
        )
    except Exception as e:
        print(f"[*] Notice: Could not save root userpref.blend: {e}")

    root_userpref = Path("/root/.config/blender/5.2/config/userpref.blend")
    user_home = Path("/home/user")
    if root_userpref.is_file() and user_home.is_dir():
        try:
            user_cfg = user_home / ".config/blender/5.2/config"
            user_cfg.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root_userpref, user_cfg / "userpref.blend")
            subprocess.run(["chown", "-R", "user:user", str(user_home / ".config/blender")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("[✓] Synced Blender 5.2 autoexec userpref.blend to /home/user (Cloud Desktop GUI)")
        except Exception as e:
            print(f"[*] Notice: Could not sync userpref to /home/user: {e}")


def _install_addon_file(src_path, target_dirs):
    """Installs a .zip or .py Blender add-on into target addon directories and returns its module name."""
    src = Path(src_path)
    mod_name = None

    if src.suffix.lower() == ".py":
        mod_name = src.stem
        for td in target_dirs:
            td.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, td / src.name)
        return mod_name

    if src.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(src, "r") as zf:
                names = [n for n in zf.namelist() if not n.startswith("__MACOSX") and n.strip("/")]
                top_levels = {n.split("/")[0] for n in names}
                if len(top_levels) == 1:
                    sole = next(iter(top_levels))
                    mod_name = sole[:-3] if sole.endswith(".py") else sole
                else:
                    mod_name = src.stem
                for td in target_dirs:
                    td.mkdir(parents=True, exist_ok=True)
                    zf.extractall(td)
            return mod_name
        except Exception as e:
            print(f"[!] Failed to install add-on zip {src.name}: {e}")
    return None


def install_addons(server_url, token, dash_cfg, project_dir=None):
    """Downloads bundled add-ons from PC receiver and installs add-ons found in project/addons."""
    target_dirs = [Path("/root/.config/blender/5.2/scripts/addons")]
    if Path("/home/user").is_dir():
        target_dirs.append(Path("/home/user/.config/blender/5.2/scripts/addons"))

    addon_sources = []
    staging = Path("/workspace/addons_staging")

    if dash_cfg.get("has_addons"):
        bundle_zip = Path("/workspace/addons_bundle.zip")
        bundle_zip.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        cmd = ["curl", "-sfL", "--connect-timeout", "10", "-m", "120"] + _auth_headers(token) + ["-o", str(bundle_zip), f"{server_url}/download_addons"]
        res = subprocess.run(cmd)
        if res.returncode == 0 and bundle_zip.is_file() and bundle_zip.stat().st_size > 0:
            try:
                with zipfile.ZipFile(bundle_zip, "r") as zf:
                    zf.extractall(staging)
                for item in sorted(staging.iterdir()):
                    if item.suffix.lower() in (".zip", ".py"):
                        addon_sources.append(item)
            except Exception as e:
                print(f"[!] Could not unpack addons bundle: {e}")

    if project_dir:
        proj_addons = Path(project_dir) / "addons"
        if proj_addons.is_dir():
            for item in sorted(proj_addons.iterdir()):
                if item.suffix.lower() in (".zip", ".py"):
                    addon_sources.append(item)

    installed_modules = []
    for item in addon_sources:
        mod = _install_addon_file(item, target_dirs)
        if mod and mod not in installed_modules:
            installed_modules.append(mod)
            print(f"[✓] Installed Blender 5.2 add-on: {mod} (from {item.name})")

    if Path("/home/user/.config/blender").is_dir():
        subprocess.run(["chown", "-R", "user:user", "/home/user/.config/blender"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return installed_modules


def check_receiver_online(server_url):
    """Pings the PC receiver to test connectivity."""
    try:
        res = subprocess.run(
            ["curl", "-s", "--connect-timeout", "4", "-m", "5", f"{server_url}/ping"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return "PONG" in res.stdout
    except Exception:
        return False


def upload_frame_to_pc(file_path, server_url, token, job_id):
    """Uploads a single completed frame to the PC receiver."""
    file_path = Path(file_path)
    try:
        cmd = (
            ["curl", "-s", "-o", "/dev/null", "--connect-timeout", "10", "-m", "120", "-w", "%{http_code}"]
            + _auth_headers(token)
            + ["-H", f"X-Filename: {file_path.name}", "-H", f"X-Job: {job_id}", "--data-binary", f"@{file_path}", f"{server_url}/upload_frame"]
        )
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        code = res.stdout.strip()
        if res.returncode == 0 and code == "200":
            return True, code
        return False, code or str(res.returncode)
    except Exception as e:
        return False, str(e)


def frame_uploader_daemon(output_dir, server_url, token, job_id, job_start_time, render_finished_event):
    """
    Monitors the current job output directory and streams rendered frames to PC in chronological order.
    """
    uploaded = set()
    is_offline = False

    while True:
        eligible_frames = []
        if output_dir.exists():
            for f in sorted(list(output_dir.glob("*.*"))):
                if f.suffix.lower() in [".png", ".jpg", ".jpeg", ".exr", ".mp4", ".mov", ".tga"]:
                    if f.stat().st_mtime >= (job_start_time - 2):
                        eligible_frames.append(f)

        pending = [f for f in eligible_frames if f.name not in uploaded]

        if pending:
            target_frame = pending[0]
            try:
                prev_size = target_frame.stat().st_size
                time.sleep(0.5)
                current_size = target_frame.stat().st_size
            except Exception:
                time.sleep(0.5)
                continue

            if current_size != prev_size or current_size == 0:
                time.sleep(0.5)
                continue

            if is_offline:
                if not check_receiver_online(server_url):
                    time.sleep(3)
                    continue
                print("\n[PC Stream] 🟢 PC connection restored! Resuming frame delivery...")
                is_offline = False

            print(f"[PC Stream] 🚀 Uploading {target_frame.name} ({current_size / 1024:.1f} KB)...")
            success, code = upload_frame_to_pc(target_frame, server_url, token, job_id)
            if success:
                uploaded.add(target_frame.name)
                print(f"[PC Stream] ✓ {target_frame.name} delivered to PC")
            else:
                if not is_offline:
                    print(f"\n[PC Stream] ⚠️ Upload failed (HTTP {code}). PC or internet may be disconnected.")
                    print("[PC Stream] ⏳ Blender will continue rendering safely in background. Pausing upload stream until PC reconnects...")
                    is_offline = True
                time.sleep(3)
                continue
        else:
            if render_finished_event.is_set():
                time.sleep(1)
                all_rendered = [
                    f for f in output_dir.glob("*.*")
                    if f.suffix.lower() in [".png", ".jpg", ".jpeg", ".exr", ".mp4", ".mov", ".tga"]
                    and f.stat().st_mtime >= (job_start_time - 2)
                ]
                still_pending = [f for f in all_rendered if f.name not in uploaded]
                if not still_pending:
                    print(f"\n[PC Stream] 🏁 All {len(uploaded)} rendered frames delivered to PC!")
                    break
            time.sleep(1)


def post_worker_endpoint(server_url, token, path, payload, timeout=5):
    try:
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload)
            hdrs = _auth_headers(token) + ["-H", "Content-Type: application/json"]
        else:
            body = str(payload)
            hdrs = _auth_headers(token)
        cmd = ["curl", "-s", "-m", str(timeout)] + hdrs + ["-d", body, f"{server_url}{path}"]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    except Exception:
        return False


def notify_pc_status(server_url, token, status_msg):
    post_worker_endpoint(server_url, token, "/status", status_msg, timeout=4)


def fetch_job_config(server_url, token):
    try:
        cmd = ["curl", "-s", "-m", "6"] + _auth_headers(token) + [f"{server_url}/api/job_config"]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
        if res.stdout.strip().startswith("{"):
            return json.loads(res.stdout)
    except Exception:
        pass
    return {}


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

    filtered = [p for p in set(all_blends) if "__MACOSX" not in p.parts and not p.name.startswith("._")]
    unique_blends = sorted(filtered, key=lambda p: p.stat().st_mtime, reverse=True)

    if not unique_blends:
        print("[!] No .blend files found in /workspace or home folder.")
        sys.exit(1)

    print(f"\n[+] Found {len(unique_blends)} .blend file(s):")
    for i, b in enumerate(unique_blends[:5]):
        print(f"  [{i+1}] {b} ({b.stat().st_size / (1024*1024):.1f} MB)")

    selected = unique_blends[0]
    print(f"[*] Auto-selecting most recent: {selected.name}")
    return str(selected.resolve())


def select_blend_from_extracted(extract_dir, preferred_name=""):
    candidates = [
        p for p in (list(extract_dir.glob("*.blend")) + list(extract_dir.glob("**/*.blend")))
        if "__MACOSX" not in p.parts and not p.name.startswith("._")
    ]
    unique_candidates = list(dict.fromkeys(p.resolve() for p in candidates))
    if not unique_candidates:
        return None

    if preferred_name:
        pref_norm = preferred_name.replace("\\", "/").strip("/")
        for c in unique_candidates:
            rel = str(c.relative_to(extract_dir.resolve())).replace("\\", "/")
            if rel == pref_norm or c.name == Path(pref_norm).name:
                return str(c)

    unique_candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    return str(unique_candidates[0])


def prepare_project_and_settings(server_url, token, args, dash_cfg):
    """Downloads/unpacks project cleanly, installs add-ons, and writes /workspace/render_job_settings.json."""
    blend_file = None
    project_dir = None
    gdrive_url = (dash_cfg.get("gdrive_url") or "").strip()

    if gdrive_url:
        print("\n" + "=" * 60)
        print(" 📥 MENGUNDUH PROYEK DARI GOOGLE DRIVE (GIGABIT SPEED)...")
        print(f" URL: {gdrive_url}")
        print("=" * 60)
        notify_pc_status(server_url, token, "Downloading project from Google Drive...")

        try:
            import gdown
        except ImportError:
            print("[*] Menginstall utilitas gdown...")
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "gdown"])
            import gdown

        dest_dir = Path("/workspace/gdrive_downloads")
        dest_dir.mkdir(parents=True, exist_ok=True)
        for old in dest_dir.glob("*"):
            if old.is_file():
                old.unlink(missing_ok=True)

        m = re.search(r'/d/([a-zA-Z0-9_-]+)', gdrive_url) or re.search(r'id=([a-zA-Z0-9_-]+)', gdrive_url)
        file_id = m.group(1) if m else None
        direct_url = f"https://drive.google.com/uc?id={file_id}" if file_id else gdrive_url

        downloaded = None
        try:
            downloaded = gdown.download(direct_url, output=str(dest_dir) + "/", quiet=False)
        except Exception as e:
            print(f"[!] Warning gdown: {e}")

        if not downloaded or not Path(downloaded).exists():
            new_f = [p for p in dest_dir.glob("*") if p.is_file()]
            if new_f:
                downloaded = str(new_f[0])

        if not downloaded or not Path(downloaded).exists():
            print("[*] Mencoba download via gdown CLI...")
            subprocess.run(["gdown", gdrive_url, "-O", str(dest_dir) + "/"])
            new_f = [p for p in dest_dir.glob("*") if p.is_file()]
            if new_f:
                downloaded = str(new_f[0])

        if not downloaded or not Path(downloaded).exists():
            print("[*] Mencoba download via gdown CLI --fuzzy...")
            subprocess.run(["gdown", "--fuzzy", gdrive_url, "-O", str(dest_dir) + "/"])
            new_f = [p for p in dest_dir.glob("*") if p.is_file()]
            if new_f:
                downloaded = str(new_f[0])

        if (not downloaded or not Path(downloaded).exists()) and file_id:
            print("[*] Mencoba download via direct curl...")
            curl_target = str(dest_dir / "gdrive_project.zip")
            curl_cmd = (
                f"curl -sSL -c /tmp/gcookies.txt 'https://drive.google.com/uc?export=download&id={file_id}' > /tmp/gresp.html && "
                f"CONFIRM=$(grep -o 'confirm=[a-zA-Z0-9_-]*' /tmp/gresp.html | head -n 1 | cut -d= -f2) && "
                f"curl -sSL -b /tmp/gcookies.txt 'https://drive.google.com/uc?export=download&confirm='\"$CONFIRM\"'&id={file_id}' -o '{curl_target}'"
            )
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
                is_zip = zipfile.is_zipfile(dl_path)
            except Exception:
                pass

        if is_zip:
            extract_dir = Path("/workspace/project")
            shutil.rmtree(extract_dir, ignore_errors=True)
            extract_dir.mkdir(parents=True, exist_ok=True)
            print(f"[📦 Project Unpack] Mengekstrak {dl_path.name} ke {extract_dir}...")
            if subprocess.run(["which", "unzip"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
                subprocess.run("apt-get update -qq && apt-get install -y -qq unzip", shell=True)
            subprocess.run(["unzip", "-o", "-q", str(dl_path), "-d", str(extract_dir)])

            blend_file = select_blend_from_extracted(extract_dir, dash_cfg.get("blend_in_zip", ""))
            if not blend_file:
                print(f"[!] Error: Tidak ditemukan file .blend di dalam {dl_path.name}!")
                sys.exit(1)
            project_dir = extract_dir
            print(f"[✓] Scene .blend ditemukan di dalam zip: {blend_file}")
        else:
            blend_file = str(dl_path.resolve())
            print(f"[✓] Scene .blend siap: {blend_file}")

    elif dash_cfg.get("upload_project"):
        proj_name = Path(dash_cfg.get("project_name", "project.blend")).name
        proj_type = dash_cfg.get("project_type", "blend")
        file_size = dash_cfg.get("file_size", 0)
        size_mb = file_size / (1024 * 1024) if file_size else 0

        dest_file = Path("/workspace") / proj_name
        print("\n" + "=" * 60)
        print(f" 📥 DOWNLOADING PROJECT FROM PC: {proj_name} ({size_mb:.1f} MB)...")
        print("=" * 60)
        notify_pc_status(server_url, token, f"Downloading project {proj_name} from PC...")

        if dest_file.exists() and file_size and dest_file.stat().st_size != file_size:
            dest_file.unlink(missing_ok=True)

        dl_cmd = (
            ["curl", "-fSL", "-C", "-", "--retry", "5", "--retry-delay", "2", "--connect-timeout", "15"]
            + _auth_headers(token)
            + ["-o", str(dest_file), f"{server_url}/download_project"]
        )
        subprocess.run(dl_cmd)

        if not dest_file.exists() or dest_file.stat().st_size == 0:
            print("[!] Download with resume failed. Retrying fresh download...")
            dest_file.unlink(missing_ok=True)
            subprocess.run(["curl", "-fSL"] + _auth_headers(token) + ["-o", str(dest_file), f"{server_url}/download_project"])

        if not dest_file.exists() or dest_file.stat().st_size == 0:
            print("[!] Fatal: Failed to download project file from PC!")
            sys.exit(1)

        print(f"[✓] Project downloaded successfully ({dest_file.stat().st_size / (1024*1024):.1f} MB)!")

        if proj_type == "zip" or dest_file.suffix.lower() == ".zip":
            extract_dir = Path("/workspace/project")
            shutil.rmtree(extract_dir, ignore_errors=True)
            extract_dir.mkdir(parents=True, exist_ok=True)
            print(f"[📦 Project Unpack] Unpacking {dest_file.name} to {extract_dir}...")
            if subprocess.run(["which", "unzip"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
                print("[*] Installing unzip...")
                subprocess.run("apt-get update -qq && apt-get install -y -qq unzip", shell=True)
            subprocess.run(["unzip", "-o", "-q", str(dest_file), "-d", str(extract_dir)])

            blend_file = select_blend_from_extracted(extract_dir, dash_cfg.get("blend_in_zip", ""))
            if not blend_file:
                print(f"[!] Error: No .blend files found inside {dest_file.name}!")
                sys.exit(1)
            project_dir = extract_dir
            print(f"[✓] Scene .blend found inside zip: {blend_file}")
        else:
            blend_file = str(dest_file.resolve())
            print(f"[✓] Scene .blend ready: {blend_file}")
    else:
        explicit = args.blend or dash_cfg.get("vast_blend_path")
        blend_file = find_blend_file(explicit)
        print(f"[✓] Scene: {blend_file}")

    addon_modules = install_addons(server_url, token, dash_cfg, project_dir=project_dir)
    dash_cfg["addon_modules"] = addon_modules
    try:
        Path("/workspace/render_job_settings.json").write_text(json.dumps(dash_cfg), encoding="utf-8")
    except Exception:
        pass

    return blend_file, addon_modules


def run_single_render_job(blend_file, frame_args, server_url, token, blender_bin, gpu_script, addon_modules=None, is_daemon=False):
    clean_stem = re.sub(r'[^\w\-\.]', '_', Path(blend_file).stem)
    job_id = f"{clean_stem}_{time.strftime('%Y%m%d_%H%M%S')}"
    job_start_time = time.time()

    output_dir = Path("/workspace/render_output") / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    render_finished_event = threading.Event()
    uploader_thread = threading.Thread(
        target=frame_uploader_daemon,
        args=(output_dir, server_url, token, job_id, job_start_time, render_finished_event),
        daemon=True,
    )
    uploader_thread.start()

    print("\n" + "=" * 60)
    print(" 🔥 RENDERING IN PROGRESS (BLENDER 5.2 LTS / GPU)...")
    print(f" Job ID: {job_id}")
    print(f" Output Folder: {output_dir}")
    print("=" * 60)
    notify_pc_status(server_url, token, f"Rendering job {job_id} started")

    render_cmd = [blender_bin, "-y", "-b", blend_file]
    if addon_modules:
        render_cmd.extend(["--addons", ",".join(addon_modules)])
    render_cmd.extend([
        "-P", str(gpu_script.resolve()),
        "-o", f"{str(output_dir)}/frame_#####",
    ])
    render_cmd.extend(frame_args)

    print("Executing:", " ".join(render_cmd))
    returncode = -1
    log_buf = []
    last_flush = time.time()

    def _flush_logs(force=False):
        nonlocal last_flush
        if not log_buf:
            return
        if force or len(log_buf) >= 15 or (time.time() - last_flush) >= 1.0:
            chunk = "\n".join(log_buf)
            log_buf.clear()
            last_flush = time.time()
            post_worker_endpoint(server_url, token, "/log", {"job_id": job_id, "text": chunk}, timeout=4)

    try:
        proc = subprocess.Popen(
            render_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            errors="replace",
        )
        for line in iter(proc.stdout.readline, ""):
            clean = line.rstrip("\r\n")
            print(clean, flush=True)
            log_buf.append(clean)
            important = any(k in clean for k in ("[Blend Scripts]", "[Blender Setup]", "Saved:", "Fra:", "Error", "Traceback"))
            _flush_logs(force=important)
        proc.stdout.close()
        returncode = proc.wait()
        _flush_logs(force=True)

        if returncode != 0:
            print(f"\n[!] Blender process exited with code {returncode}")
        else:
            print("\n[✓] Blender finished rendering all frames!")
    except KeyboardInterrupt:
        print("\n[!] Render interrupted by user.")
    finally:
        _flush_logs(force=True)
        render_finished_event.set()
        print("[*] Waiting for all rendered frames to be safely delivered to PC...")
        uploader_thread.join()

        print("[*] Notifying PC receiver that render job has finished...")
        finish_payload = {
            "job_id": job_id,
            "success": (returncode == 0),
            "returncode": returncode,
            "is_daemon": bool(is_daemon),
        }
        for _attempt in range(20):
            if post_worker_endpoint(server_url, token, "/finish_job", finish_payload, timeout=5):
                break
            time.sleep(3)

        status_label = "COMPLETED SUCCESSFULLY" if returncode == 0 else f"ENDED WITH CODE {returncode}"
        print("\n" + "=" * 60)
        print(f" [{'✓' if returncode == 0 else '!'}] JOB {job_id} {status_label}.")
        print("=" * 60)


def build_frame_args(args, cfg):
    start_val = args.start if args.start is not None else cfg.get("start_frame")
    end_val = args.end if args.end is not None else cfg.get("end_frame")
    single_val = args.single if args.single is not None else cfg.get("single_frame")
    mode = cfg.get("render_mode", "")

    if (mode == "single" or not mode) and single_val is not None and str(single_val).strip() != "":
        return ["-f", str(single_val)], f"Single Frame: {single_val}"
    if (mode == "range" or not mode) and start_val is not None and end_val is not None and str(start_val).strip() != "" and str(end_val).strip() != "":
        return ["-s", str(start_val), "-e", str(end_val), "-a"], f"Frame Range: {start_val} to {end_val}"
    return ["-a"], "Full Animation (-a)"


def main():
    parser = argparse.ArgumentParser(description="Vast.ai Blender 5.2 LTS Render Worker")
    parser.add_argument("--blend", help="Path to .blend file")
    parser.add_argument("--server", default="http://localhost:8888", help="PC receiver URL (default: http://localhost:8888)")
    parser.add_argument("--token", default="", help="Per-session worker authentication token")
    parser.add_argument("--start", type=int, help="Start frame")
    parser.add_argument("--end", type=int, help="End frame")
    parser.add_argument("--single", type=int, help="Single frame number")
    parser.add_argument("--daemon", action="store_true", help="Stay running as daemon and listen for render commands from PC")
    args = parser.parse_args()

    server_url = args.server.rstrip("/")
    token = args.token.strip()
    print("=" * 60)
    print(" 🚀 VAST.AI BLENDER 5.2 LTS RENDER WORKER")
    print(f" Target PC Receiver: {server_url}")
    print("=" * 60)

    print(f"[*] Testing connection to PC receiver at {server_url}...")
    if not check_receiver_online(server_url):
        print(f"[!] Warning: Could not reach PC receiver at {server_url}.")
        print("    Check that VastRenderHub / receiver.py is running on your PC.")
    else:
        print("[✓] Connected to PC receiver!")

    blender_bin = find_blender()
    print(f"[✓] Using Blender: {blender_bin}")
    ensure_blender_autoexec_config(blender_bin)

    dash_cfg = fetch_job_config(server_url, token)
    if dash_cfg:
        print(f"[+] Loaded Configuration from PC: {dash_cfg}")

    blend_file, addon_modules = prepare_project_and_settings(server_url, token, args, dash_cfg)
    last_project_sig = (
        dash_cfg.get("project_revision"),
        dash_cfg.get("gdrive_url"),
        dash_cfg.get("project_name"),
        dash_cfg.get("has_addons"),
    )

    gpu_script = Path(__file__).parent / "enable_gpu.py"
    if not gpu_script.exists():
        gpu_script = Path("/workspace/enable_gpu.py")

    frame_args, mode_desc = build_frame_args(args, dash_cfg)
    print(f"[✓] Configured for {mode_desc}")

    if args.daemon:
        print("\n" + "=" * 60)
        print(" 🛰 DAEMON MODE ACTIVE: Listening for jobs from PC dashboard...")
        print("=" * 60)
        notify_pc_status(server_url, token, "Daemon worker active and listening")
        run_single_render_job(blend_file, frame_args, server_url, token, blender_bin, gpu_script, addon_modules=addon_modules, is_daemon=True)

        while True:
            time.sleep(3)
            poll_cmd = ["curl", "-s", "-m", "5"] + _auth_headers(token) + [f"{server_url}/api/daemon_poll"]
            poll_res = subprocess.run(poll_cmd, stdout=subprocess.PIPE, text=True)
            if poll_res.stdout.strip().startswith("{"):
                try:
                    job_req = json.loads(poll_res.stdout)
                    if job_req.get("active"):
                        print(f"\n[+] Received New Job from Dashboard: {job_req}")
                        new_sig = (
                            job_req.get("project_revision"),
                            job_req.get("gdrive_url"),
                            job_req.get("project_name"),
                            job_req.get("has_addons"),
                        )
                        if new_sig != last_project_sig:
                            blend_file, addon_modules = prepare_project_and_settings(server_url, token, args, job_req)
                            last_project_sig = new_sig
                        else:
                            job_req["addon_modules"] = addon_modules
                            Path("/workspace/render_job_settings.json").write_text(json.dumps(job_req), encoding="utf-8")

                        new_f_args, _ = build_frame_args(args, job_req)
                        run_single_render_job(blend_file, new_f_args, server_url, token, blender_bin, gpu_script, addon_modules=addon_modules, is_daemon=True)
                except Exception as e:
                    print(f"[!] Daemon poll error: {e}")
    else:
        run_single_render_job(blend_file, frame_args, server_url, token, blender_bin, gpu_script, addon_modules=addon_modules, is_daemon=False)


if __name__ == "__main__":
    main()

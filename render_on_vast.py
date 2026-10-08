#!/usr/bin/env python3
"""
Vast.ai <-> PC Fast Blender Render Pipeline
Run this on your PC to automatically push .blend, render with GPU on Vast.ai, and pull results.
"""

import os
import sys
import re
import argparse
import subprocess
import shutil
from pathlib import Path

GPU_SETUP_SCRIPT = """
import bpy
try:
    bpy.context.scene.render.engine = 'CYCLES'
    bpy.context.preferences.filepaths.use_scripts_auto_execute = True
    bpy.context.scene.render.use_persistent_data = True
    cprefs = bpy.context.preferences.addons['cycles'].preferences
    # Try OptiX first, fallback to CUDA
    for dev_type in ('OPTIX', 'CUDA'):
        try:
            cprefs.compute_device_type = dev_type
            cprefs.get_devices()
            activated = False
            for dev in cprefs.devices:
                if dev.type == dev_type:
                    dev.use = True
                    activated = True
                    print(f"[GPU] Activated {dev.name} ({dev_type})")
                else:
                    dev.use = False
            if activated:
                bpy.context.scene.cycles.device = 'GPU'
                print(f"[GPU] Cycles device set to GPU ({dev_type})")
                break
        except Exception as err:
            pass
except Exception as e:
    print(f"[Warning] Failed to set GPU devices: {e}")
"""

def parse_ssh_string(ssh_str):
    """
    Parses SSH strings like:
    ssh -p 32541 root@123.45.67.89 -L 8080:localhost:8080
    or 'root@123.45.67.89 -p 32541'
    """
    ssh_str = ssh_str.strip()
    # Remove leading 'ssh ' if present
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

    # Remove port forwards like -L ...
    ssh_str = re.sub(r'-[LDR]\s+[^\s]+', '', ssh_str)

    # Remaining token is user@host or host
    tokens = [t.strip() for t in ssh_str.split() if t.strip() and not t.startswith('-')]
    target = tokens[0] if tokens else "root@localhost"

    return target, port, identity

def run_ssh(target, port, identity, cmd, capture=False):
    ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-p", str(port)]
    if identity:
        ssh_cmd.extend(["-i", identity])
    ssh_cmd.extend([target, cmd])
    
    if capture:
        res = subprocess.run(ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return res.returncode, res.stdout, res.stderr
    else:
        return subprocess.run(ssh_cmd).returncode

def run_scp_upload(local_path, remote_path, target, port, identity):
    scp_cmd = ["scp", "-o", "StrictHostKeyChecking=no", "-P", str(port)]
    if identity:
        scp_cmd.extend(["-i", identity])
    scp_cmd.extend([local_path, f"{target}:{remote_path}"])
    return subprocess.run(scp_cmd).returncode

def run_scp_download(remote_path, local_path, target, port, identity):
    scp_cmd = ["scp", "-o", "StrictHostKeyChecking=no", "-r", "-P", str(port)]
    if identity:
        scp_cmd.extend(["-i", identity])
    scp_cmd.extend([f"{target}:{remote_path}", local_path])
    return subprocess.run(scp_cmd).returncode

def main():
    print("=" * 60)
    print(" 🚀 VAST.AI FAST BLENDER ACCELERATED RENDERER")
    print("=" * 60)

    # Load cached config if exists
    cache_file = Path(__file__).parent / ".last_vast_ssh.txt"
    cached_ssh = ""
    if cache_file.exists():
        cached_ssh = cache_file.read_text().strip()

    parser = argparse.ArgumentParser(description="Render Blender files on Vast.ai with GPU acceleration")
    parser.add_argument("--ssh", help="Vast.ai SSH command (e.g. 'ssh -p 12345 root@1.2.3.4')")
    parser.add_argument("--blend", help="Path to .blend file")
    parser.add_argument("--start", type=int, help="Start frame (optional)")
    parser.add_argument("--end", type=int, help="End frame (optional)")
    parser.add_argument("--single", type=int, help="Render single frame (e.g. 1)")
    args = parser.parse_args()

    ssh_raw = args.ssh
    if not ssh_raw:
        prompt_txt = f"Paste your Vast.ai SSH command [{cached_ssh}]: " if cached_ssh else "Paste your Vast.ai SSH command (from Vast.ai dashboard): "
        ssh_input = input(prompt_txt).strip()
        ssh_raw = ssh_input if ssh_input else cached_ssh

    if not ssh_raw:
        print("[!] Error: No SSH command provided.")
        sys.exit(1)

    # Save SSH for quick reuse
    cache_file.write_text(ssh_raw)

    target, port, identity = parse_ssh_string(ssh_raw)
    print(f"\n[+] Connecting to Vast.ai instance: {target} on port {port}...")

    # Test SSH
    code, stdout, _ = run_ssh(target, port, identity, "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader", capture=True)
    if code != 0:
        print(f"[!] SSH Connection failed. Output: {stdout}")
        print("Please check your SSH command and keys.")
        sys.exit(1)
    
    gpu_name = stdout.strip() or "NVIDIA GPU Detected"
    print(f"[✓] Connected successfully! GPU detected: {gpu_name}")

    # Blender File selection
    blend_path_str = args.blend
    if not blend_path_str:
        blend_path_str = input("\nDrag & drop or enter path to your .blend file: ").strip().strip('"').strip("'")
    
    blend_path = Path(blend_path_str)
    if not blend_path.exists():
        print(f"[!] Error: File not found: {blend_path}")
        sys.exit(1)

    print(f"[+] Selected: {blend_path.name} ({blend_path.stat().st_size / (1024*1024):.1f} MB)")

    # Prepare remote directories and check Blender
    print("\n[+] Checking Blender installation on Vast.ai...")
    check_blender_cmd = "which blender || [ -f /workspace/blender/blender ] && echo 'FOUND' || echo 'NOT_FOUND'"
    _, out, _ = run_ssh(target, port, identity, check_blender_cmd, capture=True)

    remote_blender = "blender"
    if "NOT_FOUND" in out or not out.strip():
        print("[*] Blender not found in instance. Downloading fast portable Blender 4.2 LTS on Vast.ai...")
        install_script = """
        mkdir -p /workspace && cd /workspace && \
        if [ ! -f /workspace/blender/blender ]; then
            echo 'Downloading Blender 4.2...' && \
            wget -q --show-progress https://download.blender.org/release/Blender4.2/blender-4.2.3-linux-x64.tar.xz -O blender.tar.xz && \
            tar -xf blender.tar.xz && \
            mv blender-4.2.* blender && \
            rm blender.tar.xz
        fi
        """
        run_ssh(target, port, identity, install_script)
        remote_blender = "/workspace/blender/blender"
    elif "/workspace/blender/blender" in out or "FOUND" in out:
        remote_blender = "/workspace/blender/blender"
    
    # Upload GPU hook script & blend file
    print(f"\n[+] Uploading {blend_path.name} to Vast.ai...")
    run_ssh(target, port, identity, "mkdir -p /workspace/render_job/output", capture=True)

    # Write gpu script on remote
    gpu_setup_escaped = GPU_SETUP_SCRIPT.replace('"', '\\"').replace('$', '\\$')
    write_gpu_cmd = f'cat << \'EOF\' > /workspace/render_job/gpu_setup.py\n{GPU_SETUP_SCRIPT}\nEOF'
    run_ssh(target, port, identity, write_gpu_cmd)

    # Upload blend file
    res = run_scp_upload(str(blend_path), "/workspace/render_job/scene.blend", target, port, identity)
    if res != 0:
        print("[!] Upload failed.")
        sys.exit(1)
    print("[✓] Blend file uploaded!")

    # Frame range options
    frame_args = ""
    if args.single is not None:
        frame_args = f"-f {args.single}"
    elif args.start is not None and args.end is not None:
        frame_args = f"-s {args.start} -e {args.end} -a"
    else:
        render_mode = input("\nRender mode: [A]ll frames (animation), [S]ingle frame, or [R]ange (default: A): ").strip().upper()
        if render_mode == 'S':
            f_num = input("Enter frame number to render [default: 1]: ").strip() or "1"
            frame_args = f"-f {f_num}"
        elif render_mode == 'R':
            s_num = input("Start frame: ").strip() or "1"
            e_num = input("End frame: ").strip() or "250"
            frame_args = f"-s {s_num} -e {e_num} -a"
        else:
            frame_args = "-a"

    # Start rendering
    print("\n" + "=" * 60)
    print(" 🔥 RENDERING ON VAST.AI GPU...")
    print("=" * 60)
    
    render_cmd = (
        f"{remote_blender} -y -b /workspace/render_job/scene.blend "
        f"-P /workspace/render_job/gpu_setup.py "
        f"-o /workspace/render_job/output/frame_##### "
        f"{frame_args}"
    )

    ret = run_ssh(target, port, identity, render_cmd)
    if ret != 0:
        print("[!] Render encountered an error or was stopped.")

    # Download results
    local_out_dir = Path(__file__).parent / "renders" / blend_path.stem
    local_out_dir.mkdir(parents=True, exist_ok=True)
    
    print("\n[+] Downloading rendered results to PC...")
    dl_res = run_scp_download("/workspace/render_job/output/*", str(local_out_dir), target, port, identity)
    
    if dl_res == 0:
        print(f"\n[✓] SUCCESS! Rendered files downloaded to:\n    {local_out_dir.resolve()}")
        # Open folder in Windows Explorer
        os.system(f'explorer "{local_out_dir.resolve()}"')
    else:
        print("[!] Failed to download output folder.")

if __name__ == "__main__":
    main()

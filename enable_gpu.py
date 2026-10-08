import bpy
import sys
import os
import json
from pathlib import Path

def setup_blender_optimizations():
    try:
        scene = bpy.context.scene
        scene.render.engine = 'CYCLES'

        # Check for custom job settings passed from dashboard
        settings_file = Path("/workspace/render_job_settings.json")
        job_settings = {}
        if settings_file.exists():
            try:
                job_settings = json.loads(settings_file.read_text())
            except Exception:
                pass

        # 1. Enable Auto-Run Python Scripts (drivers, rigged characters, procedural scripts)
        try:
            bpy.context.preferences.filepaths.use_scripts_auto_execute = True
            print("[Blender Setup] ✓ Automatically run Python scripts enabled.")
        except Exception as e:
            print(f"[Blender Setup] Notice: Could not set use_scripts_auto_execute: {e}")

        # 2. Enable Persistent Data (keeps BVH/textures/geometry in memory between frames)
        try:
            scene.render.use_persistent_data = True
            print("[Blender Setup] ✓ Persistent Data enabled (drastically accelerates multi-frame renders).")
        except Exception as e:
            print(f"[Blender Setup] Notice: Could not set use_persistent_data: {e}")

        # 3. Custom Samples & Denoising from Dashboard
        if "samples" in job_settings and job_settings["samples"]:
            try:
                samples_val = int(job_settings["samples"])
                scene.cycles.samples = samples_val
                print(f"[Blender Setup] ✓ Cycles Samples set to: {samples_val}")
            except Exception as e:
                print(f"[Blender Setup] Could not set samples: {e}")

        if job_settings.get("denoise", True):
            optix_weights = Path("/usr/share/nvidia/nvoptix.bin")
            has_optix = optix_weights.is_file()

            if not has_optix:
                try:
                    for alt_path in Path("/usr").glob("**/nvoptix.bin"):
                        if alt_path.is_file():
                            optix_weights.parent.mkdir(parents=True, exist_ok=True)
                            optix_weights.symlink_to(alt_path)
                            has_optix = True
                            print(f"[Blender Setup] Linked OptiX weights from {alt_path}")
                            break
                except Exception:
                    pass

            try:
                scene.cycles.use_denoising = True
                if has_optix:
                    scene.cycles.denoiser = 'OPTIX'
                    print("[Blender Setup] ✓ OptiX AI Denoising enabled.")
                else:
                    scene.cycles.denoiser = 'OPENIMAGEDENOISE'
                    print("[Blender Setup] ✓ OpenImageDenoise (OIDN) enabled (nvoptix.bin tidak ditemukan di container, menggunakan OIDN agar render lancar tanpa error).")
            except Exception as e:
                print(f"[Blender Setup] Notice setting denoiser: {e}")

        # 4. GPU Hardware Acceleration (OptiX / CUDA)
        cprefs = bpy.context.preferences.addons['cycles'].preferences
        print("[Blender Setup] Configuring GPU acceleration...")
        
        # Priority order: OPTIX (fastest on RTX), CUDA
        activated_devices = []
        for dev_type in ('OPTIX', 'CUDA'):
            try:
                cprefs.compute_device_type = dev_type
                cprefs.get_devices()
                found = False
                for device in cprefs.devices:
                    if device.type == dev_type:
                        device.use = True
                        found = True
                        activated_devices.append(f"{device.name} ({dev_type})")
                    else:
                        device.use = False
                
                if found:
                    scene.cycles.device = 'GPU'
                    print(f"[Blender Setup] ✓ Enabled compute type: {dev_type}")
                    for d in activated_devices:
                        print(f"  -> {d}")
                    return True
            except Exception as e:
                print(f"[Blender Setup] Could not configure {dev_type}: {e}")

        print("[Blender Setup] No compatible GPU device found; falling back to CPU.")
        scene.cycles.device = 'CPU'
        return False
    except Exception as e:
        print(f"[Blender Setup] Error during Blender setup: {e}")
        return False

setup_blender_optimizations()

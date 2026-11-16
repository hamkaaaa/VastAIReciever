import bpy
import sys
import os
import json
from pathlib import Path

ANIMATABLE_COLLECTIONS = (
    "objects", "cameras", "meshes", "materials", "node_groups", "worlds",
    "scenes", "lights", "shape_keys", "armatures", "curves", "textures",
    "particles", "grease_pencils", "lattices", "metaballs", "speakers",
    "volumes", "pointclouds", "hair_curves", "linestyles", "masks",
    "movieclips",
)


def _iter_drivers():
    for coll_name in ANIMATABLE_COLLECTIONS:
        coll = getattr(bpy.data, coll_name, None)
        if coll is None:
            continue
        for id_block in coll:
            ad = getattr(id_block, "animation_data", None)
            if ad is None:
                continue
            for fc in ad.drivers:
                yield id_block, fc


def execute_blend_text_scripts(run_all=True):
    """
    Force-run the Python scripts embedded in the .blend (bpy.data.texts).
    -y should already run 'Register' texts on load, but some setups still skip them
    (e.g. Shakify's 'Prep For Render Farm' script). We run them again here, then
    reset every driver flagged invalid so it is evaluated again with the new properties.
    """
    print(f"[Blend Scripts] autoexec_fail={getattr(bpy.app, 'autoexec_fail', '?')} "
          f"(True means Blender skipped auto-run on load)")
    print(f"[Blend Scripts] Embedded text blocks: {[t.name for t in bpy.data.texts]}")

    for text in list(bpy.data.texts):
        is_registered = bool(getattr(text, "use_module", False))
        is_python = text.name.lower().endswith(".py") or is_registered
        if not is_python:
            print(f"[Blend Scripts] - skip '{text.name}' (not a .py / not registered)")
            continue
        if not is_registered and not run_all:
            print(f"[Blend Scripts] - skip '{text.name}' (not registered)")
            continue
        try:
            code = compile(text.as_string(), text.name, "exec")
            # Same namespace as pressing "Run Script" in the Text Editor
            exec(code, {"__name__": "__main__", "__file__": text.name, "bpy": bpy})
            print(f"[Blend Scripts] ✓ Executed '{text.name}' (registered={is_registered})")
        except Exception as e:
            print(f"[Blend Scripts] ! Error running '{text.name}': {type(e).__name__}: {e}")

    # Drivers that failed at load time (properties did not exist yet) get flagged
    # invalid and Blender stops evaluating them. Reset them so they are evaluated again.
    reset = 0
    for _id, fc in _iter_drivers():
        try:
            if not fc.driver.is_valid:
                fc.driver.is_valid = True
                reset += 1
            fc.driver.expression = fc.driver.expression  # tag for re-evaluation
        except Exception:
            pass

    try:
        for obj in bpy.data.objects:
            obj.update_tag(refresh={'OBJECT', 'DATA', 'TIME'})
            if obj.animation_data:
                obj.animation_data.drivers.update()
        scene = bpy.context.scene
        scene.frame_set(scene.frame_current)
        bpy.context.view_layer.update()
    except Exception as e:
        print(f"[Blend Scripts] Notice: depsgraph refresh failed: {e}")

    broken = [(i.name, fc.data_path) for i, fc in _iter_drivers() if not fc.driver.is_valid]
    total = sum(1 for _ in _iter_drivers())
    print(f"[Blend Scripts] Drivers: {total} total, {reset} re-enabled, {len(broken)} still broken")
    for name, path in broken[:30]:
        print(f"[Blend Scripts]   ✗ {name}: {path}")


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

        # 1. Enable Auto-Run Python Scripts & Save Preferences (so GUI / Cloud Desktop never prompts)
        try:
            bpy.context.preferences.filepaths.use_scripts_auto_execute = True
            bpy.ops.wm.save_userpref()
            print("[Blender Setup] ✓ Automatically run Python scripts enabled and saved to userpref.blend.")
        except Exception as e:
            print(f"[Blender Setup] Notice: Could not set/save use_scripts_auto_execute: {e}")

        # 1b. Force-run every script embedded in the .blend (Shakify render-farm script, rig UIs, etc.)
        try:
            execute_blend_text_scripts(run_all=job_settings.get("exec_all_scripts", True))
        except Exception as e:
            print(f"[Blend Scripts] Error: {e}")

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

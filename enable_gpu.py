import bpy
import sys
import os
import json
import traceback
from pathlib import Path

ANIMATABLE_COLLECTIONS = (
    "objects", "cameras", "meshes", "materials", "node_groups", "worlds",
    "scenes", "lights", "shape_keys", "armatures", "curves", "textures",
    "particles", "grease_pencils", "lattices", "metaballs", "speakers",
    "volumes", "pointclouds", "hair_curves", "linestyles", "masks",
    "movieclips", "actions",
)

NODE_TREE_COLLECTIONS = ("materials", "worlds", "scenes", "lights", "linestyles")


def _iter_all_drivers():
    """Yield (id_block, fcurve) across all datablocks including material/world/scene node_trees."""
    for coll_name in ANIMATABLE_COLLECTIONS:
        coll = getattr(bpy.data, coll_name, None)
        if coll is None:
            continue
        for id_block in coll:
            ad = getattr(id_block, "animation_data", None)
            if ad is not None:
                for fc in ad.drivers:
                    yield id_block, fc
            if coll_name in NODE_TREE_COLLECTIONS:
                nt = getattr(id_block, "node_tree", None)
                if nt is not None:
                    nt_ad = getattr(nt, "animation_data", None)
                    if nt_ad is not None:
                        for fc in nt_ad.drivers:
                            yield nt, fc


def ensure_camera_shakify_shim():
    """
    If drivers reference Camera Shakify's `camera_shakes[...]` property and the add-on
    (or its embedded farm script) isn't registered yet, register the minimal PropertyGroup
    so shake drivers evaluate cleanly on render nodes.
    """
    if hasattr(bpy.types.Object, "camera_shakes"):
        return False

    needs_shakify = False
    for _id_block, fc in _iter_all_drivers():
        drv = getattr(fc, "driver", None)
        if drv is None:
            continue
        for var in drv.variables:
            for tgt in var.targets:
                if "camera_shakes[" in getattr(tgt, "data_path", ""):
                    needs_shakify = True
                    break
            if needs_shakify:
                break
        if needs_shakify:
            break

    if not needs_shakify:
        return False

    try:
        class CameraShakeInstance(bpy.types.PropertyGroup):
            influence: bpy.props.FloatProperty(name="Influence", default=1.0, min=0.0, soft_min=0.0, soft_max=1.0)
            scale: bpy.props.FloatProperty(name="Scale", default=1.0, min=0.0, soft_min=0.0, soft_max=2.0)
            use_manual_timing: bpy.props.BoolProperty(name="Manual Timing", default=False)
            time: bpy.props.FloatProperty(name="Time", default=0.0)
            speed: bpy.props.FloatProperty(name="Speed", default=1.0, soft_min=0.0, soft_max=4.0)
            offset: bpy.props.FloatProperty(name="Frame Offset", default=0.0)

        bpy.utils.register_class(CameraShakeInstance)
        bpy.types.Object.camera_shakes = bpy.props.CollectionProperty(type=CameraShakeInstance)
        print("[Blend Scripts] ✓ Auto-registered CameraShakify fallback shim (camera_shakes)")
        return True
    except Exception as e:
        print(f"[Blend Scripts] Notice: Could not register CameraShakify fallback shim: {e}")
        return False


def enable_installed_addons(addon_modules):
    """Enable bundled/installed Blender add-on modules if provided."""
    if not addon_modules:
        return
    try:
        import addon_utils
        bpy.utils.refresh_script_paths()
    except Exception as e:
        print(f"[Blender Setup] Notice: addon_utils unavailable: {e}")
        return

    for mod in addon_modules:
        mod_name = str(mod).strip()
        if not mod_name:
            continue
        try:
            addon_utils.enable(mod_name, default_set=True, persistent=True)
            print(f"[Blender Setup] ✓ Enabled add-on: {mod_name}")
        except Exception as e:
            print(f"[Blender Setup] ! Failed to enable add-on '{mod_name}': {e}")


def execute_blend_text_scripts(run_all=True, force_rerun_registered=False):
    """
    Execute Python text blocks embedded in the .blend (bpy.data.texts).
    Avoids double-running `use_module=True` scripts when Blender (-y) already ran them on load,
    while always executing non-registered Python scripts when `run_all=True`.
    """
    autoexec_failed = bool(getattr(bpy.app, "autoexec_fail", False))
    autoexec_msg = getattr(bpy.app, "autoexec_fail_message", "")
    texts_summary = [(t.name, bool(getattr(t, "use_module", False))) for t in bpy.data.texts]

    print(f"[Blend Scripts] Blender {bpy.app.version_string} | autoexec_fail={autoexec_failed}"
          + (f" ({autoexec_msg})" if autoexec_msg else ""))
    print(f"[Blend Scripts] Embedded text blocks: {texts_summary}")

    for text in list(bpy.data.texts):
        is_registered = bool(getattr(text, "use_module", False))
        src = text.as_string()
        lower_name = text.name.lower()
        is_non_script_ext = lower_name.endswith((".txt", ".md", ".json", ".xml", ".csv", ".log"))
        is_python = is_registered or lower_name.endswith(".py") or (
            not is_non_script_ext and ("import bpy" in src or "bpy." in src)
        )

        if not is_python:
            print(f"[Blend Scripts] - skip '{text.name}' (not a Python script)")
            continue

        if is_registered and not autoexec_failed and not force_rerun_registered:
            print(f"[Blend Scripts] ✓ '{text.name}' already auto-executed by Blender (-y); skipping duplicate run.")
            continue

        if not is_registered and not run_all:
            print(f"[Blend Scripts] - skip '{text.name}' (not marked Register, exec_all_scripts=False)")
            continue

        try:
            code = compile(src, text.name, "exec")
            file_ref = f"{bpy.data.filepath}/{text.name}" if bpy.data.filepath else text.name
            ns = {"__name__": "__main__", "__file__": file_ref, "bpy": bpy}
            exec(code, ns)
            if "__main__" not in src and callable(ns.get("register")):
                try:
                    ns["register"]()
                    print(f"[Blend Scripts] ✓ Called register() for '{text.name}'")
                except Exception as reg_err:
                    print(f"[Blend Scripts] ! Error in register() of '{text.name}': {type(reg_err).__name__}: {reg_err}")
                    traceback.print_exc()
            print(f"[Blend Scripts] ✓ Executed '{text.name}' (registered={is_registered})")
        except Exception as e:
            print(f"[Blend Scripts] ! Error running '{text.name}': {type(e).__name__}: {e}")
            traceback.print_exc()


def refresh_drivers_and_depsgraph():
    """
    Re-enable any drivers marked invalid at load time (e.g., before scripts/shims registered),
    tag datablocks dirty, update the evaluated depsgraph, and step frame to force re-evaluation.
    """
    ensure_camera_shakify_shim()

    reset_count = 0
    total_count = 0
    touched_blocks = set()

    for id_block, fc in _iter_all_drivers():
        total_count += 1
        try:
            if not fc.driver.is_valid:
                fc.driver.is_valid = True
                reset_count += 1
            fc.driver.expression = fc.driver.expression
            touched_blocks.add(id_block)
        except Exception:
            pass

    for id_block in touched_blocks:
        try:
            id_block.update_tag()
        except Exception:
            pass

    for obj in bpy.data.objects:
        try:
            obj.update_tag(refresh={'OBJECT', 'DATA', 'TIME'})
        except Exception:
            pass

    try:
        dg = bpy.context.evaluated_depsgraph_get()
        dg.update()
        scene = bpy.context.scene
        cur = scene.frame_current
        scene.frame_set(cur + 1)
        scene.frame_set(cur)
        bpy.context.view_layer.update()
        dg.update()
    except Exception as e:
        print(f"[Blend Scripts] Notice: depsgraph refresh failed: {e}")

    broken = [(getattr(i, "name", repr(i)), fc.data_path) for i, fc in _iter_all_drivers() if not fc.driver.is_valid]
    print(f"[Blend Scripts] Drivers: {total_count} total, {reset_count} re-enabled, {len(broken)} still broken")
    for name, path in broken[:30]:
        print(f"[Blend Scripts]   ✗ {name}: {path}")


def setup_blender_optimizations():
    try:
        scene = bpy.context.scene

        # Load job settings from env override or default Vast path
        settings_path = os.environ.get("VAST_JOB_SETTINGS") or "/workspace/render_job_settings.json"
        settings_file = Path(settings_path)
        job_settings = {}
        if settings_file.exists():
            try:
                job_settings = json.loads(settings_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        # 1. Enable Auto-Run Python Scripts (and optionally persist userpref.blend)
        try:
            bpy.context.preferences.filepaths.use_scripts_auto_execute = True
            if os.environ.get("VAST_SKIP_SAVE_PREFS") != "1":
                bpy.ops.wm.save_userpref()
                print("[Blender Setup] ✓ Automatically run Python scripts enabled and saved to userpref.blend.")
            else:
                print("[Blender Setup] ✓ Automatically run Python scripts enabled (save_userpref skipped).")
        except Exception as e:
            print(f"[Blender Setup] Notice: Could not set/save use_scripts_auto_execute: {e}")

        # 1b. Enable bundled add-ons if configured
        enable_installed_addons(job_settings.get("addon_modules") or [])

        # 1c. Execute embedded .blend scripts & refresh drivers/depsgraph
        try:
            execute_blend_text_scripts(
                run_all=bool(job_settings.get("exec_all_scripts", True)),
                force_rerun_registered=bool(job_settings.get("force_rerun_registered", False)),
            )
            refresh_drivers_and_depsgraph()
        except Exception as e:
            print(f"[Blend Scripts] Error: {e}")
            traceback.print_exc()

        # 2. Render Engine (Cycles by default unless force_cycles=False)
        if job_settings.get("force_cycles", True):
            scene.render.engine = 'CYCLES'

        # 3. Persistent Data (keeps BVH/textures/geometry in memory between frames)
        try:
            pers_val = bool(job_settings.get("persistent_data", True))
            scene.render.use_persistent_data = pers_val
            print(f"[Blender Setup] ✓ Persistent Data: {'ON' if pers_val else 'OFF'}")
        except Exception as e:
            print(f"[Blender Setup] Notice: Could not set use_persistent_data: {e}")

        # 3b. Simplify (Max Subdivision Render)
        if job_settings.get("use_simplify", False):
            try:
                subdiv_val = int(job_settings.get("simplify_subdiv", 2))
                scene.render.use_simplify = True
                scene.render.simplify_subdivision_render = subdiv_val
                print(f"[Blender Setup] ✓ Simplify enabled (Max Subdivision Render = {subdiv_val})")
            except Exception as e:
                print(f"[Blender Setup] Notice: Could not set simplify: {e}")

        # 4. Custom Samples & Denoising from Dashboard
        raw_samples = job_settings.get("samples")
        if raw_samples is not None and str(raw_samples).strip() != "":
            try:
                samples_val = int(raw_samples)
                if samples_val > 0:
                    scene.cycles.samples = samples_val
                    print(f"[Blender Setup] ✓ Cycles Samples set to: {samples_val}")
            except Exception as e:
                print(f"[Blender Setup] Could not set samples: {e}")

        if job_settings.get("denoise", True):
            optix_weights = Path("/usr/share/nvidia/nvoptix.bin")
            has_optix = optix_weights.is_file()

            if not has_optix and Path("/usr").exists():
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
                    print("[Blender Setup] ✓ OpenImageDenoise (OIDN) enabled.")
            except Exception as e:
                print(f"[Blender Setup] Notice setting denoiser: {e}")
        else:
            try:
                scene.cycles.use_denoising = False
                print("[Blender Setup] ✓ Denoising disabled per job setting.")
            except Exception:
                pass

        # 5. GPU Hardware Acceleration (OptiX / CUDA)
        if scene.render.engine == 'CYCLES':
            cprefs = bpy.context.preferences.addons['cycles'].preferences
            print("[Blender Setup] Configuring GPU acceleration...")

            for dev_type in ('OPTIX', 'CUDA'):
                try:
                    cprefs.compute_device_type = dev_type
                    cprefs.get_devices()
                    activated_devices = []
                    for device in cprefs.devices:
                        if device.type == dev_type:
                            device.use = True
                            activated_devices.append(f"{device.name} ({dev_type})")
                        else:
                            device.use = False

                    if activated_devices:
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
        return True
    except Exception as e:
        print(f"[Blender Setup] Error during Blender setup: {e}")
        traceback.print_exc()
        return False


setup_blender_optimizations()

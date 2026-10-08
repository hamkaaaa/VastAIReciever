import bpy
import sys

def setup_blender_optimizations():
    try:
        scene = bpy.context.scene
        scene.render.engine = 'CYCLES'

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

        # 3. GPU Hardware Acceleration (OptiX / CUDA)
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

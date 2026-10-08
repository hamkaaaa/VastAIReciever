import bpy
import sys

def setup_cycles_gpu():
    try:
        scene = bpy.context.scene
        scene.render.engine = 'CYCLES'
        cprefs = bpy.context.preferences.addons['cycles'].preferences

        print("\n[Blender Setup] Configuring GPU acceleration...")
        
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
                    print(f"[Blender Setup] Successfully enabled compute type: {dev_type}")
                    for d in activated_devices:
                        print(f"  -> Enabled: {d}")
                    return True
            except Exception as e:
                print(f"[Blender Setup] Could not configure {dev_type}: {e}")

        print("[Blender Setup] No compatible GPU device found; falling back to CPU.")
        scene.cycles.device = 'CPU'
        return False
    except Exception as e:
        print(f"[Blender Setup] Error setting up Cycles GPU: {e}")
        return False

setup_cycles_gpu()

#!/usr/bin/env python3
"""
VastAI Render Hub — Native Windows Desktop Application (CustomTkinter + Pillow)
Replaces browser/Edge UI with a pure native desktop GUI and bounded LRU frame cache.
"""

import ctypes
import os
import sys
import threading
import time
import zipfile
from collections import OrderedDict
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageTk

import receiver


def _set_windows_app_id():
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VastAI.RenderHub.Blender52")
        except Exception:
            pass


def _resolve_icon_path():
    for base in (receiver.RESOURCE_DIR, receiver.APP_DIR):
        for rel in ("assets/app_icon.ico", "app_icon.ico"):
            p = base / rel
            if p.is_file():
                return p
    return None


class FrameCache:
    """Bounded LRU cache for decoded, viewport-resized render frames (prevents OOM)."""

    def __init__(self, max_items=24):
        self.max_items = max(1, int(max_items))
        self._cache = OrderedDict()

    def clear(self):
        self._cache.clear()

    def get_photo(self, filepath: Path, target_w: int, target_h: int):
        tw = max(64, int(target_w))
        th = max(64, int(target_h))
        key = (str(filepath), tw, th)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        with Image.open(filepath) as im:
            rgb = im.convert("RGB")
            rgb.thumbnail((tw, th), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(rgb)

        self._cache[key] = photo
        while len(self._cache) > self.max_items:
            self._cache.popitem(last=False)
        return photo

    def __len__(self):
        return len(self._cache)


class GpuMarketplaceDialog(ctk.CTkToplevel):
    """Modal window for switching, destroying, or renting Vast.ai GPUs."""

    def __init__(self, parent_app):
        super().__init__(parent_app)
        self.app = parent_app
        self.offers = []
        self.title("Rent or Switch Vast.ai GPU")
        self.geometry("920x640")
        self.minsize(780, 520)
        self.configure(fg_color="#0d1018")
        self.transient(parent_app)
        self.grab_set()

        ico = _resolve_icon_path()
        if ico:
            try:
                self.after(150, lambda: self.iconbitmap(str(ico)))
            except Exception:
                pass

        self._build_ui()
        self.refresh_instances()
        self.search_offers("RTX 5090,RTX 4090,RTX 3090,RTX 4080")

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # Existing instances header + scrollable strip
        inst_hdr = ctk.CTkFrame(self, fg_color="transparent")
        inst_hdr.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        ctk.CTkLabel(inst_hdr, text="Your Existing Vast.ai Instances", font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        ctk.CTkButton(inst_hdr, text="↻ Refresh", width=80, height=28, fg_color="#1f2436", command=self.refresh_instances).pack(side="right")

        self.existing_frame = ctk.CTkScrollableFrame(self, height=115, fg_color="#131724", corner_radius=10)
        self.existing_frame.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))
        self.existing_frame.grid_columnconfigure(0, weight=1)

        # Filter bar
        filter_bar = ctk.CTkFrame(self, fg_color="#131724", corner_radius=10)
        filter_bar.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 10))

        presets = [
            ("All Best", "RTX 5090,RTX 4090,RTX 3090,RTX 4080"),
            ("RTX 5090", "RTX 5090"),
            ("RTX 4090", "RTX 4090,RTX 4090D"),
            ("RTX 3090", "RTX 3090,RTX 3090 Ti"),
            ("RTX 4080", "RTX 4080,RTX 4080S,RTX 4070 Ti"),
            ("Any GPU", "ALL"),
        ]
        btn_row = ctk.CTkFrame(filter_bar, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=(10, 6))
        for label, q in presets:
            ctk.CTkButton(
                btn_row, text=label, width=82, height=28, fg_color="#252b40", hover_color="#4f46e5",
                command=lambda qry=q: self.search_offers(qry)
            ).pack(side="left", padx=3)

        self.ent_custom = ctk.CTkEntry(btn_row, placeholder_text="Custom: L40S, H100...", width=165, height=28)
        self.ent_custom.pack(side="left", padx=(10, 4))
        ctk.CTkButton(
            btn_row, text="Search", width=70, height=28, fg_color="#6366f1",
            command=lambda: self.search_offers(self.ent_custom.get().strip() or "ALL")
        ).pack(side="left")

        opt_row = ctk.CTkFrame(filter_bar, fg_color="transparent")
        opt_row.pack(fill="x", padx=12, pady=(0, 8))
        self.var_destroy_old = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(opt_row, text="Destroy previous instance automatically after renting a new one", variable=self.var_destroy_old).pack(side="left")
        self.lbl_status = ctk.CTkLabel(opt_row, text="", text_color="#9ca3af", font=ctk.CTkFont(size=12))
        self.lbl_status.pack(side="right")

        # Offers list
        self.offers_frame = ctk.CTkScrollableFrame(self, fg_color="#131724", corner_radius=10)
        self.offers_frame.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 14))
        self.offers_frame.grid_columnconfigure(0, weight=1)

    def refresh_instances(self):
        for w in self.existing_frame.winfo_children():
            w.destroy()
        ctk.CTkLabel(self.existing_frame, text="Loading instances...", text_color="#9ca3af").pack(pady=8)

        def _worker():
            cfg = receiver.load_config()
            if not cfg.get("api_key", "").strip():
                self.after(0, lambda: self._render_instances([], "", "Set your Vast.ai API key in Settings first."))
                return
            insts = receiver.get_all_user_instances()
            active_id = str(cfg.get("instance_id", "")).strip()
            self.after(0, lambda: self._render_instances(insts, active_id, None))

        threading.Thread(target=_worker, daemon=True).start()

    def _render_instances(self, insts, active_id, err):
        if not self.winfo_exists():
            return
        for w in self.existing_frame.winfo_children():
            w.destroy()
        if err:
            ctk.CTkLabel(self.existing_frame, text=err, text_color="#fca5a5").pack(pady=8)
            return
        if not insts:
            ctk.CTkLabel(self.existing_frame, text="No instances found on your Vast.ai account.", text_color="#6b7280").pack(pady=8)
            return

        for inst in insts:
            id_str = str(inst.get("id", ""))
            is_active = id_str == active_id
            gpu = inst.get("gpu_name", "GPU")
            st = str(inst.get("actual_status") or inst.get("cur_state") or "unknown").upper()
            dph = f"${float(inst.get('dph_total', 0)):.3f}/hr" if inst.get("dph_total") else "-"
            row = ctk.CTkFrame(self.existing_frame, fg_color="#1b2032" if is_active else "#171b2a", corner_radius=8)
            row.pack(fill="x", pady=3, padx=4)

            txt = f"#{id_str}  ·  {gpu}  ·  {st}  ·  {dph}" + ("   [ACTIVE]" if is_active else "")
            ctk.CTkLabel(row, text=txt, font=ctk.CTkFont(size=12, weight="bold" if is_active else "normal")).pack(side="left", padx=10, pady=6)

            ctk.CTkButton(
                row, text="Destroy", width=72, height=26, fg_color="#7f1d1d", hover_color="#b91c1c",
                command=lambda iid=id_str: self._destroy_instance(iid)
            ).pack(side="right", padx=6, pady=4)
            if not is_active:
                ctk.CTkButton(
                    row, text="Select", width=68, height=26, fg_color="#4f46e5",
                    command=lambda iid=id_str: self._select_instance(iid)
                ).pack(side="right", padx=4, pady=4)

    def _select_instance(self, iid):
        cfg = receiver.load_config()
        cfg["instance_id"] = iid
        receiver.save_config(cfg)
        self.app.ent_instance_id.delete(0, tk.END)
        self.app.ent_instance_id.insert(0, iid)
        self.refresh_instances()
        self.app.refresh_instance_async()

    def _destroy_instance(self, iid):
        if not messagebox.askyesno("Confirm Destroy", f"Permanently destroy Vast.ai instance #{iid}?", parent=self):
            return

        def _worker():
            receiver.destroy_instance_api(iid)
            self.after(0, self.refresh_instances)
            self.app.after(0, self.app.refresh_instance_async)

        threading.Thread(target=_worker, daemon=True).start()

    def search_offers(self, models_str):
        for w in self.offers_frame.winfo_children():
            w.destroy()
        self.lbl_status.configure(text="Searching Vast.ai...")
        ctk.CTkLabel(self.offers_frame, text="Fetching live GPU offers from Vast.ai...", text_color="#9ca3af").pack(pady=18)

        def _worker():
            cfg = receiver.load_config()
            if not cfg.get("api_key", "").strip():
                self.after(0, lambda: self._render_offers([], "Set your Vast.ai API key in Settings first."))
                return
            if models_str.upper() == "ALL":
                flt = ()
            else:
                flt = tuple(x.strip() for x in models_str.split(",") if x.strip())
            offers = receiver.search_available_gpus(gpu_names=flt, max_results=25)
            self.after(0, lambda: self._render_offers(offers, None))

        threading.Thread(target=_worker, daemon=True).start()

    def _render_offers(self, offers, err):
        if not self.winfo_exists():
            return
        for w in self.offers_frame.winfo_children():
            w.destroy()
        self.offers = offers or []
        if err:
            self.lbl_status.configure(text="")
            ctk.CTkLabel(self.offers_frame, text=err, text_color="#fca5a5").pack(pady=18)
            return
        if not self.offers:
            self.lbl_status.configure(text="0 offers")
            ctk.CTkLabel(self.offers_frame, text="No verified rentable GPUs matched that filter right now.", text_color="#6b7280").pack(pady=18)
            return

        self.lbl_status.configure(text=f"{len(self.offers)} offers found")
        for idx, o in enumerate(self.offers):
            vram = f"{round(o['gpu_ram'] / 1024)} GB" if o.get("gpu_ram") else "-"
            dph = f"${float(o.get('dph_total', 0)):.3f}/hr"
            inet = f"{round(o.get('inet_down', 0))} Mbps"
            loc = (o.get("geolocation") or "Unknown")[:22]
            rel = f"{float(o.get('reliability2', 0)) * 100:.1f}%"

            row = ctk.CTkFrame(self.offers_frame, fg_color="#181c2b", corner_radius=8)
            row.pack(fill="x", pady=3, padx=4)

            info = f"#{idx+1:<2}  {o.get('gpu_name', 'GPU'):<13} |  {vram:<6} |  {dph:<10} |  {inet:<9} |  {loc:<22} |  Rel {rel}"
            ctk.CTkLabel(row, text=info, font=ctk.CTkFont(family="Consolas", size=12)).pack(side="left", padx=10, pady=7)
            btn = ctk.CTkButton(row, text="Rent", width=72, height=28, fg_color="#6366f1", hover_color="#4f46e5")
            btn.configure(command=lambda oid=o["id"], i=idx, b=btn: self._rent(oid, i, b))
            btn.pack(side="right", padx=8, pady=5)

    def _rent(self, offer_id, idx, btn):
        btn.configure(state="disabled", text="Renting...")
        fallbacks = [o["id"] for o in self.offers[idx + 1: idx + 5]]
        del_old = bool(self.var_destroy_old.get())
        self.lbl_status.configure(text=f"Renting offer #{offer_id}...")

        def _worker():
            res = receiver.rent_gpu_offer(offer_id, fallback_ids=fallbacks, delete_old=del_old)
            self.after(0, lambda: self._on_rent_done(res, btn))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_rent_done(self, res, btn):
        if not self.winfo_exists():
            return
        if res.get("success"):
            iid = str(res["instance_id"])
            self.app.ent_instance_id.delete(0, tk.END)
            self.app.ent_instance_id.insert(0, iid)
            self.app.refresh_instance_async()
            self.destroy()
        else:
            messagebox.showerror("Rent Failed", res.get("error") or "Unknown error", parent=self)
            btn.configure(state="normal", text="Rent")
            self.lbl_status.configure(text="Rent failed")


class VastRenderHubApp(ctk.CTk):
    """Main native desktop window for VastAI Render Hub (Blender 5.2 LTS)."""

    def __init__(self, worker_server=None):
        super().__init__()
        self.worker_server = worker_server
        self.frame_cache = FrameCache(max_items=24)

        self.frames = []
        self.current_job = ""
        self.current_frame_index = 0
        self.is_playing = False
        self.fps = 24
        self._play_after_id = None
        self._resize_after_id = None
        self._current_photo = None  # keep reference to displayed PhotoImage

        self.last_log_id = 0
        self.zip_blend_files = []
        self.addon_paths = []
        self._api_key_visible = False
        self._closing = False

        _set_windows_app_id()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("VastAI Render Hub — Blender 5.2 LTS")
        self.geometry("1440x880")
        self.minsize(1100, 700)
        self.configure(fg_color="#090a0f")

        ico = _resolve_icon_path()
        if ico:
            try:
                self.iconbitmap(str(ico))
            except Exception:
                pass

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._load_initial_data()
        self._bind_shortcuts()

        self.after(400, self._poll_fast)
        self.after(600, self._poll_jobs)
        self.after(800, self.refresh_instance_async)
        self.after(15000, self._periodic_instance_poll)

    # ------------------------------------------------------------------ UI Construction
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # HEADER BAR
        hdr = ctk.CTkFrame(self, fg_color="#0f121b", corner_radius=0, height=58)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_propagate(False)

        left_hdr = ctk.CTkFrame(hdr, fg_color="transparent")
        left_hdr.pack(side="left", padx=16, pady=10)
        ctk.CTkLabel(left_hdr, text="⚡ VastAI Render Hub", font=ctk.CTkFont(size=18, weight="bold")).pack(side="left", padx=(0, 10))
        self.lbl_gpu_badge = ctk.CTkLabel(
            left_hdr, text="NO GPU", fg_color="#1f2547", text_color="#a5b4fc",
            corner_radius=6, padx=8, pady=2, font=ctk.CTkFont(size=11, weight="bold")
        )
        self.lbl_gpu_badge.pack(side="left", padx=4)
        ctk.CTkLabel(
            left_hdr, text="BLENDER 5.2 LTS", fg_color="#3b2b10", text_color="#fcd34d",
            corner_radius=6, padx=8, pady=2, font=ctk.CTkFont(size=11, weight="bold")
        ).pack(side="left", padx=4)

        right_hdr = ctk.CTkFrame(hdr, fg_color="transparent")
        right_hdr.pack(side="right", padx=16, pady=8)

        self.lbl_inst_status = ctk.CTkLabel(
            right_hdr, text="● CHECKING...", fg_color="#181c28", text_color="#9ca3af",
            corner_radius=8, padx=10, pady=4, font=ctk.CTkFont(size=12, weight="bold")
        )
        self.lbl_inst_status.pack(side="left", padx=6)

        self.lbl_cost = ctk.CTkLabel(
            right_hdr, text="Rate: --  |  Session: $0.0000 · Rp 0",
            text_color="#38bdf8", font=ctk.CTkFont(family="Consolas", size=12)
        )
        self.lbl_cost.pack(side="left", padx=8)

        ctk.CTkButton(
            right_hdr, text="🎮 Rent / Switch GPU", width=148, height=32, fg_color="#6366f1",
            hover_color="#4f46e5", command=self._open_gpu_modal
        ).pack(side="left", padx=4)

        self.btn_start_inst = ctk.CTkButton(
            right_hdr, text="▶ Start", width=76, height=32, fg_color="#065f46",
            hover_color="#047857", command=lambda: self._instance_action("start")
        )
        self.btn_stop_inst = ctk.CTkButton(
            right_hdr, text="⏹ Stop", width=76, height=32, fg_color="#7f1d1d",
            hover_color="#b91c1c", command=lambda: self._instance_action("stop")
        )

        # MAIN BODY
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=14, pady=12)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=0)
        body.grid_rowconfigure(0, weight=1)

        # LEFT PANEL (Viewer + Console)
        left_panel = ctk.CTkFrame(body, fg_color="transparent")
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left_panel.grid_columnconfigure(0, weight=1)
        left_panel.grid_rowconfigure(0, weight=3)
        left_panel.grid_rowconfigure(1, weight=2)

        viewer_card = ctk.CTkFrame(left_panel, fg_color="#12141e", corner_radius=12, border_width=1, border_color="#23273a")
        viewer_card.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        viewer_card.grid_columnconfigure(0, weight=1)
        viewer_card.grid_rowconfigure(2, weight=1)

        # Job bar
        job_bar = ctk.CTkFrame(viewer_card, fg_color="#0d0f17", corner_radius=0)
        job_bar.grid(row=0, column=0, sticky="ew", padx=1, pady=(1, 0))
        ctk.CTkLabel(job_bar, text="Job:", font=ctk.CTkFont(size=12, weight="bold"), text_color="#9ca3af").pack(side="left", padx=(12, 6), pady=8)
        self.opt_jobs = ctk.CTkOptionMenu(job_bar, values=["No renders yet"], width=300, command=self._on_job_selected)
        self.opt_jobs.pack(side="left", padx=4, pady=6)

        self.lbl_live_pill = ctk.CTkLabel(
            job_bar, text="● RENDERING LIVE", fg_color="#451a1a", text_color="#fca5a5",
            corner_radius=10, padx=10, pady=2, font=ctk.CTkFont(size=11, weight="bold")
        )

        ctk.CTkButton(job_bar, text="📁 Open Folder", width=110, height=28, fg_color="#1e2233", command=self._open_renders_folder).pack(side="right", padx=(4, 12), pady=6)
        ctk.CTkButton(job_bar, text="↻ Refresh", width=82, height=28, fg_color="#1e2233", command=self._poll_jobs).pack(side="right", padx=4, pady=6)

        # Status strip
        strip = ctk.CTkFrame(viewer_card, fg_color="#151928", corner_radius=0)
        strip.grid(row=1, column=0, sticky="ew", padx=1)
        self.lbl_tunnel = ctk.CTkLabel(strip, text="Tunnel: Starting Cloudflare tunnel...", text_color="#a5b4fc", font=ctk.CTkFont(family="Consolas", size=11))
        self.lbl_tunnel.pack(side="left", padx=12, pady=5)
        ctk.CTkButton(strip, text="Copy URL", width=68, height=22, fg_color="#23283d", font=ctk.CTkFont(size=11), command=self._copy_tunnel_url).pack(side="left", padx=4, pady=3)
        self.lbl_worker = ctk.CTkLabel(strip, text="Worker: Idle (0 frames)", text_color="#cbd5e1", font=ctk.CTkFont(size=11))
        self.lbl_worker.pack(side="right", padx=12, pady=5)

        # Viewport (pure tk.Frame + tk.Label for bounded ImageTk.PhotoImage display)
        self.viewport_frame = tk.Frame(viewer_card, bg="#050608", highlightthickness=0)
        self.viewport_frame.grid(row=2, column=0, sticky="nsew", padx=2, pady=2)
        self.viewport_frame.grid_propagate(False)
        self.viewport_frame.bind("<Configure>", self._on_viewport_resize)

        self.viewport_label = tk.Label(
            self.viewport_frame,
            text="Waiting for rendered frames from Vast.ai...\nSelect a completed job above to preview.",
            bg="#050608",
            fg="#6b7280",
            font=("Segoe UI", 11),
            compound="center",
        )
        self.viewport_label.pack(fill="both", expand=True)

        self.btn_open_external = ctk.CTkButton(
            self.viewport_frame, text="Open Frame / Video Externally", width=210, height=32,
            fg_color="#4f46e5", command=self._open_current_file_externally
        )

        # Playback controls
        ctrl_bar = ctk.CTkFrame(viewer_card, fg_color="#0d1018", corner_radius=0)
        ctrl_bar.grid(row=3, column=0, sticky="ew", padx=1, pady=(0, 1))

        slider_row = ctk.CTkFrame(ctrl_bar, fg_color="transparent")
        slider_row.pack(fill="x", padx=12, pady=(8, 4))
        self.slider_frame = ctk.CTkSlider(slider_row, from_=0, to=1, number_of_steps=1, state="disabled", command=self._on_scrub)
        self.slider_frame.set(0)
        self.slider_frame.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.lbl_frame_counter = ctk.CTkLabel(slider_row, text="0 / 0", width=95, anchor="e", font=ctk.CTkFont(family="Consolas", size=12, weight="bold"))
        self.lbl_frame_counter.pack(side="right")

        btn_bar = ctk.CTkFrame(ctrl_bar, fg_color="transparent")
        btn_bar.pack(fill="x", padx=12, pady=(2, 8))
        ctk.CTkButton(btn_bar, text="⏮", width=38, height=30, fg_color="#1f2436", command=lambda: self._step_frame(-1)).pack(side="left", padx=3)
        self.btn_play = ctk.CTkButton(btn_bar, text="▶", width=46, height=30, fg_color="#6366f1", command=self._toggle_play)
        self.btn_play.pack(side="left", padx=3)
        ctk.CTkButton(btn_bar, text="⏭", width=38, height=30, fg_color="#1f2436", command=lambda: self._step_frame(1)).pack(side="left", padx=3)
        ctk.CTkButton(btn_bar, text="⏩ Latest", width=78, height=30, fg_color="#1f2436", command=self._jump_latest).pack(side="left", padx=3)

        self.lbl_active_filename = ctk.CTkLabel(btn_bar, text="", text_color="#9ca3af", font=ctk.CTkFont(family="Consolas", size=11))
        self.lbl_active_filename.pack(side="left", padx=12)

        self.opt_fps = ctk.CTkOptionMenu(btn_bar, values=["12 fps", "24 fps", "30 fps", "60 fps"], width=90, command=self._on_fps_change)
        self.opt_fps.set("24 fps")
        self.opt_fps.pack(side="right")

        # Console card
        console_card = ctk.CTkFrame(left_panel, fg_color="#12141e", corner_radius=12, border_width=1, border_color="#23273a")
        console_card.grid(row=1, column=0, sticky="nsew")
        console_card.grid_columnconfigure(0, weight=1)
        console_card.grid_rowconfigure(1, weight=1)

        chdr = ctk.CTkFrame(console_card, fg_color="#0d0f17", corner_radius=0)
        chdr.grid(row=0, column=0, sticky="ew", padx=1, pady=(1, 0))
        ctk.CTkLabel(chdr, text="🖥 Live Worker & Blender 5.2 Console", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left", padx=12, pady=6)

        ctk.CTkButton(chdr, text="Clear", width=62, height=24, fg_color="#1e2233", command=self._clear_console).pack(side="right", padx=(4, 10), pady=4)
        ctk.CTkButton(chdr, text="📋 Copy Cmd", width=96, height=24, fg_color="#1e2233", command=self._copy_manual_command).pack(side="right", padx=4, pady=4)
        self.var_autoscroll = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(chdr, text="Auto-scroll", variable=self.var_autoscroll, width=90).pack(side="right", padx=6)

        self.txt_console = ctk.CTkTextbox(console_card, fg_color="#06070b", text_color="#cbd5e1", font=ctk.CTkFont(family="Consolas", size=11), wrap="word")
        self.txt_console.grid(row=1, column=0, sticky="nsew", padx=2, pady=2)
        self.txt_console.configure(state="disabled")
        self.txt_console.tag_config("blender", foreground="#93c5fd")
        self.txt_console.tag_config("frame", foreground="#6ee7b7")
        self.txt_console.tag_config("error", foreground="#fca5a5")
        self.txt_console.tag_config("cost", foreground="#fcd34d")
        self.txt_console.tag_config("system", foreground="#cbd5e1")

        # RIGHT SCROLLABLE SIDEBAR
        sidebar = ctk.CTkScrollableFrame(body, width=400, fg_color="#0f121b", corner_radius=12, border_width=1, border_color="#23273a")
        sidebar.grid(row=0, column=1, sticky="nsew")
        sidebar.grid_columnconfigure(0, weight=1)

        # Card 1: Project & Add-ons
        proj_card = ctk.CTkFrame(sidebar, fg_color="#161926", corner_radius=10)
        proj_card.pack(fill="x", padx=8, pady=(8, 8))
        ctk.CTkLabel(proj_card, text="📁 Project & Add-ons", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=12, pady=(10, 6))

        self.seg_source = ctk.CTkSegmentedButton(proj_card, values=["Google Drive", "Local PC", "Vast Local"], command=self._on_source_tab)
        self.seg_source.pack(fill="x", padx=12, pady=(0, 8))
        self.seg_source.set("Google Drive")

        # Sub-panels for source type
        self.frm_gdrive = ctk.CTkFrame(proj_card, fg_color="transparent")
        ctk.CTkLabel(self.frm_gdrive, text="Google Drive Link (.blend or .zip):", font=ctk.CTkFont(size=11), text_color="#9ca3af").pack(anchor="w")
        self.ent_gdrive = ctk.CTkEntry(self.frm_gdrive, placeholder_text="https://drive.google.com/file/d/...")
        self.ent_gdrive.pack(fill="x", pady=(2, 2))
        self.ent_gdrive.bind("<FocusOut>", lambda _e: self._sync_job_config())

        self.frm_local = ctk.CTkFrame(proj_card, fg_color="transparent")
        ctk.CTkLabel(self.frm_local, text="Local File (.blend / .zip) or Folder:", font=ctk.CTkFont(size=11), text_color="#9ca3af").pack(anchor="w")
        self.ent_local_path = ctk.CTkEntry(self.frm_local, placeholder_text="Select .blend, .zip, or folder...")
        self.ent_local_path.pack(fill="x", pady=(2, 6))
        self.ent_local_path.bind("<FocusOut>", lambda _e: self._sync_job_config())

        browse_row = ctk.CTkFrame(self.frm_local, fg_color="transparent")
        browse_row.pack(fill="x")
        ctk.CTkButton(browse_row, text="📄 Browse File", height=28, fg_color="#252b40", command=lambda: self._browse_project("file")).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(browse_row, text="📂 Browse Folder", height=28, fg_color="#252b40", command=lambda: self._browse_project("folder")).pack(side="left", fill="x", expand=True, padx=(4, 0))

        self.frm_zip_blend = ctk.CTkFrame(self.frm_local, fg_color="transparent")
        ctk.CTkLabel(self.frm_zip_blend, text="Scene .blend inside Archive:", font=ctk.CTkFont(size=11), text_color="#9ca3af").pack(anchor="w", pady=(6, 2))
        self.opt_zip_blend = ctk.CTkOptionMenu(self.frm_zip_blend, values=[""], command=lambda _v: self._sync_job_config())
        self.opt_zip_blend.pack(fill="x")

        self.frm_vast_local = ctk.CTkFrame(proj_card, fg_color="transparent")
        ctk.CTkLabel(self.frm_vast_local, text="Path on Vast.ai (blank = newest .blend):", font=ctk.CTkFont(size=11), text_color="#9ca3af").pack(anchor="w")
        self.ent_vast_path = ctk.CTkEntry(self.frm_vast_local, placeholder_text="/workspace/scene.blend")
        self.ent_vast_path.pack(fill="x", pady=(2, 2))
        self.ent_vast_path.bind("<FocusOut>", lambda _e: self._sync_job_config())

        self.frm_gdrive.pack(fill="x", padx=12, pady=(0, 8))

        # Add-ons row
        addon_hdr = ctk.CTkFrame(proj_card, fg_color="transparent")
        addon_hdr.pack(fill="x", padx=12, pady=(4, 2))
        ctk.CTkLabel(addon_hdr, text="Blender 5.2 Add-ons:", font=ctk.CTkFont(size=11, weight="bold"), text_color="#9ca3af").pack(side="left")
        ctk.CTkButton(addon_hdr, text="Clear", width=50, height=22, fg_color="#252b40", font=ctk.CTkFont(size=11), command=self._clear_addons).pack(side="right", padx=(4, 0))
        ctk.CTkButton(addon_hdr, text="+ Add .zip/.py", width=92, height=22, fg_color="#252b40", font=ctk.CTkFont(size=11), command=self._browse_addons).pack(side="right")

        self.lbl_addons = ctk.CTkLabel(
            proj_card, text="None attached (Camera Shakify auto-shim built-in)",
            text_color="#6b7280", font=ctk.CTkFont(size=11), wraplength=350, justify="left"
        )
        self.lbl_addons.pack(anchor="w", padx=12, pady=(0, 10))

        # Card 2: Render Settings
        rnd_card = ctk.CTkFrame(sidebar, fg_color="#161926", corner_radius=10, border_width=1, border_color="#313660")
        rnd_card.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(rnd_card, text="🎬 Render Settings (Blender 5.2 LTS)", font=ctk.CTkFont(size=13, weight="bold"), text_color="#a5b4fc").pack(anchor="w", padx=12, pady=(10, 6))

        self.seg_mode = ctk.CTkSegmentedButton(rnd_card, values=["Full Anim", "Frame Range", "Single Frame"], command=self._on_mode_tab)
        self.seg_mode.pack(fill="x", padx=12, pady=(0, 8))
        self.seg_mode.set("Full Anim")

        self.frm_range = ctk.CTkFrame(rnd_card, fg_color="transparent")
        self.ent_start = ctk.CTkEntry(self.frm_range, placeholder_text="Start (1)")
        self.ent_start.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.ent_end = ctk.CTkEntry(self.frm_range, placeholder_text="End (120)")
        self.ent_end.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.ent_start.bind("<FocusOut>", lambda _e: self._sync_job_config())
        self.ent_end.bind("<FocusOut>", lambda _e: self._sync_job_config())

        self.frm_single = ctk.CTkFrame(rnd_card, fg_color="transparent")
        self.ent_single = ctk.CTkEntry(self.frm_single, placeholder_text="Frame number (e.g. 1)")
        self.ent_single.pack(fill="x")
        self.ent_single.bind("<FocusOut>", lambda _e: self._sync_job_config())

        # Samples
        samp_hdr = ctk.CTkFrame(rnd_card, fg_color="transparent")
        samp_hdr.pack(fill="x", padx=12, pady=(4, 2))
        ctk.CTkLabel(samp_hdr, text="Samples (blank=.blend):", font=ctk.CTkFont(size=11), text_color="#9ca3af").pack(side="left")
        for lbl, val in [("Default", ""), ("64", "64"), ("128", "128"), ("256", "256")]:
            ctk.CTkButton(samp_hdr, text=lbl, width=44, height=22, fg_color="#252b40", font=ctk.CTkFont(size=10), command=lambda v=val: self._set_samples(v)).pack(side="right", padx=2)
        self.ent_samples = ctk.CTkEntry(rnd_card, placeholder_text="Default (.blend setting)")
        self.ent_samples.pack(fill="x", padx=12, pady=(0, 8))
        self.ent_samples.bind("<FocusOut>", lambda _e: self._sync_job_config())

        # Switches
        self.var_exec_all = tk.BooleanVar(value=True)
        self.var_denoise = tk.BooleanVar(value=True)
        self.var_persistent = tk.BooleanVar(value=True)
        self.var_simplify = tk.BooleanVar(value=False)

        ctk.CTkSwitch(rnd_card, text="Auto-Run All Blender Scripts (5.2)", variable=self.var_exec_all, command=self._sync_job_config).pack(anchor="w", padx=12, pady=3)
        ctk.CTkSwitch(rnd_card, text="AI Denoising (OptiX / OIDN)", variable=self.var_denoise, command=self._sync_job_config).pack(anchor="w", padx=12, pady=3)
        ctk.CTkSwitch(rnd_card, text="Persistent Data (Faster Frames)", variable=self.var_persistent, command=self._sync_job_config).pack(anchor="w", padx=12, pady=3)

        simp_row = ctk.CTkFrame(rnd_card, fg_color="transparent")
        simp_row.pack(fill="x", padx=12, pady=(3, 8))
        ctk.CTkSwitch(simp_row, text="Simplify Max Subdiv", variable=self.var_simplify, command=self._sync_job_config).pack(side="left")
        self.ent_subdiv = ctk.CTkEntry(simp_row, width=48, justify="center")
        self.ent_subdiv.insert(0, "2")
        self.ent_subdiv.pack(side="right")
        self.ent_subdiv.bind("<FocusOut>", lambda _e: self._sync_job_config())

        # Action Buttons
        act_row = ctk.CTkFrame(rnd_card, fg_color="transparent")
        act_row.pack(fill="x", padx=12, pady=(4, 6))
        self.btn_start_render = ctk.CTkButton(
            act_row, text="🚀 Start Render on Vast.ai", height=38, fg_color="#6366f1",
            hover_color="#4f46e5", font=ctk.CTkFont(size=13, weight="bold"), command=self._start_render
        )
        self.btn_start_render.pack(side="left", fill="x", expand=True)

        self.btn_cancel_launch = ctk.CTkButton(
            act_row, text="⏹ Cancel", width=84, height=38, fg_color="#7f1d1d",
            hover_color="#b91c1c", command=self._cancel_launch
        )

        ctk.CTkButton(
            rnd_card, text="🛰 Queue Job to Running Daemon Worker", height=30,
            fg_color="#23283d", hover_color="#2e3552", command=self._queue_daemon_job
        ).pack(fill="x", padx=12, pady=(0, 10))

        # Card 3: Cost Protection & Credentials
        cfg_card = ctk.CTkFrame(sidebar, fg_color="#161926", corner_radius=10)
        cfg_card.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(cfg_card, text="⚙ Cost Protection & Vast.ai Key", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=12, pady=(10, 6))

        self.var_auto_stop = tk.BooleanVar(value=True)
        self.var_idle_stop = tk.BooleanVar(value=True)
        ctk.CTkSwitch(cfg_card, text="Auto-Stop Instance on Finish", variable=self.var_auto_stop, command=self._save_config).pack(anchor="w", padx=12, pady=3)

        idle_row = ctk.CTkFrame(cfg_card, fg_color="transparent")
        idle_row.pack(fill="x", padx=12, pady=3)
        ctk.CTkSwitch(idle_row, text="Idle Auto-Stop (mins)", variable=self.var_idle_stop, command=self._save_config).pack(side="left")
        self.ent_idle_mins = ctk.CTkEntry(idle_row, width=52, justify="center")
        self.ent_idle_mins.insert(0, "10")
        self.ent_idle_mins.pack(side="right")
        self.ent_idle_mins.bind("<FocusOut>", lambda _e: self._save_config())

        ctk.CTkLabel(cfg_card, text="Vast.ai API Key:", font=ctk.CTkFont(size=11), text_color="#9ca3af").pack(anchor="w", padx=12, pady=(6, 2))
        key_row = ctk.CTkFrame(cfg_card, fg_color="transparent")
        key_row.pack(fill="x", padx=12)
        self.ent_api_key = ctk.CTkEntry(key_row, show="*", placeholder_text="Paste Vast API Key...")
        self.ent_api_key.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.btn_toggle_key = ctk.CTkButton(key_row, text="Show", width=54, height=28, fg_color="#252b40", command=self._toggle_key_visibility)
        self.btn_toggle_key.pack(side="right")

        ctk.CTkLabel(cfg_card, text="Active Instance ID:", font=ctk.CTkFont(size=11), text_color="#9ca3af").pack(anchor="w", padx=12, pady=(6, 2))
        self.ent_instance_id = ctk.CTkEntry(cfg_card, placeholder_text="Auto-filled on rent or select in GPU modal")
        self.ent_instance_id.pack(fill="x", padx=12, pady=(0, 8))

        ctk.CTkButton(
            cfg_card, text="Save & Refresh Instance", height=30, fg_color="#252b40",
            hover_color="#353d5c", command=self._save_and_refresh_instance
        ).pack(fill="x", padx=12, pady=(0, 12))

    # ------------------------------------------------------------------ Config & Source Sync
    def _load_initial_data(self):
        cfg = receiver.load_config()
        if cfg.get("api_key"):
            self.ent_api_key.insert(0, cfg["api_key"])
        if cfg.get("instance_id"):
            self.ent_instance_id.insert(0, str(cfg["instance_id"]))
        self.var_auto_stop.set(cfg.get("auto_stop", True))
        self.var_idle_stop.set(cfg.get("idle_stop", True))
        self.ent_idle_mins.delete(0, tk.END)
        self.ent_idle_mins.insert(0, str(cfg.get("idle_minutes", 10)))

        with receiver.state_lock:
            jcfg = dict(receiver.current_job_config)
        self.addon_paths = list(jcfg.get("addon_paths") or [])
        self._refresh_addons_label()

    def _toggle_key_visibility(self):
        self._api_key_visible = not self._api_key_visible
        self.ent_api_key.configure(show="" if self._api_key_visible else "*")
        self.btn_toggle_key.configure(text="Hide" if self._api_key_visible else "Show")

    def _save_config(self):
        cfg = receiver.load_config()
        cfg["api_key"] = self.ent_api_key.get().strip()
        cfg["instance_id"] = self.ent_instance_id.get().strip()
        cfg["auto_stop"] = bool(self.var_auto_stop.get())
        cfg["idle_stop"] = bool(self.var_idle_stop.get())
        try:
            cfg["idle_minutes"] = max(1, int(self.ent_idle_mins.get().strip() or "10"))
        except ValueError:
            cfg["idle_minutes"] = 10
        receiver.save_config(cfg)

    def _save_and_refresh_instance(self):
        self._save_config()
        self.refresh_instance_async()

    def _on_source_tab(self, value):
        self.frm_gdrive.pack_forget()
        self.frm_local.pack_forget()
        self.frm_vast_local.pack_forget()
        if value == "Google Drive":
            self.frm_gdrive.pack(fill="x", padx=12, pady=(0, 8))
        elif value == "Local PC":
            self.frm_local.pack(fill="x", padx=12, pady=(0, 8))
        else:
            self.frm_vast_local.pack(fill="x", padx=12, pady=(0, 8))
        self._sync_job_config()

    def _on_mode_tab(self, value):
        self.frm_range.pack_forget()
        self.frm_single.pack_forget()
        if value == "Frame Range":
            self.frm_range.pack(fill="x", padx=12, pady=(0, 6), after=self.seg_mode)
        elif value == "Single Frame":
            self.frm_single.pack(fill="x", padx=12, pady=(0, 6), after=self.seg_mode)
        self._sync_job_config()

    def _set_samples(self, val):
        self.ent_samples.delete(0, tk.END)
        if val:
            self.ent_samples.insert(0, val)
        self._sync_job_config()

    def _browse_project(self, kind):
        if kind == "folder":
            picked = filedialog.askdirectory(title="Select Blender Project Folder", parent=self)
        else:
            picked = filedialog.askopenfilename(
                title="Select Blender Scene (.blend) or Archive (.zip)",
                filetypes=[("Blender / Zip", "*.blend *.zip"), ("All Files", "*.*")],
                parent=self,
            )
        if not picked:
            return
        self.ent_local_path.delete(0, tk.END)
        self.ent_local_path.insert(0, picked)
        self.seg_source.set("Local PC")
        self._on_source_tab("Local PC")
        self._refresh_zip_blends_ui(picked)
        self._sync_job_config()

    def _refresh_zip_blends_ui(self, path_str):
        p = Path(path_str)
        if p.is_file() and p.suffix.lower() == ".zip":
            blends = receiver.inspect_zip_blend_files(p)
            self.zip_blend_files = blends
            if len(blends) > 1:
                self.opt_zip_blend.configure(values=blends)
                self.opt_zip_blend.set(blends[0])
                self.frm_zip_blend.pack(fill="x")
                return
        self.zip_blend_files = []
        self.frm_zip_blend.pack_forget()

    def _browse_addons(self):
        picked = filedialog.askopenfilenames(
            title="Select Blender 5.2 Add-ons (.zip or .py)",
            filetypes=[("Blender Add-ons", "*.zip *.py"), ("All Files", "*.*")],
            parent=self,
        )
        if not picked:
            return
        for p in picked:
            if p not in self.addon_paths:
                self.addon_paths.append(p)
        self._refresh_addons_label()
        self._sync_job_config()

    def _clear_addons(self):
        self.addon_paths = []
        self._refresh_addons_label()
        self._sync_job_config()

    def _refresh_addons_label(self):
        if not self.addon_paths:
            self.lbl_addons.configure(text="None attached (Camera Shakify auto-shim built-in)", text_color="#6b7280")
        else:
            names = ", ".join(Path(p).name for p in self.addon_paths)
            self.lbl_addons.configure(text=f"Attached: {names}", text_color="#c7d2fe")

    def _sync_job_config(self):
        src_map = {"Google Drive": "gdrive", "Local PC": "pc_upload", "Vast Local": "vast_local"}
        mode_map = {"Full Anim": "all", "Frame Range": "range", "Single Frame": "single"}
        try:
            subdiv = int(self.ent_subdiv.get().strip() or "2")
        except ValueError:
            subdiv = 2

        proj_path = self.ent_local_path.get().strip()
        if proj_path and Path(proj_path).is_file() and proj_path.lower().endswith(".zip") and not self.zip_blend_files:
            self._refresh_zip_blends_ui(proj_path)

        blend_in_zip = self.opt_zip_blend.get() if len(self.zip_blend_files) > 1 else ""
        with receiver.state_lock:
            receiver.current_job_config.update({
                "source_type": src_map.get(self.seg_source.get(), "gdrive"),
                "gdrive_url": self.ent_gdrive.get().strip(),
                "project_path": proj_path,
                "blend_in_zip": blend_in_zip,
                "zip_blend_files": list(self.zip_blend_files),
                "vast_blend_path": self.ent_vast_path.get().strip(),
                "addon_paths": list(self.addon_paths),
                "render_mode": mode_map.get(self.seg_mode.get(), "all"),
                "start_frame": self.ent_start.get().strip(),
                "end_frame": self.ent_end.get().strip(),
                "single_frame": self.ent_single.get().strip(),
                "samples": self.ent_samples.get().strip(),
                "denoise": bool(self.var_denoise.get()),
                "persistent_data": bool(self.var_persistent.get()),
                "use_simplify": bool(self.var_simplify.get()),
                "simplify_subdiv": subdiv,
                "exec_all_scripts": bool(self.var_exec_all.get()),
            })

    # ------------------------------------------------------------------ Actions
    def _start_render(self):
        self._save_config()
        self._sync_job_config()
        if receiver.launch_in_progress:
            messagebox.showinfo("Launch Busy", "A Vast.ai render launch is already in progress.", parent=self)
            return

        self.btn_start_render.configure(state="disabled", text="⏳ Preparing...")

        def _prep_and_go():
            ok, err = receiver.prepare_local_bundles_from_config()
            if not ok:
                self.after(0, lambda: self._on_start_error(err))
                return
            with receiver.state_lock:
                receiver.current_job_config["project_revision"] = int(receiver.current_job_config.get("project_revision", 0)) + 1
            receiver.ensure_tunnel_async()
            threading.Thread(target=receiver.launch_render_worker_thread, daemon=True).start()
            self.after(0, lambda: self.btn_start_render.configure(state="normal", text="🚀 Start Render on Vast.ai"))

        threading.Thread(target=_prep_and_go, daemon=True).start()

    def _on_start_error(self, err):
        self.btn_start_render.configure(state="normal", text="🚀 Start Render on Vast.ai")
        messagebox.showerror("Project Error", err, parent=self)

    def _cancel_launch(self):
        receiver.launch_cancel_event.set()
        if receiver.active_ssh_proc is not None:
            try:
                receiver.active_ssh_proc.terminate()
            except Exception:
                pass
        receiver.add_log("Cancel signal sent to Vast.ai launch.", "system")

    def _queue_daemon_job(self):
        self._sync_job_config()
        ok, err = receiver.prepare_local_bundles_from_config()
        if not ok:
            messagebox.showerror("Project Error", err, parent=self)
            return
        with receiver.state_lock:
            receiver.current_job_config["project_revision"] = int(receiver.current_job_config.get("project_revision", 0)) + 1
            payload = dict(receiver.current_job_config)
            payload["active"] = True
            payload["has_addons"] = bool(receiver.current_addons_zip and receiver.current_addons_zip.is_file())
            if payload.get("source_type") == "pc_upload" and receiver.current_project_file and receiver.current_project_file.is_file():
                payload["upload_project"] = True
                payload["gdrive_url"] = ""
                payload["project_name"] = receiver.current_project_file.name
                payload["project_type"] = receiver.current_project_type or "blend"
                payload["file_size"] = receiver.current_project_file.stat().st_size
            elif payload.get("source_type") == "gdrive":
                payload["upload_project"] = False
            else:
                payload["upload_project"] = False
                payload["gdrive_url"] = ""
            receiver.pending_daemon_job = payload
        receiver.add_log("Queued render job for active daemon worker.", "system")

    def _copy_tunnel_url(self):
        if receiver.current_tunnel_url:
            self.clipboard_clear()
            self.clipboard_append(receiver.current_tunnel_url)

    def _copy_manual_command(self):
        self._sync_job_config()
        cmd = receiver.build_vast_worker_command(daemon=False)
        self.clipboard_clear()
        self.clipboard_append(cmd)
        receiver.add_log("Copied manual Vast.ai render command to clipboard.", "system")

    def _open_gpu_modal(self):
        self._save_config()
        GpuMarketplaceDialog(self)

    def _instance_action(self, action):
        if action == "stop" and not messagebox.askyesno("Stop Instance", "Stop Vast.ai instance now to save credits?", parent=self):
            return

        def _worker():
            if action == "start":
                receiver.start_instance_api()
            elif action == "stop":
                receiver.stop_instance_api()
            self.after(500, self.refresh_instance_async)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------ Polling
    def refresh_instance_async(self):
        def _worker():
            cfg = receiver.load_config()
            if not cfg.get("api_key", "").strip():
                self.after(0, lambda: self._apply_instance_ui(None, "NO API KEY"))
                return
            info = receiver.get_instance_info(auto_discover=True)
            self.after(0, lambda: self._apply_instance_ui(info, None))

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_instance_ui(self, info, err):
        if not self.winfo_exists():
            return
        self.btn_start_inst.pack_forget()
        self.btn_stop_inst.pack_forget()

        if err:
            self.lbl_gpu_badge.configure(text="NO GPU")
            self.lbl_inst_status.configure(text=f"● {err}", text_color="#9ca3af")
            return
        if not info:
            self.lbl_gpu_badge.configure(text="NO GPU")
            self.lbl_inst_status.configure(text="● NO INSTANCE", text_color="#9ca3af")
            return

        iid = str(info.get("id", ""))
        if iid and not self.ent_instance_id.get().strip():
            self.ent_instance_id.insert(0, iid)

        self.lbl_gpu_badge.configure(text=info.get("gpu_name") or "GPU")
        actual = str(info.get("actual_status") or "").lower()
        cur = str(info.get("cur_state") or "").lower()
        msg = str(info.get("status_msg") or "").lower()

        if "scheduling" in actual or "scheduling" in cur or "in use" in msg:
            self.lbl_inst_status.configure(text="● GPU BUSY / SCHEDULING", text_color="#fbbf24")
        elif actual == "running":
            self.lbl_inst_status.configure(text=f"● RUNNING (#{iid})", text_color="#10b981")
            self.btn_stop_inst.pack(side="left", padx=4)
        elif cur == "running" or actual in ("loading", "starting", "creating"):
            self.lbl_inst_status.configure(text="● BOOTING GPU...", text_color="#fbbf24")
        else:
            self.lbl_inst_status.configure(text=f"● STOPPED (#{iid})", text_color="#9ca3af")
            self.btn_start_inst.pack(side="left", padx=4)

    def _periodic_instance_poll(self):
        if self._closing:
            return
        self.refresh_instance_async()
        self.after(15000, self._periodic_instance_poll)

    def _poll_fast(self):
        if self._closing:
            return

        # 1. Drain new log lines
        with receiver.log_lock:
            new_entries = [e for e in receiver.log_buffer if e["id"] > self.last_log_id]
            latest_id = receiver.log_seq

        if new_entries:
            self.txt_console.configure(state="normal")
            for entry in new_entries:
                tag = entry["source"] if entry["source"] in ("blender", "frame", "error", "cost", "system") else "system"
                self.txt_console.insert(tk.END, f"[{entry['ts']}] [{entry['source']}] {entry['text']}\n", tag)
            # Keep console line count bounded
            line_count = int(self.txt_console.index("end-1c").split(".")[0])
            if line_count > 800:
                self.txt_console.delete("1.0", f"{line_count - 800}.0")
            self.txt_console.configure(state="disabled")
            self.last_log_id = latest_id
            if self.var_autoscroll.get():
                self.txt_console.see(tk.END)

        # 2. Update status strip & cost
        t_url = receiver.current_tunnel_url
        self.lbl_tunnel.configure(text=f"Tunnel: {t_url}" if t_url else "Tunnel: Starting Cloudflare tunnel...")
        self.lbl_worker.configure(text=f"Worker: {receiver.worker_status_text} ({receiver.total_frames_received} frames)")

        is_busy = bool(receiver.render_active or receiver.launch_in_progress)
        if is_busy:
            self.lbl_live_pill.configure(text="● LAUNCHING" if receiver.launch_in_progress and not receiver.render_active else "● RENDERING LIVE")
            self.lbl_live_pill.pack(side="left", padx=8)
        else:
            self.lbl_live_pill.pack_forget()

        if receiver.launch_in_progress:
            self.btn_cancel_launch.pack(side="left", padx=(6, 0))
        else:
            self.btn_cancel_launch.pack_forget()

        bt = receiver.vast_boot_time
        elapsed = max(0.0, time.time() - bt) if bt else 0.0
        cost = receiver.compute_cost_metrics(elapsed)
        rate_str = f"${cost['hourly_rate']:.3f}/hr" if receiver.cached_instance_info else "--"
        idr_str = f"{int(round(cost['cost_idr'])):,}".replace(",", ".")
        self.lbl_cost.configure(text=f"Rate: {rate_str}  |  Session: ${cost['cost_usd']:.4f} · Rp {idr_str}")

        self.after(1000, self._poll_fast)

    def _clear_console(self):
        self.txt_console.configure(state="normal")
        self.txt_console.delete("1.0", tk.END)
        self.txt_console.configure(state="disabled")

    # ------------------------------------------------------------------ Jobs & Bounded Frame Viewer
    def _poll_jobs(self):
        if self._closing:
            return
        jobs = []
        if receiver.OUTPUT_DIR.is_dir():
            dirs = [d for d in receiver.OUTPUT_DIR.iterdir() if d.is_dir()]
            dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
            for d in dirs:
                cnt = sum(
                    1 for f in d.iterdir()
                    if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".exr", ".mp4", ".mov") and not f.name.startswith(".tmp_")
                )
                jobs.append((d.name, f"{d.name} ({cnt} frames)"))

        if not jobs:
            self.opt_jobs.configure(values=["No renders yet"])
            self.opt_jobs.set("No renders yet")
            self.current_job = ""
        else:
            labels = [lbl for _, lbl in jobs]
            self.opt_jobs.configure(values=labels)
            matching = [lbl for name, lbl in jobs if name == self.current_job]
            if matching:
                self.opt_jobs.set(matching[0])
            else:
                self.current_job = jobs[0][0]
                self.opt_jobs.set(jobs[0][1])
            self._refresh_frames()

        self.after(3000, self._poll_jobs)

    def _on_job_selected(self, label):
        if label == "No renders yet":
            self.current_job = ""
            self.frames = []
            return
        name = label.rsplit(" (", 1)[0]
        if name != self.current_job:
            self.current_job = name
            self.frames = []
            self.current_frame_index = 0
            self._pause()
            self.frame_cache.clear()
            self._refresh_frames()

    def _refresh_frames(self):
        if not self.current_job:
            return
        job_dir = receiver.OUTPUT_DIR / self.current_job
        if not job_dir.is_dir():
            return
        new_list = sorted(
            f.name for f in job_dir.iterdir()
            if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".exr", ".mp4", ".mov") and not f.name.startswith(".tmp_")
        )
        had_zero = len(self.frames) == 0
        added = len(new_list) > len(self.frames)
        self.frames = new_list

        max_idx = max(1, len(self.frames) - 1)
        self.slider_frame.configure(
            to=max_idx,
            number_of_steps=max_idx,
            state="normal" if len(self.frames) > 1 else "disabled",
        )

        if self.frames:
            if had_zero or (not self.is_playing and added):
                self._show_frame(len(self.frames) - 1)
            else:
                self.lbl_frame_counter.configure(text=f"{self.current_frame_index + 1} / {len(self.frames)}")
        else:
            self.lbl_frame_counter.configure(text="0 / 0")
            self.lbl_active_filename.configure(text="")
            self.btn_open_external.place_forget()
            self._current_photo = None
            self.viewport_label.configure(image="", text="No frames in this folder yet.")

    def _show_frame(self, index):
        if not self.frames:
            return
        index = max(0, min(index, len(self.frames) - 1))
        self.current_frame_index = index
        fname = self.frames[index]
        fpath = receiver.OUTPUT_DIR / self.current_job / fname

        self.slider_frame.set(index)
        self.lbl_frame_counter.configure(text=f"{index + 1} / {len(self.frames)}")
        self.lbl_active_filename.configure(text=fname)

        ext = fpath.suffix.lower()
        if ext in (".mp4", ".mov", ".exr"):
            self._current_photo = None
            self.viewport_label.configure(image="", text=f"{ext.upper()[1:]} file ready:\n{fpath.name}")
            self.btn_open_external.place(relx=0.5, rely=0.6, anchor="center")
            return

        self.btn_open_external.place_forget()
        vw = max(240, self.viewport_frame.winfo_width() - 8)
        vh = max(180, self.viewport_frame.winfo_height() - 8)
        try:
            photo = self.frame_cache.get_photo(fpath, vw, vh)
            self._current_photo = photo
            self.viewport_label.configure(image=photo, text="")
        except Exception as e:
            self._current_photo = None
            self.viewport_label.configure(image="", text=f"Preview unavailable for {fname}: {e}")

    def _on_viewport_resize(self, _event=None):
        if self._resize_after_id is not None:
            self.after_cancel(self._resize_after_id)
        self._resize_after_id = self.after(120, lambda: self._show_frame(self.current_frame_index))

    def _on_scrub(self, val):
        self._pause()
        self._show_frame(int(round(float(val))))

    def _step_frame(self, delta):
        if not self.frames:
            return
        self._pause()
        nxt = (self.current_frame_index + delta) % len(self.frames)
        self._show_frame(nxt)

    def _jump_latest(self):
        if not self.frames:
            return
        self._pause()
        self._show_frame(len(self.frames) - 1)

    def _toggle_play(self):
        if self.is_playing:
            self._pause()
        else:
            self._play()

    def _play(self):
        if not self.frames:
            return
        self.is_playing = True
        self.btn_play.configure(text="⏸")
        self._schedule_next_play_tick()

    def _schedule_next_play_tick(self):
        if not self.is_playing or not self.frames:
            return
        nxt = (self.current_frame_index + 1) % len(self.frames)
        self._show_frame(nxt)
        self._play_after_id = self.after(max(16, int(1000 / self.fps)), self._schedule_next_play_tick)

    def _pause(self):
        self.is_playing = False
        self.btn_play.configure(text="▶")
        if self._play_after_id is not None:
            try:
                self.after_cancel(self._play_after_id)
            except Exception:
                pass
            self._play_after_id = None

    def _on_fps_change(self, val):
        try:
            self.fps = int(val.split()[0])
        except Exception:
            self.fps = 24

    def _open_renders_folder(self):
        target = receiver.OUTPUT_DIR / self.current_job if self.current_job else receiver.OUTPUT_DIR
        if not target.exists():
            target = receiver.OUTPUT_DIR
        try:
            if os.name == "nt":
                os.startfile(str(target))
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(target)])
        except Exception as e:
            messagebox.showerror("Open Folder", str(e), parent=self)

    def _open_current_file_externally(self):
        if not self.current_job or not self.frames:
            return
        target = receiver.OUTPUT_DIR / self.current_job / self.frames[self.current_frame_index]
        if target.is_file():
            try:
                os.startfile(str(target))
            except Exception as e:
                messagebox.showerror("Open File", str(e), parent=self)

    # ------------------------------------------------------------------ Shortcuts & Exit
    def _bind_shortcuts(self):
        def _is_typing():
            w = self.focus_get()
            return w is not None and w.winfo_class() in ("Entry", "Text", "TEntry")

        self.bind("<space>", lambda _e: None if _is_typing() else self._toggle_play())
        self.bind("<Left>", lambda _e: None if _is_typing() else self._step_frame(-1))
        self.bind("<Right>", lambda _e: None if _is_typing() else self._step_frame(1))

    def _on_close(self):
        if receiver.render_active or receiver.launch_in_progress:
            if not messagebox.askyesno(
                "Render Active",
                "A render or launch is currently active. Closing will stop the local receiver and tunnel. Quit anyway?",
                parent=self,
            ):
                return
        self._closing = True
        self._pause()
        self.frame_cache.clear()
        receiver.cleanup_processes(self.worker_server, None)
        self.destroy()


def run_desktop_app(worker_server):
    app = VastRenderHubApp(worker_server=worker_server)
    app.mainloop()

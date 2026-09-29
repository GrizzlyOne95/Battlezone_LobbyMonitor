"""
Battlezone Redux Lobby Monitor
A tool for monitoring and interacting with Battlezone game lobbies.
Supports WebSocket (BZ98R) and RakNet (BZCC) protocols.
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import json
import threading
import time
import sys
import webbrowser
import urllib.request
import re
from io import BytesIO
import socket
import os
import csv
import ctypes
import random
import queue
import importlib.util
import hashlib
import ipaddress
import traceback
import urllib.error
from datetime import datetime, timedelta
import tarfile
import subprocess

from bzr_monitor_utils import (
    aggregate_recent_player_counts,
    build_bzcc_lobby,
    build_discord_message_payload,
    build_lobby_share_text,
    build_steam_join_url,
    clean_lobby_name,
    extract_map_name_from_game_settings,
    extract_map_name_from_metadata,
    format_game_settings_summary,
    is_safe_link,
    list_matches,
    parse_game_settings,
    parse_id_list,
    parse_raknet_frames,
    raknet_frame_header_extra,
    should_relay_discord_message,
)

if sys.platform == "win32":
    import winreg
    import winsound

try:
    import pystray
    from pystray import MenuItem as item

    HAS_TRAY = True
except Exception:
    # pystray raises ValueError (not ImportError) when no tray backend such
    # as GTK is available, e.g. on minimal Linux desktops.
    HAS_TRAY = False

# Try to import pypresence for Discord RPC
try:
    from pypresence import Presence

    HAS_RPC = True
except ImportError:
    HAS_RPC = False

# Try to import websocket-client
try:
    import websocket

    # Ensure we have the correct library (websocket-client) which has WebSocketApp
    if not hasattr(websocket, "WebSocketApp"):
        print(
            "WARNING: Incorrect 'websocket' package detected. Please install 'websocket-client'.",
            file=sys.stderr,
        )
        websocket = None
except ImportError:
    websocket = None

# Try to import PIL for images
try:
    from PIL import Image, ImageTk

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

def _app_dir():
    """Directory next to the script (or the frozen executable).

    Deliberately not the current working directory: when Windows launches the
    app from the Run registry key the CWD is usually System32, and in a
    PyInstaller one-file build __file__ lives in a temp dir deleted on exit.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_dir()
CONFIG_NAME = "bzr_monitor_config.json"
CONFIG_FILE = os.path.join(APP_DIR, CONFIG_NAME)
DEFAULT_GRIEFER_IDS = "S76561198297657246"
TOR_DOWNLOAD_PAGE = "https://www.torproject.org/download/tor/"
HTTP_TIMEOUT = 10

# websocket-client needs python-socks (not pysocks) for SOCKS proxies.
HAS_PYTHON_SOCKS = importlib.util.find_spec("python_socks") is not None

APP_USER_MODEL_ID = "GrizzlyOne95.Battlezone.LobbyMonitor"


def _set_app_user_model_id():
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def _resolve_bundled_icon(name):
    """Locate a bundled icon working from source and under sys._MEIPASS."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "branding", name))
        candidates.append(os.path.join(meipass, name))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "branding", name))
    candidates.append(os.path.join(here, name))
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def apply_window_icon(window):
    """Apply the canonical app icon to a Tk/Toplevel window."""
    try:
        ico_path = _resolve_bundled_icon("app_icon.ico") or _resolve_bundled_icon("bzrmon.ico")
        if ico_path:
            try:
                window.iconbitmap(ico_path)
            except Exception:
                pass
        png_path = _resolve_bundled_icon("app_icon.png")
        if png_path:
            try:
                image = tk.PhotoImage(file=png_path)
                window.iconphoto(True, image)
                window._battlezone_app_icon = image
            except Exception:
                pass
    except Exception:
        pass


def load_tray_image():
    """Load the canonical tray icon, working from source and frozen builds."""
    if not HAS_PIL:
        return None
    for name in ("app_icon.png", "app_icon.ico", "bzrmon.ico"):
        path = _resolve_bundled_icon(name)
        if path:
            try:
                return Image.open(path)
            except Exception:
                continue
    try:
        return Image.new("RGB", (64, 64), color=(0, 255, 0))
    except Exception:
        return None


_set_app_user_model_id()


class BZLobbyMonitor:
    def __init__(self, root):
        self.root = root
        self.root.title("Battlezone Redux Lobby Monitor")
        self.root.geometry("1000x700")
        self.root.minsize(800, 600)

        apply_window_icon(self.root)

        # Every Tk call must happen on the main thread. Worker threads hand
        # work over through this queue (see call_in_ui).
        self.ui_queue = queue.Queue()
        self.root.after(50, self._drain_ui_queue)

        self.lobbies = {}
        self.ws = None
        self.ws_thread = None
        self.connected = False
        self.app_running = True  # lifetime of the app (background loops)
        self.should_run = False  # the user wants a lobby connection
        self.conn_gen = 0  # bumped per connect/disconnect to retire old workers
        self.reconnect_after_id = None
        self.http_poll_inflight = False
        self.stats_after_id = None
        self.stats_loading = False
        self.stats_points = None
        self.current_lobby_id = None
        self.my_id = None
        self.image_cache = {}
        self.pending_fetches = set()
        self.discord_bot_id = None
        self.discord_thread = None
        self.muted_users = set()
        self.geo_cache = {}
        self.geo_pending = set()
        self.geo_failed = {}
        self.image_failed = {}
        self.rpc = None
        self.last_announce_time = time.time()
        self.last_event_announce_time = 0
        self.last_welcome_times = {}
        self.last_claim_attempt = 0
        self.tray_icon = None
        self.tor_process = None
        self.raknet_query = None  # Custom payload for RakNet connected state
        self.tx_rel_seq = -1  # Reliable Message Sequence Number

        self.colors = {}
        self.load_config()
        self.load_custom_fonts()
        self.setup_styles()
        self.setup_ui()
        self.apply_config()

        # Start stats logger if enabled
        if self.config.get("stats_enabled", False):
            self.start_stats_logger()
            self.root.after(500, self.draw_stats)
        self.start_proxy_monitor()
        self.start_bot_loop()
        self.cleanup_logs()

        if HAS_RPC and self.config.get("rpc_enabled", False):
            self.init_rpc()

        if HAS_TRAY:
            self.setup_tray()

        if not websocket:
            messagebox.showerror(
                "Missing Dependency",
                "Please install 'websocket-client' to use this tool.\npip install websocket-client",
            )
            # We don't destroy root immediately to let user see the UI, but disable connect
            self.connect_btn.config(state="disabled")
            self.log("ERROR: 'websocket-client' library not found.")
            self.log("Run: pip install websocket-client")

    def load_config(self):
        self.config = {
            "proxy_enabled": False,
            "proxy_host": "",
            "proxy_port": "",
            "proxy_type": "http",
            "minimize_on_close": False,
            "logging_enabled": False,
            "log_retention": 7,
            "stats_enabled": False,
            "alert_new_lobby": False,
            "alert_player_join": False,
            "alert_disconnect": False,
            "alert_sound": False,
            "alert_flash": False,
            "alert_watch_only": False,
            "watch_list": "",
            "friend_list": "",
            "ban_list": "",
            "alert_griefer": False,
            "run_on_startup": False,
            "filter_locked": False,
            "filter_full": False,
            "log_folder": "",
            "discord_enabled": False,
            "discord_token": "",
            "discord_channel_id": "",
            "discord_lobby_id": "",
            "discord_relay_to_discord": True,
            "discord_relay_to_lobby": True,
            "ip_safety": False,
            "auto_reconnect": False,
            "reconnect_delay": 10,
            "bot_enabled": False,
            "bot_welcome_msg": "Welcome to the lobby, {player}!",
            "bot_welcome_cooldown": 60,
            "bot_announce_enabled": False,
            "bot_announce_msg": "Join our Discord!",
            "bot_announce_interval": 5,
            "bot_event_enabled": False,
            "bot_event_msg": "",
            "bot_event_start": "",
            "bot_event_end": "",
            "bot_event_interval": 10,
            "auto_claim_enabled": False,
            "auto_claim_name": "default",
            "auto_claim_bot_name": "",
            "rpc_enabled": False,
            "rpc_client_id": "",  # Set your Discord Application Client ID from https://discord.com/developers/applications
            "sound_join": "",
            "sound_mention": "",
            "sound_griefer": "",
            "griefer_ids": DEFAULT_GRIEFER_IDS,
        }
        # Older versions kept the config in the working directory.
        for path in (CONFIG_FILE, os.path.abspath(CONFIG_NAME)):
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        self.config.update(loaded)
                    break
                except Exception as e:
                    print(f"Failed to load config {path}: {e}", file=sys.stderr)

    def load_custom_fonts(self):
        self.custom_font_name = "Consolas"
        if sys.platform == "win32":
            # Try to load BZONE.ttf if it exists in the same dir
            base_dir = os.path.dirname(os.path.abspath(__file__))
            font_path = os.path.join(base_dir, "BZONE.ttf")
            if os.path.exists(font_path):
                try:
                    if ctypes.windll.gdi32.AddFontResourceExW(font_path, 0x10, 0) > 0:
                        self.custom_font_name = "BZONE"
                except Exception:
                    pass
        else:
            self.custom_font_name = "Monospace"

    def setup_styles(self):
        self.colors = {
            "bg": "#0a0a0a",
            "fg": "#d4d4d4",
            "highlight": "#00ff00",
            "dark_highlight": "#004400",
            "accent": "#00ffff",
        }
        c = self.colors

        style = ttk.Style()
        style.theme_use("default")

        main_font = (self.custom_font_name, 10)
        bold_font = (self.custom_font_name, 11, "bold")

        style.configure(
            ".",
            background=c["bg"],
            foreground=c["fg"],
            font=main_font,
            bordercolor=c["dark_highlight"],
        )
        style.configure("TFrame", background=c["bg"])
        style.configure("TNotebook", background=c["bg"], borderwidth=0)
        style.configure(
            "TNotebook.Tab", background="#1a1a1a", foreground=c["fg"], padding=[10, 2]
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", c["dark_highlight"])],
            foreground=[("selected", c["highlight"])],
        )
        style.configure("TLabelframe", background=c["bg"], bordercolor=c["highlight"])
        style.configure(
            "TLabelframe.Label",
            background=c["bg"],
            foreground=c["highlight"],
            font=bold_font,
        )
        style.configure("TLabel", background=c["bg"], foreground=c["fg"])
        style.configure(
            "TEntry",
            fieldbackground="#1a1a1a",
            foreground=c["accent"],
            insertcolor=c["highlight"],
        )
        style.configure("TButton", background="#1a1a1a", foreground=c["fg"])
        style.map(
            "TButton",
            background=[("active", c["dark_highlight"])],
            foreground=[("active", c["highlight"])],
        )
        style.configure(
            "TCheckbutton",
            background=c["bg"],
            foreground=c["fg"],
            indicatorcolor="#1a1a1a",
            indicatoron=True,
        )
        style.map("TCheckbutton", indicatorcolor=[("selected", c["highlight"])])
        style.configure(
            "Treeview",
            background="#0a0a0a",
            foreground=c["fg"],
            fieldbackground="#0a0a0a",
            rowheight=25,
        )
        style.map(
            "Treeview",
            background=[("selected", c["accent"])],
            foreground=[("selected", "#000000")],
        )
        style.configure(
            "Treeview.Heading", background="#1a1a1a", foreground=c["fg"], font=bold_font
        )

        self.root.configure(bg=c["bg"])

    def flash_button_text(self, button, text, duration=2000):
        if not button:
            return
        try:
            original_text = button.cget("text")
            button.config(text=text)
            self.root.after(
                duration,
                lambda: (
                    button.config(text=original_text) if button.winfo_exists() else None
                ),
            )
        except Exception:
            pass

    def save_config(self):
        # Write to a temp file and swap it in so a crash mid-write can't
        # leave a truncated config behind.
        tmp_path = CONFIG_FILE + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4)
            os.replace(tmp_path, CONFIG_FILE)
        except Exception as e:
            print(f"Failed to save config: {e}", file=sys.stderr)

    # --- Thread helpers ---
    def call_in_ui(self, fn, *args, delay_ms=0):
        """Run fn(*args) on the Tk main thread. Safe to call from any thread."""
        self.ui_queue.put((fn, args, delay_ms))

    def _drain_ui_queue(self):
        while True:
            try:
                fn, args, delay_ms = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if delay_ms:
                    self.root.after(delay_ms, fn, *args)
                else:
                    fn(*args)
            except Exception:
                traceback.print_exc()
        try:
            self.root.after(50, self._drain_ui_queue)
        except tk.TclError:
            pass  # window destroyed

    def _conn_active(self, gen):
        return self.should_run and gen == self.conn_gen

    @staticmethod
    def _get_int(var, default):
        try:
            return int(var.get())
        except (tk.TclError, ValueError, TypeError):
            return default

    def open_url(self, req, timeout=HTTP_TIMEOUT):
        """urlopen that honours the proxy / IP Safety settings."""
        return self._build_url_opener().open(req, timeout=timeout)

    def _build_url_opener(self):
        cfg = self.config
        host = str(cfg.get("proxy_host", "")).strip()
        port = str(cfg.get("proxy_port", "")).strip()
        if cfg.get("proxy_enabled") and host and port:
            if cfg.get("proxy_type") == "socks5":
                try:
                    import socks
                    import sockshandler
                except ImportError:
                    raise ConnectionError("SOCKS proxy requires 'pysocks'")
                return urllib.request.build_opener(
                    sockshandler.SocksiPyHandler(socks.SOCKS5, host, int(port), rdns=True)
                )
            proxy = f"http://{host}:{port}"
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
        if cfg.get("ip_safety"):
            raise ConnectionError("IP Safety is on but no proxy is configured")
        return urllib.request.build_opener()

    def setup_ui(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)

        self.lobby_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.lobby_tab, text="Lobby Management")

        self.config_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.config_tab, text="Configuration")

        self.discord_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.discord_tab, text="Discord Integration")

        self.bot_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.bot_tab, text="Bot Settings")

        self.stats_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.stats_tab, text="Statistics")

        self.about_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.about_tab, text="About")

        self.setup_lobby_tab()
        self.setup_config_tab()
        self.setup_discord_tab()
        self.setup_bot_tab()
        self.setup_stats_tab()
        self.setup_about_tab()

    def create_scrollable_frame(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)

        canvas = tk.Canvas(frame, bg=self.colors["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        window_id = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _configure_canvas(event):
            canvas.itemconfig(window_id, width=event.width)

        canvas.bind("<Configure>", _configure_canvas)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_mousewheel(event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_mousewheel(event):
            canvas.unbind_all("<MouseWheel>")

        scrollable_frame.bind("<Enter>", _bind_mousewheel)
        scrollable_frame.bind("<Leave>", _unbind_mousewheel)

        return scrollable_frame

    def setup_lobby_tab(self):
        # Top Bar: Connection
        top_frame = ttk.Frame(self.lobby_tab, padding=5)
        top_frame.pack(fill="x")

        ttk.Label(top_frame, text="Game:").pack(side="left", padx=2)
        self.game_var = tk.StringVar()
        self.game_combo = ttk.Combobox(
            top_frame, textvariable=self.game_var, state="readonly", width=28
        )
        self.game_combo["values"] = (
            "Battlezone 98 Redux",
            "Battlezone Combat Commander",
        )
        self.game_combo.current(0)
        self.game_combo.pack(side="left", padx=2)
        self.game_combo.bind("<<ComboboxSelected>>", self.on_game_select)

        ttk.Label(top_frame, text="Name:").pack(side="left", padx=2)
        self.name_var = tk.StringVar(value="BZMonitorUser")
        ttk.Entry(top_frame, textvariable=self.name_var, width=15).pack(
            side="left", padx=2
        )

        ttk.Label(top_frame, text="Host:").pack(side="left", padx=2)
        self.host_var = tk.StringVar(value="battlezone98mp.webdev.rebellion.co.uk:1337")
        ttk.Entry(top_frame, textvariable=self.host_var, width=40).pack(
            side="left", padx=2
        )

        ttk.Label(top_frame, text="Key:").pack(side="left", padx=2)
        self.key_var = tk.StringVar(value="")
        ttk.Entry(top_frame, textvariable=self.key_var, width=20).pack(
            side="left", padx=2
        )

        self.connect_btn = ttk.Button(
            top_frame, text="Connect", command=self.toggle_connection
        )
        self.connect_btn.pack(side="left", padx=5)

        self.status_var = tk.StringVar(value="Disconnected")
        ttk.Label(
            top_frame, textvariable=self.status_var, foreground=self.colors["accent"]
        ).pack(side="right", padx=5)

        self.current_lobby_var = tk.StringVar(value="In Lounge")
        ttk.Label(
            top_frame, textvariable=self.current_lobby_var, foreground="cyan"
        ).pack(side="right", padx=5)

        # Action Bar: Lobby Controls
        action_frame = ttk.Frame(self.lobby_tab, padding=5)
        action_frame.pack(fill="x")

        ttk.Label(action_frame, text="New Lobby:").pack(side="left", padx=2)
        self.new_lobby_var = tk.StringVar(value="MyLobby")
        ttk.Entry(action_frame, textvariable=self.new_lobby_var, width=15).pack(
            side="left", padx=2
        )
        self.create_btn = ttk.Button(
            action_frame, text="Create", command=self.create_lobby
        )
        self.create_btn.pack(side="left", padx=2)

        ttk.Separator(action_frame, orient="vertical").pack(
            side="left", padx=5, fill="y"
        )

        self.join_btn = ttk.Button(
            action_frame, text="Join Selected", command=self.join_selected_lobby
        )
        self.join_btn.pack(side="left", padx=2)
        self.leave_btn = ttk.Button(
            action_frame, text="Refresh Lounge", command=self.leave_or_refresh_lounge
        )
        self.leave_btn.pack(side="left", padx=2)
        self.steam_join_btn = ttk.Button(
            action_frame, text="Join (Steam)", command=self.join_steam_lobby
        )
        self.steam_join_btn.pack(side="left", padx=2)
        self.copy_share_btn = ttk.Button(
            action_frame, text="Copy Share", command=self.copy_lobby_share_text
        )
        self.copy_share_btn.pack(side="left", padx=2)
        self.discord_status_btn = ttk.Button(
            action_frame, text="Post Status (Discord)", command=self.post_lobby_status
        )
        self.discord_status_btn.pack(side="left", padx=2)
        self.ping_btn = ttk.Button(action_frame, text="Ping", command=self.ping_server)
        self.ping_btn.pack(side="left", padx=2)
        self.debug_btn = ttk.Button(
            action_frame, text="RakNet Debug", command=self.open_raknet_debugger
        )
        self.debug_btn.pack(side="left", padx=2)

        # Filters
        ttk.Label(action_frame, text="| Filters:").pack(side="left", padx=5)
        self.filter_locked_var = tk.BooleanVar(
            value=self.config.get("filter_locked", False)
        )
        self.filter_full_var = tk.BooleanVar(
            value=self.config.get("filter_full", False)
        )
        ttk.Checkbutton(
            action_frame,
            text="Hide Locked",
            variable=self.filter_locked_var,
            command=self.refresh_tree,
        ).pack(side="left", padx=2)
        ttk.Checkbutton(
            action_frame,
            text="Hide Full",
            variable=self.filter_full_var,
            command=self.refresh_tree,
        ).pack(side="left", padx=2)

        # Main Content: PanedWindow
        paned = ttk.PanedWindow(self.lobby_tab, orient="vertical")
        paned.pack(fill="both", expand=True, padx=5, pady=5)

        # Top Pane: Lobby List and Waiting Room
        list_pane = ttk.PanedWindow(paned, orient="horizontal")
        paned.add(list_pane, weight=3)

        lobby_frame = ttk.LabelFrame(list_pane, text="Lobbies", padding=5)
        list_pane.add(lobby_frame, weight=4)

        columns = (
            "ID",
            "Name",
            "Map",
            "Owner",
            "Players",
            "Type",
            "Version",
            "Locked",
            "Private",
        )
        self.tree = ttk.Treeview(lobby_frame, columns=columns, show="headings")

        for col in columns:
            self.tree.heading(
                col, text=col, command=lambda c=col: self.sort_tree(c, False)
            )
            self.tree.column(col, width=100)
        self.tree.column("Name", width=200)
        self.tree.column("Map", width=120)
        self.tree.column("ID", width=60)
        self.tree.column("Players", width=60)

        scrollbar = ttk.Scrollbar(
            lobby_frame, orient="vertical", command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<<TreeviewSelect>>", self.on_lobby_select)

        waiting_frame = ttk.LabelFrame(list_pane, text="Waiting Room", padding=5)
        list_pane.add(waiting_frame, weight=1)
        self.waiting_room_text = tk.Text(
            waiting_frame,
            height=8,
            width=26,
            state="disabled",
            bg="#050505",
            fg=self.colors["fg"],
            insertbackground=self.colors["highlight"],
            font=("Consolas", 9),
            wrap="word",
        )
        self.waiting_room_text.pack(fill="both", expand=True)

        # Bottom Pane: Details & Logs
        bottom_pane = ttk.PanedWindow(paned, orient="horizontal")
        paned.add(bottom_pane, weight=2)

        # 1. Lobby Details (Left)
        details_frame = ttk.LabelFrame(bottom_pane, text="Lobby Details", padding=5)
        bottom_pane.add(details_frame, weight=1)

        self.preview_label = ttk.Label(
            details_frame, text="No Preview", anchor="center", background="#000000"
        )
        self.preview_label.pack(side="top", fill="x", pady=(0, 5))

        self.lobby_details_text = tk.Text(
            details_frame,
            height=10,
            width=30,
            state="disabled",
            bg="#050505",
            fg=self.colors["fg"],
            insertbackground=self.colors["highlight"],
            font=("Consolas", 9),
        )
        self.lobby_details_text.pack(fill="both", expand=True)

        # 2. Chat & Logs (Middle)
        log_frame = ttk.LabelFrame(bottom_pane, text="Chat & Logs", padding=5)
        bottom_pane.add(log_frame, weight=2)

        self.log_text = tk.Text(
            log_frame,
            height=10,
            width=40,
            state="disabled",
            bg="#050505",
            fg=self.colors["fg"],
            insertbackground=self.colors["highlight"],
            font=("Consolas", 9),
        )
        self.log_text.pack(fill="both", expand=True)

        chat_input_frame = ttk.Frame(log_frame)
        chat_input_frame.pack(fill="x", pady=2)

        self.chat_var = tk.StringVar()
        self.chat_entry = ttk.Entry(chat_input_frame, textvariable=self.chat_var)
        self.chat_entry.pack(side="left", fill="x", expand=True)
        self.chat_entry.bind("<Return>", self.send_chat)

        self.send_chat_btn = ttk.Button(
            chat_input_frame, text="Send", command=self.send_chat
        )
        self.send_chat_btn.pack(side="right", padx=2)

        # 3. Player Details (Right)
        player_frame = ttk.LabelFrame(bottom_pane, text="Player Details", padding=5)
        bottom_pane.add(player_frame, weight=1)

        self.player_details_text = tk.Text(
            player_frame,
            height=10,
            width=30,
            state="disabled",
            bg="#050505",
            fg=self.colors["fg"],
            insertbackground=self.colors["highlight"],
            font=("Consolas", 9),
        )
        self.player_details_text.pack(fill="both", expand=True)
        self.player_details_text.bind("<Button-3>", self.show_player_context_menu)

        # Configure tags for links
        for widget in [
            self.lobby_details_text,
            self.player_details_text,
            self.log_text,
        ]:
            widget.tag_config("link", foreground=self.colors["accent"], underline=1)
            widget.tag_bind(
                "link", "<Enter>", lambda e, w=widget: w.config(cursor="hand2")
            )
            widget.tag_bind("link", "<Leave>", lambda e, w=widget: w.config(cursor=""))
            widget.tag_bind("link", "<Button-1>", self.on_link_click)
            widget.tag_config("griefer", foreground="red", font=("Segoe UI", 9, "bold"))
            widget.tag_config("host", foreground="#ffcc00", font=("Segoe UI", 9, "bold"))
            widget.tag_config("team_header", foreground=self.colors["highlight"], font=("Segoe UI", 9, "bold"))
            widget.tag_config(
                "friend",
                foreground=self.colors["highlight"],
                font=("Segoe UI", 9, "bold"),
            )

        # Chat specific tags
        self.log_text.tag_config("timestamp", foreground="#888888")
        self.log_text.tag_config(
            "author",
            foreground=self.colors["highlight"],
            font=(self.custom_font_name, 9, "bold"),
        )
        self.log_text.tag_config("mention", foreground="#ffffff", background="#555500")

        # Treeview tags
        self.tree.tag_configure("friend", foreground=self.colors["highlight"])

    def setup_config_tab(self):
        scroll_frame = self.create_scrollable_frame(self.config_tab)
        container = ttk.Frame(scroll_frame, padding=20)
        container.pack(fill="both", expand=True)

        # --- Connection Settings ---
        conn_frame = ttk.LabelFrame(container, text="Connection Settings", padding=10)
        conn_frame.pack(fill="x", pady=5)

        self.auto_reconnect_var = tk.BooleanVar(
            value=self.config.get("auto_reconnect", False)
        )
        ttk.Checkbutton(
            conn_frame,
            text="Auto-Reconnect on Disconnect",
            variable=self.auto_reconnect_var,
            command=self.save_ui_config,
        ).pack(side="left")

        ttk.Label(conn_frame, text="Delay (s):").pack(side="left", padx=(10, 2))
        self.reconnect_delay_var = tk.IntVar(
            value=self.config.get("reconnect_delay", 10)
        )
        ttk.Spinbox(
            conn_frame,
            from_=1,
            to=300,
            textvariable=self.reconnect_delay_var,
            width=5,
            command=self.save_ui_config,
        ).pack(side="left")

        # --- Proxy Settings ---
        proxy_frame = ttk.LabelFrame(
            container, text="Public IP Masking (Proxy)", padding=10
        )
        proxy_frame.pack(fill="x", pady=5)

        self.proxy_enabled_var = tk.BooleanVar(value=self.config["proxy_enabled"])
        ttk.Checkbutton(
            proxy_frame,
            text="Enable Proxy (BZ98R WebSocket + web lookups; BZCC UDP is never proxied)",
            variable=self.proxy_enabled_var,
            command=self.save_ui_config,
        ).pack(anchor="w")

        self.ip_safety_var = tk.BooleanVar(value=self.config.get("ip_safety", False))
        ttk.Checkbutton(
            proxy_frame,
            text="IP Safety (Block connections and lookups that can't use a working proxy)",
            variable=self.ip_safety_var,
            command=self.save_ui_config,
        ).pack(anchor="w")

        p_grid = ttk.Frame(proxy_frame)
        p_grid.pack(fill="x", pady=5)

        ttk.Label(p_grid, text="Host:").pack(side="left")
        self.proxy_host_var = tk.StringVar(value=self.config["proxy_host"])
        ttk.Entry(p_grid, textvariable=self.proxy_host_var, width=20).pack(
            side="left", padx=5
        )

        ttk.Label(p_grid, text="Port:").pack(side="left")
        self.proxy_port_var = tk.StringVar(value=self.config["proxy_port"])
        ttk.Entry(p_grid, textvariable=self.proxy_port_var, width=8).pack(
            side="left", padx=5
        )

        ttk.Button(p_grid, text="Find Free Proxy", command=self.find_free_proxy).pack(
            side="left", padx=10
        )
        ttk.Button(p_grid, text="Use Tor", command=self.set_tor_proxy).pack(
            side="left", padx=5
        )
        ttk.Button(p_grid, text="Test Proxy", command=self.test_proxy).pack(
            side="left", padx=5
        )

        self.tor_status_var = tk.StringVar(value="Tor: Stopped")
        self.tor_status_label = tk.Label(
            p_grid,
            textvariable=self.tor_status_var,
            fg="#666666",
            bg=self.colors["bg"],
            font=("Segoe UI", 9),
        )
        self.tor_status_label.pack(side="left", padx=5)

        ttk.Label(p_grid, text="Status:").pack(side="left", padx=(10, 2))
        self.proxy_status_canvas = tk.Canvas(
            p_grid, width=20, height=20, highlightthickness=0, bg=self.colors["bg"]
        )
        self.proxy_status_canvas.pack(side="left")
        self.proxy_status_light = self.proxy_status_canvas.create_oval(
            4, 4, 16, 16, fill="gray", outline="#666"
        )

        # --- Window Settings ---
        win_frame = ttk.LabelFrame(container, text="Window Behavior", padding=10)
        win_frame.pack(fill="x", pady=5)

        self.min_close_var = tk.BooleanVar(value=self.config["minimize_on_close"])
        ttk.Checkbutton(
            win_frame,
            text="Minimize to Taskbar on Close (Passive Mode)",
            variable=self.min_close_var,
            command=self.apply_config,
        ).pack(anchor="w")
        self.startup_var = tk.BooleanVar(value=self.config.get("run_on_startup", False))
        ttk.Checkbutton(
            win_frame,
            text="Run on Windows Startup",
            variable=self.startup_var,
            command=self.apply_config,
        ).pack(anchor="w")

        ttk.Button(win_frame, text="Quit Application", command=self.quit_app).pack(
            anchor="w", pady=5
        )

        # --- Logging Settings ---
        log_frame = ttk.LabelFrame(container, text="Logging & Analytics", padding=10)
        log_frame.pack(fill="x", pady=5)

        self.log_enabled_var = tk.BooleanVar(value=self.config["logging_enabled"])
        ttk.Checkbutton(
            log_frame,
            text="Enable Chat/Event Logging (File)",
            variable=self.log_enabled_var,
            command=self.save_ui_config,
        ).pack(anchor="w")

        folder_frame = ttk.Frame(log_frame)
        folder_frame.pack(fill="x", pady=2)
        ttk.Label(folder_frame, text="Log Folder:").pack(side="left")
        self.log_folder_var = tk.StringVar(value=self.config.get("log_folder", ""))
        ttk.Entry(folder_frame, textvariable=self.log_folder_var).pack(
            side="left", fill="x", expand=True, padx=5
        )
        ttk.Button(folder_frame, text="Browse", command=self.browse_log_folder).pack(
            side="left"
        )

        ret_frame = ttk.Frame(log_frame)
        ret_frame.pack(fill="x", pady=2)
        ttk.Label(ret_frame, text="Log Retention (Days):").pack(side="left")
        self.log_ret_var = tk.IntVar(value=self.config["log_retention"])
        ttk.Spinbox(
            ret_frame,
            from_=1,
            to=365,
            textvariable=self.log_ret_var,
            width=5,
            command=self.save_ui_config,
        ).pack(side="left", padx=5)

        self.stats_enabled_var = tk.BooleanVar(value=self.config["stats_enabled"])
        ttk.Checkbutton(
            log_frame,
            text="Enable Game Stats Logging (CSV for Charts)",
            variable=self.stats_enabled_var,
            command=self.toggle_stats_logging,
        ).pack(anchor="w", pady=5)

        # --- Alerts Settings ---
        alert_frame = ttk.LabelFrame(
            container, text="Alerts & Notifications", padding=10
        )
        alert_frame.pack(fill="x", pady=5)

        self.alert_new_lobby_var = tk.BooleanVar(
            value=self.config.get("alert_new_lobby", False)
        )
        ttk.Checkbutton(
            alert_frame,
            text="Alert on New Lobby Created",
            variable=self.alert_new_lobby_var,
            command=self.save_ui_config,
        ).pack(anchor="w")

        self.alert_player_join_var = tk.BooleanVar(
            value=self.config.get("alert_player_join", False)
        )
        ttk.Checkbutton(
            alert_frame,
            text="Alert on Player Join (Any Lobby)",
            variable=self.alert_player_join_var,
            command=self.save_ui_config,
        ).pack(anchor="w")

        self.alert_watch_only_var = tk.BooleanVar(
            value=self.config.get("alert_watch_only", False)
        )
        ttk.Checkbutton(
            alert_frame,
            text="Only Alert for Watched Players",
            variable=self.alert_watch_only_var,
            command=self.save_ui_config,
        ).pack(anchor="w", padx=20)

        ttk.Label(alert_frame, text="Watched Players (Name or ID, one per line):").pack(
            anchor="w", padx=20
        )
        self.watch_list_text = tk.Text(
            alert_frame,
            height=4,
            width=40,
            bg="#1a1a1a",
            fg=self.colors["accent"],
            insertbackground=self.colors["highlight"],
            font=("Consolas", 9),
        )
        self.watch_list_text.pack(fill="x", padx=20, pady=(0, 5))
        self.watch_list_text.insert("1.0", self.config.get("watch_list", ""))

        self.alert_griefer_var = tk.BooleanVar(
            value=self.config.get("alert_griefer", False)
        )
        ttk.Checkbutton(
            alert_frame,
            text="Alert on Known Griefers (IDs under Security & Auto-Ban)",
            variable=self.alert_griefer_var,
            command=self.save_ui_config,
        ).pack(anchor="w")

        self.alert_disconnect_var = tk.BooleanVar(
            value=self.config.get("alert_disconnect", False)
        )
        ttk.Checkbutton(
            alert_frame,
            text="Alert on Connection Lost",
            variable=self.alert_disconnect_var,
            command=self.save_ui_config,
        ).pack(anchor="w")

        opts_frame = ttk.Frame(alert_frame)
        opts_frame.pack(fill="x", pady=5)
        self.alert_sound_var = tk.BooleanVar(
            value=self.config.get("alert_sound", False)
        )
        ttk.Checkbutton(
            opts_frame,
            text="Play Sound",
            variable=self.alert_sound_var,
            command=self.save_ui_config,
        ).pack(side="left", padx=(0, 10))
        self.alert_flash_var = tk.BooleanVar(
            value=self.config.get("alert_flash", False)
        )
        ttk.Checkbutton(
            opts_frame,
            text="Flash Window",
            variable=self.alert_flash_var,
            command=self.save_ui_config,
        ).pack(side="left")

        # --- Social & Audio ---
        social_frame = ttk.LabelFrame(container, text="Social & Audio", padding=10)
        social_frame.pack(fill="x", pady=5)

        ttk.Label(social_frame, text="Friend List (Name or ID, one per line):").pack(
            anchor="w"
        )
        self.friend_list_text = tk.Text(
            social_frame,
            height=4,
            width=40,
            bg="#1a1a1a",
            fg=self.colors["highlight"],
            insertbackground=self.colors["highlight"],
            font=("Consolas", 9),
        )
        self.friend_list_text.pack(fill="x", pady=(0, 5))
        self.friend_list_text.insert("1.0", self.config.get("friend_list", ""))

        # --- Security ---
        sec_frame = ttk.LabelFrame(container, text="Security & Auto-Ban", padding=10)
        sec_frame.pack(fill="x", pady=5)
        ttk.Label(
            sec_frame, text="Auto-Ban List (Name, ID, or IP - one per line):"
        ).pack(anchor="w")
        self.ban_list_text = tk.Text(
            sec_frame,
            height=4,
            width=40,
            bg="#1a1a1a",
            fg="#ff5555",
            insertbackground=self.colors["highlight"],
            font=("Consolas", 9),
        )
        self.ban_list_text.pack(fill="x", pady=(0, 5))
        self.ban_list_text.insert("1.0", self.config.get("ban_list", ""))

        ttk.Label(
            sec_frame, text="Known Griefer IDs (for griefer alerts - one per line):"
        ).pack(anchor="w")
        self.griefer_ids_text = tk.Text(
            sec_frame,
            height=3,
            width=40,
            bg="#1a1a1a",
            fg="#ff5555",
            insertbackground=self.colors["highlight"],
            font=("Consolas", 9),
        )
        self.griefer_ids_text.pack(fill="x", pady=(0, 5))
        self.griefer_ids_text.insert("1.0", self.config.get("griefer_ids", ""))

        ttk.Label(social_frame, text="Custom Audio Alerts (.wav):").pack(
            anchor="w", pady=(5, 0)
        )

        def browse_wav(var):
            f = filedialog.askopenfilename(filetypes=[("WAV Audio", "*.wav")])
            if f:
                var.set(f)
            self.save_ui_config()

        for lbl, key in [
            ("Player Join:", "sound_join"),
            ("Chat Mention:", "sound_mention"),
            ("Griefer:", "sound_griefer"),
        ]:
            f = ttk.Frame(social_frame)
            f.pack(fill="x", pady=1)
            ttk.Label(f, text=lbl, width=15).pack(side="left")
            var = tk.StringVar(value=self.config.get(key, ""))
            setattr(self, f"{key}_var", var)
            ttk.Entry(f, textvariable=var).pack(
                side="left", fill="x", expand=True, padx=5
            )
            ttk.Button(
                f, text="...", width=3, command=lambda v=var: browse_wav(v)
            ).pack(side="left")

    def setup_discord_tab(self):
        container = ttk.Frame(self.discord_tab, padding=20)
        container.pack(fill="both", expand=True)

        # Settings
        settings_frame = ttk.LabelFrame(container, text="Discord Settings", padding=10)
        settings_frame.pack(fill="x", pady=5)

        self.discord_enabled_var = tk.BooleanVar(
            value=self.config.get("discord_enabled", False)
        )
        ttk.Checkbutton(
            settings_frame,
            text="Enable Discord Relay",
            variable=self.discord_enabled_var,
            command=self.toggle_discord_relay,
        ).pack(anchor="w")

        grid = ttk.Frame(settings_frame)
        grid.pack(fill="x", pady=5)

        ttk.Label(grid, text="Bot Token:").grid(
            row=0, column=0, sticky="w", padx=5, pady=2
        )
        self.discord_token_var = tk.StringVar(
            value=self.config.get("discord_token", "")
        )
        ttk.Entry(grid, textvariable=self.discord_token_var, width=50, show="*").grid(
            row=0, column=1, sticky="w", padx=5, pady=2
        )

        ttk.Label(grid, text="Channel ID:").grid(
            row=1, column=0, sticky="w", padx=5, pady=2
        )
        self.discord_channel_id_var = tk.StringVar(
            value=self.config.get("discord_channel_id", "")
        )
        ttk.Entry(grid, textvariable=self.discord_channel_id_var, width=20).grid(
            row=1, column=1, sticky="w", padx=5, pady=2
        )

        ttk.Label(grid, text="Lobby ID to Relay:").grid(
            row=2, column=0, sticky="w", padx=5, pady=2
        )
        self.discord_lobby_id_var = tk.StringVar(
            value=self.config.get("discord_lobby_id", "")
        )
        ttk.Entry(grid, textvariable=self.discord_lobby_id_var, width=20).grid(
            row=2, column=1, sticky="w", padx=5, pady=2
        )

        # Options
        opts_frame = ttk.LabelFrame(container, text="Relay Options", padding=10)
        opts_frame.pack(fill="x", pady=5)

        self.discord_to_discord_var = tk.BooleanVar(
            value=self.config.get("discord_relay_to_discord", True)
        )
        ttk.Checkbutton(
            opts_frame,
            text="Relay Lobby Chat -> Discord",
            variable=self.discord_to_discord_var,
            command=self.save_ui_config,
        ).pack(anchor="w")

        self.discord_to_lobby_var = tk.BooleanVar(
            value=self.config.get("discord_relay_to_lobby", True)
        )
        ttk.Checkbutton(
            opts_frame,
            text="Relay Discord Chat -> Lobby",
            variable=self.discord_to_lobby_var,
            command=self.save_ui_config,
        ).pack(anchor="w")

        btn_frame = ttk.Frame(container)
        btn_frame.pack(fill="x", pady=10)
        ttk.Button(
            btn_frame,
            text="Test Discord Connection",
            command=self.test_discord_connection,
        ).pack(side="left")
        ttk.Button(
            btn_frame, text="Save Discord Config", command=self.save_ui_config
        ).pack(side="left", padx=10)

    def setup_bot_tab(self):
        scroll_frame = self.create_scrollable_frame(self.bot_tab)
        container = ttk.Frame(scroll_frame, padding=20)
        container.pack(fill="both", expand=True)

        # Auto-Greeter
        greet_frame = ttk.LabelFrame(container, text="Auto-Greeter", padding=10)
        greet_frame.pack(fill="x", pady=5)

        self.bot_enabled_var = tk.BooleanVar(
            value=self.config.get("bot_enabled", False)
        )
        ttk.Checkbutton(
            greet_frame,
            text="Enable Auto-Welcome (When hosting/present)",
            variable=self.bot_enabled_var,
            command=self.save_ui_config,
        ).pack(anchor="w")

        ttk.Label(greet_frame, text="Message ({player} = username):").pack(
            anchor="w", pady=(5, 0)
        )
        self.bot_welcome_var = tk.StringVar(
            value=self.config.get("bot_welcome_msg", "")
        )
        ttk.Entry(greet_frame, textvariable=self.bot_welcome_var, width=60).pack(
            fill="x", pady=2
        )

        cd_frame = ttk.Frame(greet_frame)
        cd_frame.pack(fill="x", pady=2)
        ttk.Label(cd_frame, text="Cooldown (s):").pack(side="left")
        self.bot_welcome_cooldown_var = tk.IntVar(
            value=self.config.get("bot_welcome_cooldown", 60)
        )
        ttk.Spinbox(
            cd_frame,
            from_=0,
            to=3600,
            textvariable=self.bot_welcome_cooldown_var,
            width=5,
        ).pack(side="left", padx=5)

        # Announcements
        ann_frame = ttk.LabelFrame(container, text="Timed Announcements", padding=10)
        ann_frame.pack(fill="x", pady=5)

        self.bot_announce_enabled_var = tk.BooleanVar(
            value=self.config.get("bot_announce_enabled", False)
        )
        ttk.Checkbutton(
            ann_frame,
            text="Enable Timed Announcements",
            variable=self.bot_announce_enabled_var,
            command=self.save_ui_config,
        ).pack(anchor="w")

        ttk.Label(ann_frame, text="Message:").pack(anchor="w", pady=(5, 0))
        self.bot_announce_msg_var = tk.StringVar(
            value=self.config.get("bot_announce_msg", "")
        )
        ttk.Entry(ann_frame, textvariable=self.bot_announce_msg_var, width=60).pack(
            fill="x", pady=2
        )

        ttk.Label(ann_frame, text="Interval (Minutes):").pack(anchor="w", pady=(5, 0))
        self.bot_announce_int_var = tk.IntVar(
            value=self.config.get("bot_announce_interval", 5)
        )
        ttk.Spinbox(
            ann_frame, from_=1, to=120, textvariable=self.bot_announce_int_var, width=5
        ).pack(anchor="w", pady=2)

        # Timed Events
        event_frame = ttk.LabelFrame(container, text="Timed Event Messages", padding=10)
        event_frame.pack(fill="x", pady=5)

        self.bot_event_enabled_var = tk.BooleanVar(
            value=self.config.get("bot_event_enabled", False)
        )
        ttk.Checkbutton(
            event_frame,
            text="Enable Event Messages",
            variable=self.bot_event_enabled_var,
            command=self.save_ui_config,
        ).pack(anchor="w")

        ttk.Label(event_frame, text="Message:").pack(anchor="w", pady=(5, 0))
        self.bot_event_msg_var = tk.StringVar(
            value=self.config.get("bot_event_msg", "")
        )
        ttk.Entry(event_frame, textvariable=self.bot_event_msg_var, width=60).pack(
            fill="x", pady=2
        )

        date_frame = ttk.Frame(event_frame)
        date_frame.pack(fill="x", pady=2)

        ttk.Label(date_frame, text="Start (YYYY-MM-DD HH:MM):").pack(side="left")
        self.bot_event_start_var = tk.StringVar(
            value=self.config.get("bot_event_start", "")
        )
        ttk.Entry(date_frame, textvariable=self.bot_event_start_var, width=18).pack(
            side="left", padx=5
        )

        ttk.Label(date_frame, text="End:").pack(side="left")
        self.bot_event_end_var = tk.StringVar(
            value=self.config.get("bot_event_end", "")
        )
        ttk.Entry(date_frame, textvariable=self.bot_event_end_var, width=18).pack(
            side="left", padx=5
        )

        ttk.Label(date_frame, text="Int (m):").pack(side="left")
        self.bot_event_int_var = tk.IntVar(
            value=self.config.get("bot_event_interval", 10)
        )
        ttk.Spinbox(
            date_frame, from_=1, to=1440, textvariable=self.bot_event_int_var, width=5
        ).pack(side="left", padx=5)

        # Auto-Claim
        claim_frame = ttk.LabelFrame(container, text="Auto-Claim Lobby", padding=10)
        claim_frame.pack(fill="x", pady=5)

        self.auto_claim_enabled_var = tk.BooleanVar(
            value=self.config.get("auto_claim_enabled", False)
        )
        ttk.Checkbutton(
            claim_frame,
            text="Enable Auto-Claim (Recreate if missing)",
            variable=self.auto_claim_enabled_var,
            command=self.save_ui_config,
        ).pack(anchor="w")

        ttk.Label(claim_frame, text="Lobby Name:").pack(side="left")
        self.auto_claim_name_var = tk.StringVar(
            value=self.config.get("auto_claim_name", "default")
        )
        ttk.Entry(claim_frame, textvariable=self.auto_claim_name_var, width=20).pack(
            side="left", padx=5
        )

        ttk.Label(claim_frame, text="Bot Name:").pack(side="left", padx=(5, 0))
        self.auto_claim_bot_name_var = tk.StringVar(
            value=self.config.get("auto_claim_bot_name", "")
        )
        ttk.Entry(
            claim_frame, textvariable=self.auto_claim_bot_name_var, width=15
        ).pack(side="left", padx=5)

        # RPC Settings
        rpc_frame = ttk.LabelFrame(
            container, text="Discord Rich Presence (Local)", padding=10
        )
        rpc_frame.pack(fill="x", pady=5)

        self.rpc_enabled_var = tk.BooleanVar(
            value=self.config.get("rpc_enabled", False)
        )
        ttk.Checkbutton(
            rpc_frame,
            text="Enable Rich Presence",
            variable=self.rpc_enabled_var,
            command=self.toggle_rpc,
        ).pack(anchor="w")

        ttk.Label(
            rpc_frame,
            text="Client ID (from discord.com/developers/applications — required):",
        ).pack(anchor="w")
        self.rpc_id_var = tk.StringVar(
            value=self.config.get("rpc_client_id", "133570000000000000")
        )
        ttk.Entry(rpc_frame, textvariable=self.rpc_id_var).pack(fill="x", pady=2)

        ttk.Button(
            container, text="Save Bot Settings", command=self.save_ui_config
        ).pack(pady=10)

    def setup_stats_tab(self):
        container = ttk.Frame(self.stats_tab, padding=10)
        container.pack(fill="both", expand=True)

        ctrl = ttk.Frame(container)
        ctrl.pack(fill="x", pady=5)
        ttk.Button(ctrl, text="Refresh Graph", command=self.draw_stats).pack(
            side="left"
        )
        ttk.Label(ctrl, text="Active Players (Last 24h)").pack(side="left", padx=10)

        self.stats_canvas = tk.Canvas(container, bg="#1a1a1a", highlightthickness=0)
        self.stats_canvas.pack(fill="both", expand=True)
        # Resizes just redraw the cached points; the file is read on demand.
        self.stats_canvas.bind("<Configure>", lambda e: self._render_stats())

    def setup_about_tab(self):
        container = ttk.Frame(self.about_tab, padding=40)
        container.pack(fill="both", expand=True)

        # Title
        ttk.Label(
            container,
            text="Battlezone Redux Lobby Monitor",
            font=(self.custom_font_name, 20, "bold"),
            foreground=self.colors["highlight"],
        ).pack(pady=(0, 30))

        # Disclaimer Box
        info_frame = ttk.LabelFrame(container, text=" Disclaimer ", padding=20)
        info_frame.pack(fill="both", expand=True)

        disclaimer_text = (
            "This tool is created solely for the purpose of helping the community organize games "
            "and monitor lobby status. It is intended to facilitate fair play and coordination.\n\n"
            "It should NOT be used to harass players, disrupt lobbies, or cause problems within the community. "
            "Please use this tool responsibly.\n\n"
            "This application is NOT affiliated with, endorsed by, or connected to Rebellion Developments "
            "in any way. It simply leverages publicly available WebSocket protocols used by the game client."
        )

        lbl = ttk.Label(
            info_frame,
            text=disclaimer_text,
            font=(self.custom_font_name, 11),
            wraplength=600,
            justify="center",
        )
        lbl.pack(expand=True)

        ttk.Label(
            container,
            text="Developed by the Community",
            font=(self.custom_font_name, 9, "italic"),
            foreground="gray",
        ).pack(side="bottom", pady=10)

    def sort_tree(self, col, reverse):
        l = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
        try:
            l.sort(key=lambda t: int(t[0]) if t[0].isdigit() else t[0], reverse=reverse)
        except ValueError:
            l.sort(reverse=reverse)

        for index, (val, k) in enumerate(l):
            self.tree.move(k, '', index)

        self.tree.heading(col, command=lambda: self.sort_tree(col, not reverse))

    def save_ui_config(self):
        self.config["proxy_enabled"] = self.proxy_enabled_var.get()
        self.config["ip_safety"] = self.ip_safety_var.get()
        self.config["auto_reconnect"] = self.auto_reconnect_var.get()
        self.config["reconnect_delay"] = self._get_int(self.reconnect_delay_var, 10)
        self.config["proxy_host"] = self.proxy_host_var.get()
        self.config["proxy_port"] = self.proxy_port_var.get()
        # proxy_type is managed by buttons, not directly exposed in this UI save
        self.config["minimize_on_close"] = self.min_close_var.get()
        self.config["logging_enabled"] = self.log_enabled_var.get()
        self.config["log_retention"] = self._get_int(self.log_ret_var, 7)
        self.config["stats_enabled"] = self.stats_enabled_var.get()
        self.config["alert_new_lobby"] = self.alert_new_lobby_var.get()
        self.config["alert_player_join"] = self.alert_player_join_var.get()
        self.config["alert_disconnect"] = self.alert_disconnect_var.get()
        self.config["alert_sound"] = self.alert_sound_var.get()
        self.config["alert_flash"] = self.alert_flash_var.get()
        self.config["alert_watch_only"] = self.alert_watch_only_var.get()
        self.config["watch_list"] = self.watch_list_text.get("1.0", "end-1c")
        self.config["friend_list"] = self.friend_list_text.get("1.0", "end-1c")
        self.config["ban_list"] = self.ban_list_text.get("1.0", "end-1c")
        self.config["griefer_ids"] = self.griefer_ids_text.get("1.0", "end-1c")
        self.config["sound_join"] = self.sound_join_var.get()
        self.config["sound_mention"] = self.sound_mention_var.get()
        self.config["sound_griefer"] = self.sound_griefer_var.get()
        self.config["alert_griefer"] = self.alert_griefer_var.get()
        self.config["run_on_startup"] = self.startup_var.get()
        self.config["filter_locked"] = self.filter_locked_var.get()
        self.config["filter_full"] = self.filter_full_var.get()
        self.config["log_folder"] = self.log_folder_var.get()
        self.config["discord_enabled"] = self.discord_enabled_var.get()
        self.config["discord_token"] = self.discord_token_var.get()
        self.config["discord_channel_id"] = self.discord_channel_id_var.get()
        self.config["discord_lobby_id"] = self.discord_lobby_id_var.get()
        self.config["discord_relay_to_discord"] = self.discord_to_discord_var.get()
        self.config["discord_relay_to_lobby"] = self.discord_to_lobby_var.get()
        self.config["bot_enabled"] = self.bot_enabled_var.get()
        self.config["bot_welcome_msg"] = self.bot_welcome_var.get()
        self.config["bot_welcome_cooldown"] = self._get_int(self.bot_welcome_cooldown_var, 60)
        self.config["bot_announce_enabled"] = self.bot_announce_enabled_var.get()
        self.config["bot_announce_msg"] = self.bot_announce_msg_var.get()
        self.config["bot_announce_interval"] = self._get_int(self.bot_announce_int_var, 5)
        self.config["bot_event_enabled"] = self.bot_event_enabled_var.get()
        self.config["bot_event_msg"] = self.bot_event_msg_var.get()
        self.config["bot_event_start"] = self.bot_event_start_var.get()
        self.config["bot_event_end"] = self.bot_event_end_var.get()
        self.config["bot_event_interval"] = self._get_int(self.bot_event_int_var, 10)
        self.config["auto_claim_enabled"] = self.auto_claim_enabled_var.get()
        self.config["auto_claim_name"] = self.auto_claim_name_var.get()
        self.config["auto_claim_bot_name"] = self.auto_claim_bot_name_var.get()
        self.config["rpc_enabled"] = self.rpc_enabled_var.get()
        self.config["rpc_client_id"] = self.rpc_id_var.get()
        self.save_config()

    def browse_log_folder(self):
        d = filedialog.askdirectory()
        if d:
            self.log_folder_var.set(d)
            self.save_ui_config()

    def apply_config(self):
        self.save_ui_config()
        if self.config["minimize_on_close"]:
            self.root.protocol("WM_DELETE_WINDOW", self.on_window_close_attempt)
        else:
            self.root.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.set_startup(self.config["run_on_startup"])
        self.toggle_discord_relay()

    def set_startup(self, enable):
        if sys.platform == "win32":
            try:
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                    0,
                    winreg.KEY_ALL_ACCESS,
                )
                if enable:
                    if getattr(sys, "frozen", False):
                        path = f'"{sys.executable}"'
                    else:
                        path = f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'
                    winreg.SetValueEx(key, "BZLobbyMonitor", 0, winreg.REG_SZ, path)
                else:
                    try:
                        winreg.DeleteValue(key, "BZLobbyMonitor")
                    except FileNotFoundError:
                        pass
                winreg.CloseKey(key)
            except Exception as e:
                self.log(f"Startup registry error: {e}")
        elif sys.platform.startswith("linux"):
            try:
                autostart_dir = os.path.expanduser("~/.config/autostart")
                desktop_file = os.path.join(autostart_dir, "bzr_monitor.desktop")
                if enable:
                    if not os.path.exists(autostart_dir):
                        os.makedirs(autostart_dir)
                    exec_cmd = (
                        f'"{sys.executable}"'
                        if getattr(sys, "frozen", False)
                        else f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'
                    )
                    content = f"[Desktop Entry]\nType=Application\nName=Battlezone Lobby Monitor\nExec={exec_cmd}\nHidden=false\nNoDisplay=false\nX-GNOME-Autostart-enabled=true\nComment=Start BZR Monitor\n"
                    with open(desktop_file, "w") as f:
                        f.write(content)
                else:
                    if os.path.exists(desktop_file):
                        os.remove(desktop_file)
            except Exception as e:
                self.log(f"Startup setup error: {e}")

    def on_window_close_attempt(self):
        self.root.iconify()
        if HAS_TRAY and self.tray_icon:
            self.root.withdraw()
            # Tray icon runs in background thread, no action needed

    def trigger_alert(self, alert_type, data=None):
        should_alert = False
        sound_key = None

        if alert_type == "new_lobby" and self.config.get("alert_new_lobby"):
            should_alert = True
        elif alert_type == "player_join" and self.config.get("alert_player_join"):
            sound_key = "sound_join"
            if self.config.get("alert_watch_only"):
                watch_list = self.config.get("watch_list", "").lower().splitlines()
                # data is player name
                if data and any(
                    w.strip() in str(data).lower() for w in watch_list if w.strip()
                ):
                    should_alert = True
            else:
                should_alert = True
        elif alert_type == "griefer_join" and self.config.get("alert_griefer"):
            should_alert = True
            sound_key = "sound_griefer"
        elif alert_type == "disconnect" and self.config.get("alert_disconnect"):
            should_alert = True

        if should_alert:
            if self.config.get("alert_sound"):
                self.root.bell()
            if self.config.get("alert_sound"):
                self.play_custom_sound(sound_key)
            if self.config.get("alert_flash"):
                self.flash_window()

    def play_custom_sound(self, config_key):
        played = False
        if config_key:
            path = self.config.get(config_key, "")
            if path and os.path.exists(path) and sys.platform == "win32":
                try:
                    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                    played = True
                except Exception:
                    pass

        if not played:
            self.root.bell()

    def flash_window(self):
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.FlashWindow(int(self.root.wm_frame(), 16), True)
            except Exception:
                pass
        else:
            try:
                self.root.wm_attributes("-demands-attention", True)
            except Exception:
                pass

    def quit_app(self):
        self.app_running = False
        self.should_run = False
        self.conn_gen += 1
        if self.tray_icon:
            self.tray_icon.stop()
        if self.tor_process:
            self.stop_tor()
        self.save_config()
        self.root.destroy()
        sys.exit(0)

    def on_game_select(self, event):
        game = self.game_var.get()
        if game == "Battlezone 98 Redux":
            self.host_var.set("battlezone98mp.webdev.rebellion.co.uk:1337")
        elif game == "Battlezone Combat Commander":
            self.host_var.set("battlezone99mp.webdev.rebellion.co.uk:61111")

    def setup_tray(self):
        if not HAS_TRAY:
            return

        def show_window(icon, item):
            self.call_in_ui(self.root.deiconify)

        def quit_tray(icon, item):
            self.call_in_ui(self.quit_app)

        image = None
        if HAS_PIL:
            # Canonical Lobby Monitor artwork for the system tray.
            try:
                image = load_tray_image()
            except Exception:
                pass

        if image:
            menu = (item("Show", show_window), item("Quit", quit_tray))
            self.tray_icon = pystray.Icon("name", image, "BZ Monitor", menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def create_lobby(self, name=None):
        if not self.connected:
            self.flash_button_text(self.create_btn, "Not Connected")
            return

        if name is None:
            name = self.new_lobby_var.get()
        if not name:
            self.flash_button_text(self.create_btn, "Enter Name")
            return

        # Add prefix for chat lobbies as per JS client
        full_name = f"~chat~pub~~{name}"
        msg = {
            "type": "CreateLobby",
            "content": {
                "name": full_name,
                "isPrivate": False,
                "memberLimit": 20000,
                "password": "",
            },
        }
        if self.ws and self.connected:
            self.ws.send(json.dumps(msg))
            self.log(f"Requesting Create Lobby: {name}")

    def join_selected_lobby(self):
        if not self.connected:
            self.flash_button_text(self.join_btn, "Not Connected")
            return

        selected_items = self.tree.selection()
        if not selected_items:
            self.flash_button_text(self.join_btn, "Select Lobby")
            return

        # Prevent joining a lobby if already in one
        if self.current_lobby_id is not None:
            self.flash_button_text(self.join_btn, "Already in Lobby")
            return

        val = self.tree.item(selected_items[0])["values"][0]
        try:
            lid = int(val)
        except ValueError:
            lid = str(val)

        msg = {"type": "DoJoinLobby", "content": {"id": lid, "password": ""}}
        if self.ws and self.connected:
            self.ws.send(json.dumps(msg))
            self.log(f"Requesting Join Lobby: {lid}")

    def get_selected_lobby(self):
        selected_items = self.tree.selection()
        if not selected_items:
            return None, None

        lid = str(self.tree.item(selected_items[0])["values"][0])
        return lid, self.lobbies.get(lid)

    def join_steam_lobby(self):
        lid, lobby = self.get_selected_lobby()
        if not lid:
            self.flash_button_text(self.steam_join_btn, "Select Lobby")
            return

        if not lobby:
            return

        url = build_steam_join_url(lid, lobby)
        self.log(f"Opening Steam URL: {url}")
        webbrowser.open(url)

    def copy_lobby_share_text(self):
        lid, lobby = self.get_selected_lobby()
        if not lid:
            self.flash_button_text(self.copy_share_btn, "Select Lobby")
            return
        if not lobby:
            return

        share_text = build_lobby_share_text(lid, lobby)
        self.root.clipboard_clear()
        self.root.clipboard_append(share_text)
        self.root.update()
        self.flash_button_text(self.copy_share_btn, "Copied")
        self.log(f"Copied share text: {share_text}")

    def show_player_context_menu(self, event):
        try:
            index = self.player_details_text.index(f"@{event.x},{event.y}")
            line = self.player_details_text.get(
                f"{index} linestart", f"{index} lineend"
            )
            match = re.search(r" - (.*?) \(ID: (.*?)\)", line)
            if match:
                name = match.group(1)
                uid = match.group(2)
                menu = tk.Menu(self.root, tearoff=0)
                menu.add_command(
                    label=f"Add '{name}' to Watch List",
                    command=lambda: self.add_to_watch_list(name),
                )
                menu.add_command(
                    label=f"Add ID '{uid}' to Watch List",
                    command=lambda: self.add_to_watch_list(uid),
                )

                menu.add_command(
                    label=f"Add '{name}' to Friends",
                    command=lambda: self.add_to_friend_list(name),
                )
                menu.add_command(
                    label=f"Add '{name}' to Ban List",
                    command=lambda: self.add_to_ban_list(name),
                )

                menu.add_separator()
                menu.add_command(
                    label=f"Whisper '{name}'", command=lambda: self.whisper_user(name)
                )

                if uid in self.muted_users:
                    menu.add_command(
                        label=f"Unmute '{name}'",
                        command=lambda: self.toggle_mute(uid, name),
                    )
                else:
                    menu.add_command(
                        label=f"Mute '{name}'",
                        command=lambda: self.toggle_mute(uid, name),
                    )

                # Check ownership
                if self.current_lobby_id is not None:
                    lobby = self.lobbies.get(str(self.current_lobby_id))
                    if lobby:
                        owner = str(lobby.get("owner"))
                        if str(self.my_id) == owner and str(self.my_id) != str(uid):
                            menu.add_separator()
                            menu.add_command(
                                label=f"Kick '{name}'",
                                command=lambda: self.kick_user(uid, name),
                            )

                menu.post(event.x_root, event.y_root)
        except Exception as e:
            print(f"Context menu error: {e}")

    def add_to_watch_list(self, text):
        current = self.watch_list_text.get("1.0", "end-1c")
        new_text = (current + "\n" + text).strip()
        self.watch_list_text.delete("1.0", "end")
        self.watch_list_text.insert("1.0", new_text)
        self.save_ui_config()
        messagebox.showinfo("Watch List", f"Added '{text}' to watch list.")

    def add_to_friend_list(self, text):
        current = self.friend_list_text.get("1.0", "end-1c")
        new_text = (current + "\n" + text).strip()
        self.friend_list_text.delete("1.0", "end")
        self.friend_list_text.insert("1.0", new_text)
        self.save_ui_config()
        self.refresh_tree()

    def add_to_ban_list(self, text):
        current = self.ban_list_text.get("1.0", "end-1c")
        new_text = (current + "\n" + text).strip()
        self.ban_list_text.delete("1.0", "end")
        self.ban_list_text.insert("1.0", new_text)
        self.save_ui_config()

    def whisper_user(self, name):
        if not (self.ws and self.connected):
            self.log("Whisper failed: not connected.")
            return
        msg = simpledialog.askstring("Whisper", f"Message to {name}:")
        if msg and self.ws and self.connected:
            # BZ98R uses /t for tell/whisper usually
            self.ws.send(
                json.dumps({"type": "DoSendChat", "content": f"/t {name} {msg}"})
            )
            self.log(f"[Whisper to {name}]: {msg}")

    def toggle_mute(self, uid, name):
        uid = str(uid)
        if uid in self.muted_users:
            self.muted_users.remove(uid)
            self.log(f"Unmuted {name}")
        else:
            self.muted_users.add(uid)
            self.log(f"Muted {name}")

    def kick_user(self, uid, name):
        if not (self.ws and self.connected):
            self.log("Kick failed: not connected.")
            return
        if messagebox.askyesno("Kick User", f"Are you sure you want to kick {name}?"):
            self.ws.send(
                json.dumps(
                    {
                        "type": "DoKickUser",
                        "content": int(uid) if str(uid).isdigit() else uid,
                    }
                )
            )
            self.log(f"Kicked {name}")

    def leave_or_refresh_lounge(self):
        if not self.connected:
            self.flash_button_text(self.leave_btn, "Not Connected")
            return

        if self.current_lobby_id is not None:
            msg = {"type": "DoExitLobby", "content": self._server_lobby_id(self.current_lobby_id)}
            self.log(f"Requesting Exit Lobby: {self.current_lobby_id}")
        else:
            msg = {"type": "DoEnterLounge", "content": True}
            self.log("Requesting Refresh Lounge...")

        if self.ws and self.connected:
            self.ws.send(json.dumps(msg))

    def send_chat(self, event=None):
        if not self.connected:
            if hasattr(self, "send_chat_btn"):
                self.flash_button_text(self.send_chat_btn, "Not Connected")
            return

        text = self.chat_var.get()
        if not text:
            if hasattr(self, "send_chat_btn"):
                self.flash_button_text(self.send_chat_btn, "Empty")
            return
        self.send_chat_message(text)
        self.chat_var.set("")

    def send_chat_message(self, text):
        msg = {"type": "DoSendChat", "content": text}
        if self.ws and self.connected:
            self.ws.send(json.dumps(msg))

    def ping_server(self):
        if not self.connected:
            self.flash_button_text(self.ping_btn, "Not Connected")
            return

        if self.ws and self.connected:
            self.ws.send(json.dumps({"type": "Ping", "content": True}))
            self.ws.send(json.dumps({"type": "DoPing", "content": True}))
            self.log("Ping sent.")

    def on_link_click(self, event):
        try:
            widget = event.widget
            index = widget.index(f"@{event.x},{event.y}")
            tags = widget.tag_names(index)
            for tag in tags:
                if tag.startswith("url:"):
                    url = tag[4:]
                    if is_safe_link(url):
                        webbrowser.open(url)
                    else:
                        self.log(f"Blocked non-web link: {url}")
        except Exception as e:
            self.log(f"Error opening link: {e}")

    def insert_link(self, widget, text, url):
        widget.insert("end", text, ("link", f"url:{url}"))

    def log(self, message):
        self.call_in_ui(lambda: self._log_impl(message))

    def log_chat(self, author, text):
        self.call_in_ui(lambda: self._log_chat_impl(author, text))

    def _log_chat_impl(self, author, text):
        self.log_text.config(state="normal")

        # Timestamp & Author
        ts = datetime.now().strftime("[%H:%M:%S]")
        self.log_text.insert("end", f"{ts} ", "timestamp")
        self.log_text.insert("end", f"[{author}]: ", "author")

        # Parse URLs and Mentions
        parts = re.split(r"(https?://\S+)", text)
        my_name = self.name_var.get().lower()

        for part in parts:
            if part.startswith("http"):
                self.log_text.insert("end", part, ("link", f"url:{part}"))
            else:
                # Check mentions (case-insensitive)
                if my_name and my_name in part.lower():
                    self.log_text.insert("end", part, "mention")
                    if self.config.get("alert_sound"):
                        self.play_custom_sound("sound_mention")
                else:
                    self.log_text.insert("end", part)

        self.log_text.insert("end", "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

        # File Log
        if self.config.get("logging_enabled", False):
            self._file_log(f"[CHAT] {author}: {text}")

    def _log_impl(self, message):
        # UI Log
        self.log_text.config(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

        # File Log
        if self.config.get("logging_enabled", False):
            self._file_log(message)

    def _file_log(self, message):
        try:
            folder = self.get_log_folder()
            filename = os.path.join(
                folder, f"bzr_log_{datetime.now().strftime('%Y-%m-%d')}.txt"
            )
            timestamp = datetime.now().strftime("[%H:%M:%S]")
            with open(filename, "a", encoding="utf-8") as f:
                f.write(f"{timestamp} {message}\n")
        except Exception:
            pass

    def toggle_connection(self):
        # should_run covers "connecting" and "waiting to reconnect" too, so a
        # second click can never start a duplicate worker.
        if self.should_run:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        self._cancel_reconnect()
        host = self.host_var.get().strip()
        if not host:
            messagebox.showerror("Error", "Host is required")
            return

        game = self.game_var.get()
        if game == "Battlezone 98 Redux":
            mode = "ws"
        elif host.startswith("http"):
            mode = "http"
        else:
            mode = "udp"

        proxy_on = self.config.get("proxy_enabled", False)
        p_host = str(self.config.get("proxy_host", "")).strip()
        p_port = str(self.config.get("proxy_port", "")).strip()
        p_type = self.config.get("proxy_type", "http")

        if mode == "ws" and proxy_on and p_type == "socks5" and not HAS_PYTHON_SOCKS:
            messagebox.showerror(
                "Missing Dependency",
                "SOCKS proxies (Tor) need the 'python-socks' package.\n"
                "Run: pip install python-socks",
            )
            return

        if self.config.get("ip_safety", False):
            if mode == "udp":
                messagebox.showerror(
                    "IP Safety",
                    "BZCC RakNet monitoring uses raw UDP, which cannot go through "
                    "a proxy.\nConnection blocked. Disable IP Safety or use an "
                    "http:// lobby URL instead.",
                )
                return
            if not proxy_on:
                messagebox.showerror(
                    "IP Safety",
                    "IP Safety is enabled but Proxy is disabled.\nConnection blocked.",
                )
                return
            if not p_host or not p_port:
                messagebox.showerror(
                    "IP Safety", "Proxy configuration missing.\nConnection blocked."
                )
                return

            # Verify the proxy off the UI thread, then continue on it.
            self.should_run = True
            self.conn_gen += 1
            gen = self.conn_gen
            self.connect_btn.config(text="Cancel")
            self.status_var.set("Verifying proxy...")
            self.log("IP Safety: Verifying proxy...")

            def verify():
                ok = self._test_proxy_connection(p_host, p_port)
                self.call_in_ui(self._after_proxy_check, gen, ok, mode, host)

            threading.Thread(target=verify, daemon=True).start()
            return

        self.should_run = True
        self.conn_gen += 1
        self._start_connection(mode, host, self.conn_gen)

    def _after_proxy_check(self, gen, ok, mode, host):
        if not self._conn_active(gen):
            return  # user cancelled while we were checking
        self._set_proxy_indicator(ok)
        if not ok:
            self.log("IP Safety: Proxy check failed.")
            self._set_disconnected_ui()
            self.should_run = False
            messagebox.showerror("IP Safety", "Proxy connection failed.\nConnection blocked.")
            return
        self.log("IP Safety: Proxy verified.")
        self._start_connection(mode, host, gen)

    def _start_connection(self, mode, host, gen):
        self.connect_btn.config(text="Disconnect")
        if mode == "ws":
            url = f"ws://{host}"
            self.log(f"Connecting to {url}...")
            self.status_var.set("Connecting...")
            target = self.run_ws
            arg = url
        elif mode == "http":
            self.log(f"Starting BZCC HTTP Monitor on {host}...")
            self.status_var.set("Monitoring (HTTP)...")
            target = self.run_bzcc_http
            arg = host
        else:
            self.log(f"Starting RakNet Monitor on {host}...")
            self.status_var.set("Monitoring (UDP)...")
            target = self.run_raknet
            arg = host

        self.ws_thread = threading.Thread(target=target, args=(arg, gen), daemon=True)
        self.ws_thread.start()

    def disconnect(self):
        self._cancel_reconnect()
        self.should_run = False
        self.conn_gen += 1  # retire the current worker
        ws = self.ws
        self.ws = None
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        self.connected = False
        self._set_disconnected_ui()
        self.log("Disconnected.")

    def _set_disconnected_ui(self):
        self.connect_btn.config(text="Connect")
        self.status_var.set("Disconnected")

    def _cancel_reconnect(self):
        if self.reconnect_after_id is not None:
            try:
                self.root.after_cancel(self.reconnect_after_id)
            except Exception:
                pass
            self.reconnect_after_id = None

    def _on_worker_stopped(self, gen, source):
        """Called on the UI thread when a connection worker exits."""
        if gen != self.conn_gen:
            return  # an old worker; a newer connection (or disconnect) owns the UI
        self.connected = False
        self.ws = None
        self.log(f"{source} stopped.")
        if not self.should_run:
            self._set_disconnected_ui()
            return

        self.trigger_alert("disconnect")
        if self.config.get("auto_reconnect", False):
            delay = max(1, int(self.config.get("reconnect_delay", 10) or 10))
            self.log(f"Auto-reconnecting in {delay}s...")
            self.status_var.set(f"Reconnecting in {delay}s...")
            self.connect_btn.config(text="Stop")
            self._cancel_reconnect()
            self.reconnect_after_id = self.root.after(delay * 1000, self._auto_reconnect)
        else:
            self.should_run = False
            self._set_disconnected_ui()

    def _auto_reconnect(self):
        self.reconnect_after_id = None
        if self.should_run and not self.connected:
            self.connect()

    def open_raknet_debugger(self):
        win = tk.Toplevel(self.root)
        apply_window_icon(win)
        win.title("RakNet Packet Debugger")
        win.geometry("600x400")

        # Controls
        ctrl = ttk.Frame(win, padding=10)
        ctrl.pack(fill="x")

        ttk.Label(ctrl, text="Host:").pack(side="left")
        host_val = self.host_var.get()
        if "http" in host_val:
            host_val = "battlezone99mp.webdev.rebellion.co.uk:61111"
        host_ent = ttk.Entry(ctrl, width=30)
        host_ent.insert(0, host_val)
        host_ent.pack(side="left", padx=5)

        ttk.Label(ctrl, text="Hex Payload:").pack(side="left")
        payload_ent = ttk.Entry(ctrl, width=30)
        payload_ent.insert(
            0,
            "01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00",
        )  # Default Ping
        payload_ent.pack(side="left", padx=5)

        # Log
        log_area = tk.Text(win, bg="#000", fg="#0f0", font=("Consolas", 9))
        log_area.pack(fill="both", expand=True, padx=10, pady=10)

        def clean_payload(p_str):
            # Remove whitespace/newlines
            clean = re.sub(r"[^0-9a-fA-F]", "", p_str)

            # Heuristic: Detect UDP Header targeting port 61111 (0xeeb7)
            # If user pasted full Wireshark dump, look for the destination port
            if len(clean) > 60 and "eeb7" in clean.lower():
                idx = clean.lower().find("eeb7")
                # UDP Header: Src(2) Dst(2) Len(2) Sum(2) -> Payload
                # 'eeb7' is Dst(2). Payload starts 4 bytes (8 chars) after it.
                payload_idx = idx + 8
                if payload_idx < len(clean):
                    extracted = clean[payload_idx:]
                    log_area.insert(
                        "end",
                        f"Auto-Stripped Headers. Payload: {len(extracted) // 2} bytes\n",
                    )
                    return extracted
            return clean

        def send_packet():
            h_str = host_ent.get()
            p_str = clean_payload(payload_ent.get())

            host, port = h_str.split(":") if ":" in h_str else (h_str, 61111)
            try:
                port = int(port)
                payload = bytes.fromhex(p_str)

                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(3.0)

                log_area.insert("end", f"TX -> {host}:{port} | {p_str}\n")
                sock.sendto(payload, (host, port))

                try:
                    data, addr = sock.recvfrom(4096)
                    log_area.insert("end", f"RX <- {addr} | {data.hex()}\n")
                    log_area.insert("end", f"ASCII: {data.decode('utf-8', 'ignore')}\n")
                    log_area.insert("end", "-" * 40 + "\n")
                except socket.timeout:
                    log_area.insert("end", "Timed out (No Reply)\n")
                finally:
                    sock.close()
            except Exception as e:
                log_area.insert("end", f"Error: {e}\n")

        def set_query():
            p_str = clean_payload(payload_ent.get())
            try:
                raw = bytes.fromhex(p_str)
                self.tx_rel_seq = (self.tx_rel_seq + 1) & 0xFFFFFF
                self.raknet_query = self.patch_raknet_packet(raw, self.tx_rel_seq)
                log_area.insert("end", f"Query Set (RelSeq: {self.tx_rel_seq})\n")
            except Exception:
                log_area.insert("end", "Invalid Hex\n")

        def load_connect():
            # 0x09 Connection Request
            preset = "8400000040009000000009040000001384b9fa00000000000503e100"
            payload_ent.delete(0, "end")
            payload_ent.insert(0, preset)
            log_area.insert("end", "Loaded 'Connect (0x09)'\n")

        def load_login():
            # 0x13 New Incoming Connection (Generic)
            preset = "840000006002f00000000000000013047f000001456a047f000001456a047f000001456a047f000001456a047f000001456a047f000001456a047f000001456a047f000001456a047f000001456a047f000001456a0000000000000000"
            payload_ent.delete(0, "end")
            payload_ent.insert(0, preset)
            log_area.insert("end", "Loaded 'Login (0x13)'\n")

        def load_query():
            # 0x60 Game Query
            preset = "8400000040001900000060e41f80"
            payload_ent.delete(0, "end")
            payload_ent.insert(0, preset)
            log_area.insert("end", "Loaded 'Query (0x60)'\n")

        ttk.Button(ctrl, text="Send", command=send_packet).pack(side="left", padx=5)
        ttk.Button(ctrl, text="0x09", width=5, command=load_connect).pack(
            side="left", padx=2
        )
        ttk.Button(ctrl, text="0x13", width=5, command=load_login).pack(
            side="left", padx=2
        )
        ttk.Button(ctrl, text="0x60", width=5, command=load_query).pack(
            side="left", padx=2
        )
        ttk.Button(ctrl, text="Set as Monitor Query", command=set_query).pack(
            side="left", padx=5
        )

    def register_direct_lobby(self, addr, status="Online"):
        lid = f"direct_{addr[0]}_{addr[1]}"
        self.lobbies[lid] = {
            "id": lid,
            "metadata": {
                "name": f"Direct: {addr[0]}",
                "gameType": "BZCC (RakNet)",
                "map": status,
                "gameSettings": "*Unknown*",
            },
            "users": {},
            "memberLimit": 0,
            "isLocked": False,
            "isPrivate": False,
            "owner": "Unknown",
        }
        self.refresh_tree()

    def patch_raknet_packet(self, pkt, new_rel_seq=None):
        if len(pkt) < 10:
            return pkt

        # 1. Parse Header to find Body Offset
        flags = pkt[4]
        reliability = (flags >> 5) & 0x07
        is_split = (flags & 0x10) != 0

        header_len = 7  # ID(1)+Seq(3)+Flags(1)+Len(2)
        if reliability in (2, 3, 4, 6, 7) and new_rel_seq is not None:
            # Patch Reliable Message Number (Bytes 7,8,9)
            rel_bytes = new_rel_seq.to_bytes(3, "little")
            pkt = pkt[:7] + rel_bytes + pkt[10:]
        header_len += raknet_frame_header_extra(reliability, is_split)

        if len(pkt) <= header_len:
            return pkt

        # 2. Patch GUID and timestamp for Connection Request (0x09)
        msg_id = pkt[header_len]
        if msg_id == 0x09:
            # 0x09 Structure: ID(1) + ClientGUID(8) + Time(8) + DoSecurity(1)
            guid_offset = header_len + 1
            time_offset = guid_offset + 8
            if len(pkt) >= time_offset + 8:
                t_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFFFFFF
                pkt = (
                    pkt[:guid_offset]
                    + self.client_guid
                    + t_ms.to_bytes(8, "big")
                    + pkt[time_offset + 8 :]
                )

        # 3. Patch Server Address for Login (0x13)
        if msg_id == 0x13 and hasattr(self, "server_addr_bytes"):
            # 0x13 Structure: ID(1) + ServerAddr(7) + ...
            addr_offset = header_len + 1
            if len(pkt) >= addr_offset + 7:
                pkt = (
                    pkt[:addr_offset] + self.server_addr_bytes + pkt[addr_offset + 7 :]
                )

        return pkt

    def run_raknet(self, host_str, gen):
        host, _, port = host_str.rpartition(":") if ":" in host_str else (host_str, "", "61111")
        try:
            port = int(port)
        except ValueError:
            port = 61111

        self.connected = True
        self.call_in_ui(lambda: self.status_var.set("Monitoring (UDP)"))

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)

        # RakNet Unconnected Ping Structure:
        magic = b"\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78"
        # Generate a persistent GUID for this session
        self.client_guid = random.getrandbits(64).to_bytes(8, "big")

        def make_ping(ptype):
            current_time = int(time.time() * 1000) & 0xFFFFFFFFFFFFFFFF
            return ptype + current_time.to_bytes(8, "big") + magic + self.client_guid

        def make_ocr1():
            # Open Connection Request 1 (0x05)
            # ID(1) + Magic(16) + Protocol(1) + NullPadding = 1400 bytes total
            # 1400 is the standard RakNet MTU probe size; 1464 would be fragmented
            return b"\x05" + magic + b"\x06" + (b"\x00" * 1382)

        def make_ocr2(server_addr_bytes, mtu):
            # Open Connection Request 2 (0x07)
            # ID(1) + Magic(16) + ServerAddress(7) + MTU(2) + ClientGUID(8)
            return b"\x07" + magic + server_addr_bytes + mtu + self.client_guid

        pkt_mode = (
            0  # 0:0x01(ping), 1:0x02(ping), 2:0x05(OCR1), 3:0x07(OCR2), 4..7:connected
        )
        server_mtu = b"\x05\xd4"  # Default 1492
        last_send_time = 0
        self.tx_seq = 0
        self.tx_rel_seq = -1
        self._connect_sent = False
        self._connect_rel_seq = 0
        send_now = (
            False  # set True on rx to trigger immediate send without waiting 1s tick
        )
        last_http_poll = 0  # track HTTP poll time independently of query cycle
        HTTP_POLL_INTERVAL = (
            3  # seconds — fast enough for near-real-time player join/leave detection
        )

        # Packet Templates (Raw)
        pkt_connect = bytes.fromhex(
            "8400000040009000000009040000001384b9fa00000000000503e100"
        )
        pkt_query = bytes.fromhex("8400000040001900000060e41f80")

        # Pre-calculate server address bytes for OCR2 (IPv4)
        try:
            ip_bytes = socket.inet_aton(socket.gethostbyname(host))
            port_bytes = port.to_bytes(2, "big")
            self.server_addr_bytes = b"\x04" + ip_bytes + port_bytes
        except Exception:
            self.server_addr_bytes = b"\x04\x7f\x00\x00\x01" + port.to_bytes(2, "big")

        while self._conn_active(gen):
            try:
                # Send when: 1-second retry tick fires, OR send_now was set by a received packet
                if send_now or (time.time() - last_send_time > 1.0):
                    send_now = False
                    pkt = None

                    if pkt_mode == 0:
                        pkt = make_ping(b"\x01")
                    elif pkt_mode == 1:
                        pkt = make_ping(b"\x02")
                    elif pkt_mode == 2:
                        pkt = make_ocr1()
                    elif pkt_mode == 3:
                        pkt = make_ocr2(self.server_addr_bytes, server_mtu)

                    elif (
                        pkt_mode == 4
                    ):  # Send Connect (0x09) — retransmit same rel_seq until ACK'd
                        if not self._connect_sent:
                            self.tx_rel_seq = (self.tx_rel_seq + 1) & 0xFFFFFF
                            self._connect_rel_seq = self.tx_rel_seq
                            self._connect_sent = True
                            self.log("Sending Connect (0x09)...")
                        pkt = self.patch_raknet_packet(
                            pkt_connect, self._connect_rel_seq
                        )

                    elif pkt_mode == 5:  # Send Login (0x13)
                        self.tx_rel_seq = (self.tx_rel_seq + 1) & 0xFFFFFF
                        pkt = self.make_login_packet(
                            self.server_addr_bytes, port, self.tx_rel_seq
                        )
                        self.log("Sending Login (0x13)...")
                        pkt_mode = 6
                        send_now = True  # immediately follow up with query

                    elif (
                        pkt_mode == 6
                    ):  # Send Query (0x60) — signals server we're active; data comes via HTTP
                        self.tx_rel_seq = (self.tx_rel_seq + 1) & 0xFFFFFF
                        base = self.raknet_query if self.raknet_query else pkt_query
                        pkt = self.patch_raknet_packet(base, self.tx_rel_seq)
                        self.log(
                            "Sending Query (0x60) — polling HTTP for lobby data..."
                        )
                        pkt_mode = 7
                        self.start_http_lobby_poll()
                        last_http_poll = time.time()

                    elif (
                        pkt_mode == 7
                    ):  # Idle — keepalive pings + periodic HTTP + re-query
                        now = time.time()
                        if now - last_http_poll >= HTTP_POLL_INTERVAL:
                            pkt_mode = 6  # re-issue query
                            send_now = True
                        else:
                            # Connected Ping (0x00) wrapped in Unreliable RakNet frame (0x84)
                            # Frame: ID(1) + Seq(3) + Flags(1=Unreliable) + LenBits(2) + Payload
                            # Payload: MsgID(1=0x00) + Time(8) = 9 bytes = 0x48 bits
                            t_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFFFFFF
                            ping_payload = b"\x00" + t_ms.to_bytes(8, "big")
                            pkt = bytearray(
                                b"\x84\x00\x00\x00\x00\x00\x48"
                            ) + bytearray(ping_payload)

                    if pkt:
                        # Patch DatagramSeq (Bytes 1-3 Little Endian) for all frame-set packets
                        if 0x80 <= pkt[0] <= 0x8F:
                            seq_bytes = self.tx_seq.to_bytes(3, "little")
                            pkt = pkt[0:1] + seq_bytes + pkt[4:]
                            self.tx_seq = (self.tx_seq + 1) & 0xFFFFFF
                        sock.sendto(pkt, (host, port))
                    last_send_time = time.time()

                try:
                    data, addr = sock.recvfrom(4096)
                    if data:
                        pid = data[0]

                        if pid == 0x1C:  # Unconnected Pong
                            if len(data) > 33:
                                server_info = data[33:].decode("utf-8", errors="ignore")
                                self.log(f"RakNet Pong from {addr}: {server_info}")
                            else:
                                self.log(f"RakNet Pong (No Data) from {addr}")
                            # On any pong: if still in ping phase, jump straight to OCR1
                            if pkt_mode < 2:
                                pkt_mode = 2
                                send_now = True
                                self.log("Pong received — sending OCR1 immediately.")
                            self.call_in_ui(self.register_direct_lobby, addr, "Ping OK")

                        elif pid == 0x06:  # Open Connection Reply 1
                            if len(data) >= 28:
                                server_mtu = data[26:28]
                                self.log(
                                    f"RX Reply 1. MTU: {int.from_bytes(server_mtu, 'big')}. Sending OCR2..."
                                )
                            if pkt_mode < 3:
                                pkt_mode = 3
                                send_now = True
                            self.call_in_ui(self.register_direct_lobby, addr, "Handshake (1/2)")

                        elif pid == 0x08:  # Open Connection Reply 2
                            self.log(
                                f"RX Open Connection Reply 2 from {addr}. Connection Established!"
                            )
                            self.call_in_ui(self.register_direct_lobby, addr, "Connected")
                            if pkt_mode < 4:
                                pkt_mode = 4
                                send_now = True
                                self.log(
                                    "Handshake Complete. Switching to Connected Mode (4)."
                                )

                        elif 0x80 <= pid <= 0x8F:  # RakNet Frame Set (Data)
                            seq_bytes = data[1:4]
                            seq_num = int.from_bytes(seq_bytes, "little")

                            # ACK: ID(C0) + BigEndianOrder(1) + Count(2) + MinEqMax(1) + Seq(3)
                            ack_pkt = b"\xc0\x00\x01\x01" + seq_bytes
                            sock.sendto(ack_pkt, (host, port))

                            frames = parse_raknet_frames(data)
                            is_ping_pong = all(
                                f and f[0] in [0x00, 0x03] for f in frames
                            )
                            if not is_ping_pong:
                                self.log(
                                    f"RX FrameSet {len(data)}b (ID: {hex(pid)}) Seq: {seq_num} -> Sent ACK"
                                )

                            for frame in frames:
                                if not frame:
                                    continue
                                msg_id = frame[0]
                                if msg_id == 0x10:
                                    self.log("  -> Connection Request Accepted")
                                    if pkt_mode == 4:
                                        pkt_mode = 5
                                        send_now = True  # send login immediately
                                elif msg_id == 0x61:
                                    self.log("  -> Game List Response (0x61)")
                                    payload = frame[1:]
                                    self.log(f"  -> Raw Data: {payload.hex()}")
                                    if len(payload) >= 4:
                                        count = int.from_bytes(payload[:4], "little")
                                        self.log(f"  -> Lobby Count: {count}")
                                        if count > 0:
                                            self.start_http_lobby_poll()
                                            last_http_poll = time.time()

                        else:
                            # Only log unknown packets (filter out ACKs 0xC0-0xCF which are keepalives)
                            if pid not in range(0xC0, 0xD0):
                                self.log(
                                    f"RX {len(data)}b from {addr} (ID: {hex(pid)})"
                                )
                except socket.timeout:
                    pass

                time.sleep(0.05)
            except Exception as e:
                self.log(f"RakNet Error: {e}")
                break

        sock.close()
        self.call_in_ui(self._on_worker_stopped, gen, "RakNet Monitor")

    def run_bzcc_http(self, url, gen):
        self.connected = True

        while self._conn_active(gen):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "BZLobbyMonitor/1.0"}
                )
                with self.open_url(req) as r:
                    data = json.loads(r.read().decode("utf-8"))
                if self._conn_active(gen):
                    self.call_in_ui(self.process_bzcc_data, data)
            except Exception as e:
                self.log(f"HTTP Error: {e}")

            for _ in range(15):  # Poll every 15s
                if not self._conn_active(gen):
                    break
                time.sleep(1)

        self.call_in_ui(self._on_worker_stopped, gen, "BZCC Monitor")

    def process_bzcc_data(self, data):
        """Merge a BZCC lobby-server snapshot. Must run on the UI thread."""
        games = data.get("GET", []) if isinstance(data, dict) else []
        new_lobbies = {}

        for g in games or []:
            if not isinstance(g, dict):
                continue
            built = build_bzcc_lobby(g)
            if built:
                lid, lobby = built
                new_lobbies[lid] = lobby

        # Diff against previous state to synthesize join/leave/new-game/gone-game events
        old_lobby_ids = set(k for k in self.lobbies if not k.startswith("direct_"))
        new_lobby_ids = set(new_lobbies.keys())

        for lid in new_lobby_ids - old_lobby_ids:
            lobby = new_lobbies[lid]
            lname = lobby["metadata"].get("name", lid)
            self.log(f"[BZCC] New game: {lname}")
            self.trigger_alert("new_lobby")

        for lid in old_lobby_ids - new_lobby_ids:
            lobby = self.lobbies[lid]
            lname = lobby["metadata"].get("name", lid)
            self.log(f"[BZCC] Game ended: {lname}")

        for lid in new_lobby_ids & old_lobby_ids:
            old_users = self.lobbies[lid].get("users", {})
            new_users = new_lobbies[lid].get("users", {})
            lname = new_lobbies[lid]["metadata"].get("name", lid)

            for pid, pdata in new_users.items():
                if pid not in old_users:
                    pname = pdata.get("name", pid)
                    self.log(f"[BZCC] {pname} joined {lname}")
                    self.trigger_alert("player_join", pname)

            for pid, pdata in old_users.items():
                if pid not in new_users:
                    pname = pdata.get("name", pid)
                    self.log(f"[BZCC] {pname} left {lname}")

        # Preserve non-BZCC entries (direct RakNet lobbies) then update
        direct_lobbies = {
            k: v for k, v in self.lobbies.items() if k.startswith("direct_")
        }
        self.lobbies = {**direct_lobbies, **new_lobbies}
        self.refresh_tree()

    def run_ws(self, url, gen):
        # websocket.enableTrace(True)

        proxy_opts = {}
        if self.config.get("proxy_enabled", False):
            host = str(self.config.get("proxy_host", "")).strip()
            port = str(self.config.get("proxy_port", "")).strip()
            ptype = self.config.get("proxy_type", "http")
            if ptype == "socks5":
                # socks5h resolves hostnames through the proxy (no DNS leak).
                ptype = "socks5h"
            if host and port:
                try:
                    proxy_opts["http_proxy_host"] = host
                    proxy_opts["http_proxy_port"] = int(port)
                    proxy_opts["proxy_type"] = ptype
                    self.log(f"Using Proxy: {host}:{port} ({ptype})")
                except ValueError:
                    self.log(f"Invalid proxy port: {port}")
                    self.call_in_ui(self._on_worker_stopped, gen, "WebSocket")
                    return

        # Callbacks run on this worker thread; hand everything to the UI thread
        # and tag it with the connection generation so stale sockets are ignored.
        ws_app = websocket.WebSocketApp(
            url,
            on_open=lambda ws: self.call_in_ui(self._on_open_ui, ws, gen),
            on_message=lambda ws, msg: self.call_in_ui(self._on_message_ui, ws, msg, gen),
            on_error=lambda ws, err: self.log(f"WebSocket Error: {err}"),
        )
        self.ws = ws_app
        try:
            ws_app.run_forever(**proxy_opts)
        except Exception as e:
            self.log(f"WebSocket Error: {e}")
        self.call_in_ui(self._on_worker_stopped, gen, "WebSocket")

    def _on_open_ui(self, ws, gen):
        if gen != self.conn_gen:
            try:
                ws.close()
            except Exception:
                pass
            return
        self.ws = ws
        self.on_open(ws)

    def _on_message_ui(self, ws, message, gen):
        if gen == self.conn_gen:
            self.on_message(ws, message)

    def on_open(self, ws):
        self.connected = True
        self.status_var.set("Connected")
        self.log("WebSocket Connected.")

        # Send Auth
        name = self.name_var.get()

        # Auto-Claim Bot Name Override
        if self.config.get("auto_claim_enabled", False):
            bot_name = self.config.get("auto_claim_bot_name", "")
            if bot_name:
                name = bot_name
                self.log(f"Using Auto-Claim Bot Name: {name}")

        auth_data = {
            "type": "Authorization",
            "content": {
                "authtype": "web",
                "key": self.key_var.get(),
                "id": "0",
                "apiVer": "0.0",
                "clientVersion": "2.2.301",
            },
        }

        if name:
            auth_data["content"]["name"] = name
            auth_data["content"]["playerName"] = name

        ws.send(json.dumps(auth_data))

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            msg_type = data.get("type")
            content = data.get("data", {})

            if msg_type == "OnAuthorization":
                success = content.get("success")
                self.my_id = content.get("id")
                self.log(f"Auth Response: {success} (ID: {self.my_id})")
                if success:
                    # Enter Lounge to get updates
                    ws.send(json.dumps({"type": "DoEnterLounge", "content": True}))
                    self.set_player_data()
                    # Explicitly request lobby list
                    ws.send(json.dumps({"type": "GetLobbyList", "content": True}))

            elif msg_type in ["OnLobbyListChanged", "OnLobbyList", "OnGetLobbyList"]:
                self.handle_lobby_list(content)

            elif msg_type in ["OnLobbyChanged", "OnLobbyUpdate"]:
                self.handle_lobby_changed(content)

            elif msg_type == "OnLobbyRemoved":
                self.handle_lobby_removed(content)

            elif msg_type == "OnLobbyJoined":
                self.handle_lobby_joined(content)

            elif msg_type == "OnLobbyCreated":
                self.handle_lobby_created(content)

            elif msg_type == "OnChatMessage":
                self.handle_chat_message(content)

            elif msg_type == "OnLobbyMemberListChanged":
                self.handle_member_list_changed(content)

            elif msg_type == "OnUserDataChanged":
                self.handle_user_data_changed(content)

            elif msg_type == "OnLobbyDataChanged":
                self.handle_lobby_data_changed(content)

        except Exception as e:
            self.log(f"Error parsing message: {e}")

    def set_player_data(self):
        name = self.name_var.get()

        if self.config.get("auto_claim_enabled", False):
            bot_name = self.config.get("auto_claim_bot_name", "")
            if bot_name:
                name = bot_name

        if not name:
            name = f"User_{self.my_id}"

        # Send player data updates similar to ConnectionManager.js
        updates = [
            {"key": "name", "value": name},
            {"key": "playerName", "value": name},
            {"key": "clientVersion", "value": "2.2.301"},
            {"key": "authType", "value": "web"},
        ]
        for u in updates:
            self.ws.send(json.dumps({"type": "SetPlayerData", "content": u}))

    def handle_lobby_list(self, data):
        # Handle different data structures (lobbies object directly or inside data)
        if "lobbies" in data:
            new_lobbies = data.get("lobbies", {})
        else:
            new_lobbies = data
        if not isinstance(new_lobbies, dict):
            new_lobbies = {}
        self.lobbies = {str(k): v for k, v in new_lobbies.items() if isinstance(v, dict)}
        self.log(f"Received Full Lobby List: {len(self.lobbies)} lobbies.")
        self.call_in_ui(self.refresh_tree)
        self.call_in_ui(self.check_and_update_current_lobby)
        self.root.after(2000, self.check_auto_claim)

    def handle_lobby_changed(self, data):
        if "lobbies" in data:
            changed_lobbies = data.get("lobbies", {})
        elif "lobby" in data:
            # Single lobby update
            l = data.get("lobby", {})
            changed_lobbies = {str(l.get("id")): l}
        else:
            changed_lobbies = {}

        for lid, lobby in changed_lobbies.items():
            if not isinstance(lobby, dict):
                continue
            if self.config.get("alert_griefer", False):
                self.check_griefer_join(str(lid), lobby)

            if str(lid) not in self.lobbies:
                self.trigger_alert("new_lobby")

            # Only run auto-ban check if the user list actually changed
            old_users = set(self.lobbies.get(str(lid), {}).get("users", {}).keys())
            new_users = set(lobby.get("users", {}).keys())
            users_changed = old_users != new_users

            self.lobbies[str(lid)] = lobby
            if users_changed:
                self.check_auto_ban_lobby(str(lid))
        self.log(f"Lobbies Updated: {list(changed_lobbies.keys())}")
        self.call_in_ui(self.refresh_tree)
        self.call_in_ui(self.check_and_update_current_lobby)

    def get_griefer_ids(self):
        return set(parse_id_list(self.config.get("griefer_ids", "")))

    def is_known_griefer(self, uid):
        uid = str(uid).lower()
        ids = self.get_griefer_ids()
        # Accept Steam IDs with or without the "S" prefix.
        return uid in ids or f"s{uid}" in ids or (uid.startswith("s") and uid[1:] in ids)

    def check_griefer_join(self, lid, new_lobby_data):
        new_users = new_lobby_data.get("users", {}) or {}
        old_users = (self.lobbies.get(lid) or {}).get("users", {}) or {}

        for uid, user in new_users.items():
            if not self.is_known_griefer(uid) or uid in old_users:
                continue
            name = user.get("name", "Unknown") if isinstance(user, dict) else "Unknown"
            self.log(f"WARNING: Griefer {name} detected in lobby {lid}")
            self.trigger_alert("griefer_join", name)

    def handle_lobby_removed(self, data):
        lid = str(data.get("id"))
        if lid in self.lobbies:
            del self.lobbies[lid]
            self.call_in_ui(self.refresh_tree)
            self.log(f"Lobby Removed: {lid}")
            self.root.after(2000, self.check_auto_claim)
        if self.current_lobby_id is not None and str(self.current_lobby_id) == str(lid):
            self.log("Current lobby was removed. Returning to lounge.")
            self.check_and_update_current_lobby()

    def handle_lobby_joined(self, data):
        if data.get("success") is not False:
            lid = data.get("id")
            self.log(f"Joined Lobby: {lid}")
            self.update_current_lobby(lid)
        else:
            self.log(f"Join Failed: {data.get('reason')}")

    def handle_lobby_created(self, data):
        if data.get("success") is not False:
            lid = data.get("id")
            self.log(f"Lobby Created: {lid}")
            self.set_lobby_metadata(lid)
            self.update_current_lobby(lid)
        else:
            self.log(f"Create Failed: {data.get('reason')}")

    def handle_chat_message(self, chat):
        # Handle both speakerId (new) and author (old) formats
        author = chat.get("author")
        if not author:
            author = chat.get("speakerId", "Unknown")

        speaker_id = chat.get("speakerId")
        if speaker_id and str(speaker_id) in self.muted_users:
            return

        text = str(chat.get("text", ""))
        self.log(f"[CHAT] {author}: {text}")
        self.log_chat(author, text)

        # Don't bounce messages we relayed from Discord back to Discord.
        if text.startswith("[Discord] "):
            return

        # Relay to Discord
        if self.config.get("discord_enabled", False) and self.config.get(
            "discord_relay_to_discord", True
        ):
            target_lobby = self.discord_lobby_id_var.get()
            if str(self.current_lobby_id) == str(target_lobby):
                self.send_to_discord(message=f"**{author}**: {text}")

    def handle_member_list_changed(self, data):
        member = data.get("member")
        lid = data.get("lobbyId")
        uid = data.get("id", member)
        member = "" if member is None else str(member)
        action = "left" if data.get("removed") else "joined"
        self.log(f"User {member} {action} lobby {lid}")
        if not data.get("removed"):
            self.trigger_alert("player_join", member)
            self.check_auto_ban_join(lid, uid, member)

            # Bot Welcome
            if self.config.get("bot_enabled", False) and self.connected:
                if str(self.current_lobby_id) == str(lid):
                    cooldown = self.config.get("bot_welcome_cooldown", 60)
                    last_time = self.last_welcome_times.get(uid, 0)
                    if time.time() - last_time > cooldown:
                        msg = self.config.get("bot_welcome_msg", "")
                        if msg:
                            final_msg = msg.replace("{player}", member)
                            self.send_chat_message(final_msg)
                            self.last_welcome_times[uid] = time.time()
                    else:
                        self.log(f"Welcome suppressed for {member} (Cooldown active)")

        # Refresh details if we are looking at this lobby
        self.check_and_update_current_lobby()

    def check_auto_ban_join(self, lid, uid, name):
        # Fast check on join (Name/ID only, IP might not be ready)
        lobby = self.lobbies.get(str(lid))
        if not lobby:
            return

        owner = str(lobby.get("owner", ""))
        if owner != str(self.my_id):
            return  # We only ban in our lobbies

        ban_list = self.config.get("ban_list", "").lower().splitlines()
        ban_list = [b.strip() for b in ban_list if b.strip()]

        if str(uid).lower() in ban_list or str(name or "").lower() in ban_list:
            self._auto_kick(uid, name, "Auto-Ban (ID/Name Match)")

    def check_auto_ban_lobby(self, lid):
        # Deep check on lobby update (Includes IPs)
        lobby = self.lobbies.get(str(lid))
        if not lobby:
            return

        owner = str(lobby.get("owner", ""))
        if owner != str(self.my_id):
            return

        ban_list = self.config.get("ban_list", "").lower().splitlines()
        ban_list = [b.strip() for b in ban_list if b.strip()]
        if not ban_list:
            return

        users = lobby.get("users", {})
        for uid, u_data in users.items():
            if str(uid) == str(self.my_id):
                continue

            if not isinstance(u_data, dict):
                continue
            name = str(u_data.get("name") or "").lower()
            ip = str(u_data.get("ipAddress") or "").lower()

            if (
                str(uid).lower() in ban_list
                or name in ban_list
                or (ip and ip != "unknown" and ip in ban_list)
            ):
                self._auto_kick(
                    uid, u_data.get("name", "Unknown"), "Auto-Ban (IP/ID Match)"
                )

    def _auto_kick(self, uid, name, reason):
        if not (self.ws and self.connected):
            return
        self.log(f"!!! KICKING {name} (ID: {uid}) - Reason: {reason} !!!")
        self.ws.send(
            json.dumps(
                {
                    "type": "DoKickUser",
                    "content": int(uid) if str(uid).isdigit() else uid,
                }
            )
        )

    def handle_user_data_changed(self, data):
        member = data.get("member")
        self.log(f"User Data Changed: {member}")

    def handle_lobby_data_changed(self, data):
        lid = data.get("changedLobby")
        self.log(f"Lobby Data Changed: {lid}")

    def set_lobby_metadata(self, lobby_id):
        # Set default metadata for created lobbies (similar to ChatManager.js)
        name = self.new_lobby_var.get()
        full_name = f"~chat~pub~~{name}"
        meta_updates = [
            {"key": "clientVersion", "value": "2.2.301"},
            {"key": "GameVersion", "value": "2.2.301"},
            {"key": "gameType", "value": "1"},
            {"key": "gameSettings", "value": "*"},
            {"key": "name", "value": full_name},
        ]
        for m in meta_updates:
            self.ws.send(json.dumps({"type": "SetLobbyData", "content": m}))

    def check_and_update_current_lobby(self):
        if not self.my_id:
            self.update_current_lobby(None)
            return

        found_lobby_id = None
        my_id = str(self.my_id)
        for lid, lobby in self.lobbies.items():
            users = lobby.get("users", {}) if isinstance(lobby, dict) else {}
            if isinstance(users, dict) and my_id in {str(u) for u in users}:
                found_lobby_id = lid
                break
        self.update_current_lobby(found_lobby_id)

    @staticmethod
    def _server_lobby_id(lobby_id):
        """The lobby server uses numeric ids for BZ98R lobbies."""
        text = str(lobby_id)
        return int(text) if text.isdigit() else lobby_id

    def update_current_lobby(self, lobby_id):
        self.current_lobby_id = lobby_id
        if lobby_id is not None:
            lobby_name = (
                self.lobbies.get(str(lobby_id), {})
                .get("metadata", {})
                .get("name", f"ID: {lobby_id}")
            )
            self.call_in_ui(
                lambda: self.current_lobby_var.set(f"In Lobby: {lobby_name}")
            )
            self.call_in_ui(lambda: self.leave_btn.config(text="Leave Lobby"))
            self.update_rpc(f"In Lobby: {lobby_name}", "Playing Battlezone 98 Redux")
        else:
            self.call_in_ui(lambda: self.current_lobby_var.set("In Lounge"))
            self.call_in_ui(lambda: self.leave_btn.config(text="Refresh Lounge"))
            self.update_rpc("In Lounge", "Browsing Lobbies")

    def refresh_tree(self):
        # Save selection
        selected_items = self.tree.selection()
        selected_id = None
        if selected_items:
            selected_id = self.tree.item(selected_items[0])["values"][0]

        # Clear
        for row_id in self.tree.get_children():
            self.tree.delete(row_id)

        # Repopulate
        friends = parse_id_list(self.config.get("friend_list", ""))
        for lid, lobby in self.lobbies.items():
            if str(lid).startswith("direct_"):
                continue
            if not isinstance(lobby, dict):
                continue
            meta = lobby.get("metadata", {})
            if not isinstance(meta, dict):
                meta = {}
            # Clean up name display (remove ~chat~pub~~ prefix)
            name = clean_lobby_name(meta.get("name"), default="Unknown")

            owner = lobby.get("owner", "Unknown")
            if owner == -1:
                owner = "none"

            # Calculate player count
            users = lobby.get("users", {})
            if not isinstance(users, dict):
                users = {}

            # Check for friends
            has_friend = any(
                list_matches(friends, (u or {}).get("name") if isinstance(u, dict) else "", uid)
                for uid, u in users.items()
            )

            if self.filter_locked_var.get() and lobby.get("isLocked"):
                continue
            try:
                member_limit = int(lobby.get("memberLimit") or 0)
            except (TypeError, ValueError):
                member_limit = 0
            # A zero/unknown limit means "unknown", not "full".
            if self.filter_full_var.get() and member_limit > 0 and len(users) >= member_limit:
                continue

            player_count = f"{len(users)}/{lobby.get('memberLimit', '?')}"

            game_type = meta.get("gameType", "?")
            version = lobby.get("clientVersion", "?")
            locked = "Yes" if lobby.get("isLocked") else "No"
            is_private = "Yes" if lobby.get("isPrivate") else "No"

            map_name = extract_map_name_from_metadata(meta, default="?")

            tags = ("friend",) if has_friend else ()
            self.tree.insert(
                "",
                "end",
                values=(
                    lid,
                    name,
                    map_name,
                    owner,
                    player_count,
                    game_type,
                    version,
                    locked,
                    is_private,
                ),
                tags=tags,
            )

        # Restore selection if possible
        if selected_id:
            for row_id in self.tree.get_children():
                if str(self.tree.item(row_id)["values"][0]) == str(selected_id):
                    self.tree.selection_set(row_id)
                    break
        self.refresh_waiting_room()

    def refresh_waiting_room(self):
        if not hasattr(self, "waiting_room_text"):
            return

        waiting_rows = []
        for lobby in self.lobbies.values():
            if not isinstance(lobby, dict) or not lobby.get("isChat"):
                continue
            meta = lobby.get("metadata", {})
            name = meta.get("name", "Lounge") if isinstance(meta, dict) else "Lounge"
            if "~~" in str(name):
                name = str(name).split("~~")[-1]
            users = lobby.get("users", {})
            if not isinstance(users, dict) or not users:
                continue

            player_names = []
            for uid, user in users.items():
                player_names.append(self.get_user_display_name(uid, user))
            waiting_rows.append((str(name), sorted(player_names, key=str.lower)))

        self.waiting_room_text.config(state="normal")
        self.waiting_room_text.delete("1.0", "end")
        if not waiting_rows:
            self.waiting_room_text.insert("end", "No players waiting.")
        else:
            for lobby_name, player_names in sorted(waiting_rows, key=lambda row: row[0].lower()):
                self.waiting_room_text.insert("end", f"{lobby_name}\n")
                for player_name in player_names:
                    self.waiting_room_text.insert("end", f" - {player_name}\n")
                self.waiting_room_text.insert("end", "\n")
        self.waiting_room_text.config(state="disabled")

    def on_lobby_select(self, event):
        selected_items = self.tree.selection()
        if not selected_items:
            return

        lid = str(self.tree.item(selected_items[0])["values"][0])
        lobby = self.lobbies.get(lid)

        if lobby:
            self.update_lobby_details(lobby)
            self.update_player_details(lobby)

    def get_user_display_name(self, uid, user):
        if not isinstance(user, dict):
            return str(uid)

        user_name = user.get("name", "Unknown")
        user_meta = user.get("metadata", {})
        if (user_name == "unknown" or not user_name) and isinstance(user_meta, dict):
            user_name = user_meta.get("name", "Unknown")
        return str(user_name or uid)

    def get_user_team_group(self, user):
        if not isinstance(user, dict):
            return "No Team"

        user_meta = user.get("metadata", {})
        team = user_meta.get("team") if isinstance(user_meta, dict) else None
        if team in [None, ""]:
            team = user.get("team")
        if team in [None, ""]:
            return "No Team"

        try:
            team_num = int(team)
        except (TypeError, ValueError):
            return f"Team {team}"
        return "Odds" if team_num % 2 else "Evens"

    def update_lobby_details(self, lobby):
        self.lobby_details_text.config(state="normal")
        self.lobby_details_text.delete("1.0", "end")

        # Reset preview
        self.preview_label.config(image="", text="No Preview")

        # Check for cached image to display in label
        l_meta = lobby.get("metadata", {})
        game_settings = l_meta.get("gameSettings")
        mod_id = None
        if game_settings:
            parts = str(game_settings).split("*")
            # Workshop ids are numeric; anything else would end up in a URL.
            if len(parts) > 3 and parts[3].strip().isdigit() and parts[3].strip() != "0":
                mod_id = parts[3].strip()

        if mod_id and mod_id in self.image_cache:
            self.preview_label.config(image=self.image_cache[mod_id], text="")
        elif mod_id:
            self.preview_label.config(text="Loading Preview...")
            if mod_id not in self.pending_fetches:
                self.fetch_image(mod_id, is_mod=True)

        self.lobby_details_text.insert("end", f"Lobby ID: {lobby.get('id')}\n")
        self.lobby_details_text.insert("end", f"Name: {l_meta.get('name')}\n")
        self.lobby_details_text.insert("end", f"Created: {lobby.get('createdTime')}\n")

        if l_meta.get("gameType") == "BZCC":
            gt = l_meta.get("typeId")
            type_map = {1: "Deathmatch", 2: "Strategy", 3: "MPI"}
            type_str = type_map.get(gt, f"Unknown ({gt})")
            self.lobby_details_text.insert("end", f"Type: {type_str}\n")

            si = l_meta.get("stateId")
            state_map = {3: "Lobby", 4: "Loading", 5: "In Game", 6: "Post Game"}
            state_str = state_map.get(si, f"Unknown ({si})")
            self.lobby_details_text.insert("end", f"State: {state_str}\n")

            self.lobby_details_text.insert("end", f"Version: {l_meta.get('version')}\n")

            users = lobby.get("users", {})
            max_p = l_meta.get("maxPlayers", "?")
            self.lobby_details_text.insert("end", f"Players: {len(users)} / {max_p}\n")
            if l_meta.get("tps") is not None:
                self.lobby_details_text.insert("end", f"TPS: {l_meta.get('tps')}\n")
            if l_meta.get("pingMs") is not None:
                self.lobby_details_text.insert("end", f"Ping: {l_meta.get('pingMs')} ms (inferred)\n")
            if l_meta.get("maxPingMs") is not None:
                self.lobby_details_text.insert("end", f"Max Ping: {l_meta.get('maxPingMs')} ms\n")
            if l_meta.get("gameTimeMinutes") is not None:
                self.lobby_details_text.insert("end", f"Game Time: {l_meta.get('gameTimeMinutes')} min (inferred)\n")
            if l_meta.get("typeDetailId") is not None:
                self.lobby_details_text.insert("end", f"Type Detail ID: {l_meta.get('typeDetailId')} (inferred)\n")
            if l_meta.get("natType") is not None:
                self.lobby_details_text.insert("end", f"NAT Type: {l_meta.get('natType')}\n")
            if l_meta.get("passwordProtected") is not None:
                self.lobby_details_text.insert("end", f"Passworded: {l_meta.get('passwordProtected')}\n")
            if l_meta.get("hostMessage"):
                self.lobby_details_text.insert("end", f"Host Message: {l_meta.get('hostMessage')}\n")
            if l_meta.get("modsCrc"):
                self.lobby_details_text.insert("end", f"Mods CRC: {l_meta.get('modsCrc')}\n")
            if l_meta.get("modList"):
                self.lobby_details_text.insert("end", f"Mods: {l_meta.get('modList')}\n")
            elif l_meta.get("mapModCrc"):
                self.lobby_details_text.insert("end", f"Mods: {l_meta.get('mapModCrc')}\n")
        elif l_meta.get("gameType") == "BZCC (RakNet)":
            users = lobby.get("users", {})
            self.lobby_details_text.insert("end", f"Status: {l_meta.get('connectionStatus', 'Unknown')}\n")
            self.lobby_details_text.insert("end", f"Version: {l_meta.get('version', '?')}\n")
            self.lobby_details_text.insert("end", f"Players: {len(users)} / {lobby.get('memberLimit', '?')}\n")
            if l_meta.get("motd"):
                self.lobby_details_text.insert("end", f"MOTD: {l_meta.get('motd')}\n")
            if l_meta.get("mods"):
                self.lobby_details_text.insert("end", f"Mods: {l_meta.get('mods')}\n")
            if l_meta.get("mapUrl"):
                self.lobby_details_text.insert("end", "Map URL: ")
                self.insert_link(self.lobby_details_text, l_meta.get("mapUrl"), l_meta.get("mapUrl"))
                self.lobby_details_text.insert("end", "\n")
        
        # Parse Game Settings from Lobby Metadata
        if game_settings:
            settings = parse_game_settings(game_settings)
            map_name = settings.get("map") or extract_map_name_from_game_settings(game_settings, default=None)
            if map_name:
                self.lobby_details_text.insert("end", f"Map: {map_name}\n")
            if settings.get("crc32"):
                self.lobby_details_text.insert("end", f"CRC32: {settings.get('crc32')}\n")
            if mod_id:
                self.lobby_details_text.insert("end", f"Mod ID: {mod_id} (")
                self.insert_link(self.lobby_details_text, "Workshop", f"https://steamcommunity.com/sharedfiles/filedetails/?id={mod_id}")
                self.lobby_details_text.insert("end", ")\n")
            summary = format_game_settings_summary(game_settings)
            if summary:
                self.lobby_details_text.insert("end", f"Settings: {summary}\n")
        else:
            self.lobby_details_text.insert("end", f"Game Settings: {game_settings}\n")

        if str(l_meta.get("launched")) == "1":
            self.lobby_details_text.insert("end", "Status: Launched\n")

        self.lobby_details_text.config(state="disabled")

    def update_player_details(self, lobby):
        # Preserve scroll position
        self.player_details_text.config(state="normal")
        scroll_pos = self.player_details_text.yview()
        self.player_details_text.delete("1.0", "end")

        users = lobby.get("users", {})
        friends = parse_id_list(self.config.get("friend_list", ""))
        owner_id = str(lobby.get("owner", ""))
        groups = {}
        if isinstance(users, dict):
            for uid, user in users.items():
                group_name = self.get_user_team_group(user)
                groups.setdefault(group_name, []).append((str(uid), user))

        ordered_groups = ["Odds", "Evens", "No Team"]
        ordered_groups.extend(sorted(k for k in groups if k not in ordered_groups))

        if not groups:
            self.player_details_text.insert("end", "No players in this lobby.")

        for group_name in ordered_groups:
            entries = groups.get(group_name, [])
            if not entries:
                continue

            entries.sort(
                key=lambda item: (
                    str(item[0]) != owner_id,
                    self.get_user_display_name(item[0], item[1]).lower(),
                )
            )
            self.player_details_text.insert("end", f"{group_name}\n", "team_header")

            for uid, user in entries:
                user = user if isinstance(user, dict) else {}
                user_name = self.get_user_display_name(uid, user)
                user_meta = user.get("metadata", {})
                if not isinstance(user_meta, dict):
                    user_meta = {}

                is_friend = list_matches(friends, user_name, uid)

                self.player_details_text.insert(
                    "end", f" - {user_name} (ID: {uid})", "friend" if is_friend else ""
                )
                if str(uid) == owner_id:
                    self.player_details_text.insert("end", " [HOST]", "host")
                if is_friend:
                    self.player_details_text.insert("end", " [FRIEND]", "friend")
                self.player_details_text.insert("end", "\n")
                self.player_details_text.insert("end", f"   Auth: {user.get('authType')}\n")

                # Geo Lookup
                ip = user.get("ipAddress")
                if ip and ip != "unknown":
                    self.player_details_text.insert("end", f"   IP: {ip}\n")
                    geo = self.get_geo_info(ip)
                    if geo:
                        self.player_details_text.insert("end", f"   Loc: {geo}\n")

                if uid.startswith("S") and uid[1:].isdigit():
                    steam_id = uid[1:]

                    if HAS_PIL:
                        if steam_id in self.image_cache:
                            self.player_details_text.insert("end", "   Avatar: ")
                            self.player_details_text.image_create(
                                "end", image=self.image_cache[steam_id]
                            )
                            self.player_details_text.insert("end", "\n")
                        elif steam_id not in self.pending_fetches:
                            self.fetch_image(steam_id, is_mod=False)

                    self.player_details_text.insert("end", "   Profile: ")
                    self.insert_link(
                        self.player_details_text,
                        f"{steam_id}",
                        f"https://steamcommunity.com/profiles/{steam_id}",
                    )

                    if self.is_known_griefer(uid):
                        self.player_details_text.insert(
                            "end", " [KNOWN GRIEFER]", "griefer"
                        )

                    self.player_details_text.insert("end", "\n")

                # Extended User Info
                if user_meta:
                    if "team" in user_meta:
                        self.player_details_text.insert(
                            "end", f"   Team: {user_meta['team']}\n"
                        )
                    if "vehicle" in user_meta:
                        self.player_details_text.insert(
                            "end", f"   Vehicle: {user_meta['vehicle']}\n"
                        )

                    # Parse Ready String for Map info (often on host)
                    ready = user_meta.get("ready")
                    if ready:
                        ready_map = extract_map_name_from_game_settings(ready, default=None)
                        if ready_map:
                            self.player_details_text.insert(
                                "end", f"   Ready Map: {ready_map}\n"
                            )
                    if user_meta.get("launched") == "1":
                        self.player_details_text.insert("end", "   Status: Launched\n")

                # Network Info
                wan = user.get("wanAddress")
                if wan and wan != "unknown":
                    self.player_details_text.insert("end", f"   WAN: {wan}\n")

                lans = user.get("lanAddresses")
                if lans:
                    if isinstance(lans, list):
                        lan_str = ", ".join(lans)
                    else:
                        lan_str = str(lans)
                    if lan_str:
                        self.player_details_text.insert(
                            "end", f"   LAN: {lan_str}\n"
                        )

                self.player_details_text.insert("end", "-" * 30 + "\n")

        self.player_details_text.config(state="disabled")
        self.player_details_text.yview_moveto(scroll_pos[0])

    def fetch_image(self, target_id, is_mod):
        # Don't retry a failed fetch on every redraw.
        if time.time() - self.image_failed.get(target_id, 0) < 600:
            return
        self.pending_fetches.add(target_id)
        threading.Thread(
            target=self._fetch_image_worker, args=(target_id, is_mod), daemon=True
        ).start()

    def _fetch_image_worker(self, target_id, is_mod):
        try:
            image_url = None
            if is_mod:
                url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={target_id}"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with self.open_url(req) as r:
                    html = r.read(2_000_000).decode("utf-8", errors="replace")
                    # Try to find preview image
                    thumb = re.search(r'id="ActualImage"\s+src="([^"]+)"', html)
                    if not thumb:
                        thumb = re.search(
                            r'<link rel="image_src" href="([^"]+)">', html
                        )
                    if thumb:
                        image_url = thumb.group(1)
            else:
                # Fetch Steam Profile XML
                url = f"https://steamcommunity.com/profiles/{target_id}?xml=1"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with self.open_url(req) as r:
                    xml = r.read(2_000_000).decode("utf-8", errors="replace")
                    # Simple regex for avatarMedium
                    avatar = re.search(
                        r"<avatarMedium><!\[CDATA\[(.*?)\]\]></avatarMedium>", xml
                    )
                    if avatar:
                        image_url = avatar.group(1)

            # Only follow https image links scraped from the page.
            if image_url and image_url.startswith("https://"):
                with self.open_url(image_url) as r:
                    data = r.read(5_000_000)
                self.call_in_ui(self._cache_image, target_id, data, is_mod)
            else:
                self.image_failed[target_id] = time.time()
                self.pending_fetches.discard(target_id)

        except Exception as e:
            print(f"Image fetch failed for {target_id}: {e}")
            self.image_failed[target_id] = time.time()
            self.pending_fetches.discard(target_id)

    def _cache_image(self, target_id, data, is_mod):
        try:
            img = Image.open(BytesIO(data))
            # Larger preview for the lobby panel, avatar-sized otherwise.
            size = (280, 160) if is_mod else (50, 50)
            img.thumbnail(size, Image.Resampling.LANCZOS)
            self.image_cache[target_id] = ImageTk.PhotoImage(img)
            # Refresh current view if applicable
            self.on_lobby_select(None)
        except Exception as e:
            print(f"Error caching image: {e}")
        finally:
            self.pending_fetches.discard(target_id)

    # --- Proxy Tools ---
    def set_tor_proxy(self):
        # pysocks is used for proxy tests and web lookups, python-socks by the
        # WebSocket library itself.
        try:
            import socks  # noqa: F401
        except ImportError:
            socks = None
        if socks is None or not HAS_PYTHON_SOCKS:
            messagebox.showerror(
                "Missing Dependency",
                "To use Tor (SOCKS5), install 'pysocks' and 'python-socks'.\n"
                "Run: pip install pysocks python-socks",
            )
            return

        if sys.platform == "win32":
            self.manage_tor_windows()
        else:
            # Linux/Mac: Assume system Tor
            self.proxy_host_var.set("127.0.0.1")
            self.proxy_port_var.set("9050")
            self.proxy_enabled_var.set(True)
            self.config["proxy_type"] = "socks5"
            self.save_ui_config()
            messagebox.showinfo(
                "Tor Proxy",
                "Proxy set to 127.0.0.1:9050 (SOCKS5).\nEnsure the 'tor' service is running on your system.",
            )

    def manage_tor_windows(self):
        bin_dir = os.path.join(APP_DIR, "bin", "tor")
        tor_exe = os.path.join(bin_dir, "tor.exe")

        if os.path.exists(tor_exe):
            self._finish_tor_setup(bin_dir)
            return

        if not messagebox.askyesno(
            "Tor Not Found",
            "Tor Expert Bundle is missing.\nDownload it from torproject.org and configure it automatically?",
        ):
            return

        def worker():
            ok = self.download_tor(bin_dir)
            if ok:
                self.call_in_ui(self._finish_tor_setup, bin_dir)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_tor_setup(self, bin_dir):
        tor_exe = os.path.join(bin_dir, "tor.exe")
        if os.path.exists(tor_exe):
            self.start_tor(tor_exe, bin_dir)

        self.proxy_host_var.set("127.0.0.1")
        self.proxy_port_var.set("9050")
        self.proxy_enabled_var.set(True)
        self.config["proxy_type"] = "socks5"
        self.save_ui_config()
        self.log("Tor Proxy Configured (127.0.0.1:9050)")

    def _resolve_tor_bundle_url(self):
        """Find the current Windows expert bundle link on the Tor download page."""
        req = urllib.request.Request(TOR_DOWNLOAD_PAGE, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read(2_000_000).decode("utf-8", errors="replace")
        match = re.search(
            r'href="(https://[^"]+/tor-expert-bundle-windows-x86_64-[0-9][0-9A-Za-z.\-]*\.tar\.gz)"',
            html,
        )
        if not match:
            raise RuntimeError("Could not find the Windows expert bundle on the Tor download page")
        return match.group(1)

    def download_tor(self, target_dir):
        """Download, verify and unpack Tor. Runs on a worker thread."""
        self.log("Downloading Tor Expert Bundle...")
        try:
            url = self._resolve_tor_bundle_url()
            filename = url.rsplit("/", 1)[-1]
            sums_url = url.rsplit("/", 1)[0] + "/sha256sums-signed-build.txt"
            self.log(f"URL: {url}")

            with urllib.request.urlopen(sums_url, timeout=20) as r:
                sums = r.read(1_000_000).decode("utf-8", errors="replace")
            expected = None
            for line in sums.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip("*") == filename:
                    expected = parts[0].lower()
                    break
            if not expected:
                raise RuntimeError(f"No published SHA-256 for {filename}; refusing to install")

            with urllib.request.urlopen(url, timeout=60) as r:
                bundle = r.read(200_000_000)
            actual = hashlib.sha256(bundle).hexdigest()
            if actual != expected:
                raise RuntimeError(f"SHA-256 mismatch for {filename}; refusing to install")

            os.makedirs(target_dir, exist_ok=True)
            self.log("Extracting Tor...")
            with tarfile.open(fileobj=BytesIO(bundle), mode="r:gz") as tar:
                # Flatten structure: write 'tor/tor.exe' and DLLs directly to bin/tor/.
                # Only regular files, and only by basename, so nothing can escape.
                for member in tar.getmembers():
                    base = os.path.basename(member.name)
                    if not member.isfile():
                        continue
                    if base.lower() != "tor.exe" and not base.lower().endswith(".dll"):
                        continue
                    src = tar.extractfile(member)
                    if src is None:
                        continue
                    with open(os.path.join(target_dir, base), "wb") as out:
                        out.write(src.read())

            self.log("Tor installed successfully (SHA-256 verified).")
            return True
        except Exception as e:
            self.log(f"Tor Download Error: {e}")
            self.call_in_ui(messagebox.showerror, "Error", f"Failed to download Tor:\n{e}")
            return False

    def _write_torrc(self, tor_dir):
        # Always regenerate so DataDirectory points at this machine's folder.
        data_dir = os.path.join(tor_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        torrc_path = os.path.join(tor_dir, "torrc")
        with open(torrc_path, "w", encoding="utf-8") as f:
            f.write(f'SocksPort 9050\nDataDirectory "{os.path.abspath(data_dir)}"\n')
        return torrc_path

    def start_tor(self, exe_path, cwd):
        if self.tor_process:
            return  # Already running

        self.log("Starting Tor process...")
        try:
            torrc_path = self._write_torrc(cwd)
            kwargs = {}
            if sys.platform == "win32":
                # Hide console window
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                kwargs["startupinfo"] = startupinfo

            self.tor_process = subprocess.Popen(
                [exe_path, "-f", torrc_path],
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **kwargs,
            )
            self.log("Tor started in background.")
            self.tor_status_var.set("Tor: Running")
            self.tor_status_label.config(fg="#00ff00")
        except Exception as e:
            self.log(f"Failed to start Tor: {e}")

    def stop_tor(self):
        if self.tor_process:
            self.log("Stopping Tor...")
            self.tor_process.terminate()
            self.tor_process = None
            self.tor_status_var.set("Tor: Stopped")
            self.tor_status_label.config(fg="#666666")

    def find_free_proxy(self):
        if not messagebox.askyesno(
            "Public Proxy Warning",
            "Free public proxies are run by unknown third parties.\n\n"
            "The BZ98R lobby connection is unencrypted (ws://), so whoever runs the "
            "proxy can read your chat and your lobby Key, and could tamper with traffic.\n\n"
            "Continue?",
        ):
            return
        self.log("Searching for free proxies...")
        threading.Thread(target=self._find_proxy_worker, daemon=True).start()

    def _find_proxy_worker(self):
        try:
            # Fetch from a public list (HTTP proxies)
            url = (
                "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"
            )
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                data = r.read().decode("utf-8")
                proxies = [line.strip() for line in data.split("\n") if ":" in line]

            if not proxies:
                self.log("No proxies found in list.")
                return

            # Test random proxies until one works
            random.shuffle(proxies)
            for proxy in proxies[:10]:  # Try up to 10
                host, _, port = proxy.rpartition(":")
                self.log(f"Testing proxy {host}:{port}...")
                if self._test_proxy_connection(host, port, ptype="http"):
                    self.call_in_ui(lambda h=host, p=port: self._set_proxy_ui(h, p))
                    self.log(f"Found working proxy: {host}:{port}")
                    return

            self.log("Could not find a working proxy in the sample.")
        except Exception as e:
            self.log(f"Proxy search failed: {e}")

    def _test_proxy_connection(self, host, port, ptype=None):
        if ptype is None:
            ptype = self.config.get("proxy_type", "http")

        try:
            if ptype == "socks5":
                try:
                    import socks

                    s = socks.socksocket()
                    s.set_proxy(socks.SOCKS5, host, int(port))
                    s.settimeout(5)
                    s.connect(("www.google.com", 80))
                    s.close()
                    return True
                except Exception:
                    return False
            else:
                proxy_handler = urllib.request.ProxyHandler(
                    {"http": f"http://{host}:{port}", "https": f"http://{host}:{port}"}
                )
                opener = urllib.request.build_opener(proxy_handler)
                opener.open("http://www.google.com", timeout=5)
                return True
        except Exception:
            return False

    def test_proxy(self):
        host = self.proxy_host_var.get()
        port = self.proxy_port_var.get()
        ptype = self.config.get("proxy_type", "http")
        if not host or not port:
            return

        def run_test():
            self.call_in_ui(lambda: self._set_proxy_indicator(None))
            success = False
            try:
                if ptype == "socks5":
                    import socks

                    s = socks.socksocket()
                    s.set_proxy(socks.SOCKS5, host, int(port))
                    s.settimeout(10)
                    s.connect(("api.ipify.org", 80))
                    s.sendall(
                        b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n"
                    )
                    response = b""
                    while True:
                        data = s.recv(4096)
                        if not data:
                            break
                        response += data
                    s.close()
                    body = response.split(b"\r\n\r\n")[1].decode("utf-8")
                    self.log(f"Proxy Test (SOCKS5): SUCCESS. IP: {body}")
                    success = True
                    self.call_in_ui(
                        lambda: messagebox.showinfo(
                            "Proxy Verified",
                            f"SOCKS5 Proxy is working.\nExternal IP: {body}",
                        ),
                    )
                elif self._test_proxy_connection(host, port):
                    # For HTTP, try to get IP as well
                    req = urllib.request.Request("http://api.ipify.org")
                    req.set_proxy(f"{host}:{port}", "http")
                    with urllib.request.urlopen(req, timeout=10) as r:
                        ip = r.read().decode("utf-8")
                        self.log(f"Proxy Test (HTTP): SUCCESS. IP: {ip}")
                        self.call_in_ui(
                            lambda: messagebox.showinfo(
                                "Proxy Verified",
                                f"HTTP Proxy is working.\nExternal IP: {ip}",
                            ),
                        )
                    success = True
            except Exception as e:
                self.log(f"Proxy Test Failed: {e}")

            if success:
                self.call_in_ui(lambda: self._set_proxy_indicator(True))
            else:
                self.call_in_ui(lambda: self._set_proxy_indicator(False))

        threading.Thread(target=run_test, daemon=True).start()

    def _set_proxy_ui(self, host, port):
        self.proxy_host_var.set(host)
        self.proxy_port_var.set(port)
        self.proxy_enabled_var.set(True)
        self.config["proxy_type"] = "http"  # Public lists are usually HTTP
        self.save_ui_config()
        self._set_proxy_indicator(True)

    def start_proxy_monitor(self):
        threading.Thread(target=self._proxy_monitor_loop, daemon=True).start()

    def _proxy_monitor_loop(self):
        while self.app_running:
            if self.config.get("proxy_enabled", False):
                host = self.config.get("proxy_host", "")
                port = self.config.get("proxy_port", "")
                if host and port:
                    res = self._test_proxy_connection(host, port)
                    self.call_in_ui(lambda r=res: self._set_proxy_indicator(r))
                else:
                    self.call_in_ui(lambda: self._set_proxy_indicator(None))
            else:
                self.call_in_ui(lambda: self._set_proxy_indicator(None))

            if self.tor_process:
                if self.tor_process.poll() is not None:
                    self.tor_process = None
                    self.log("Tor process terminated unexpectedly.")
                    self.call_in_ui(lambda: self.tor_status_var.set("Tor: Stopped"))
                    self.call_in_ui(
                        lambda: self.tor_status_label.config(fg="#ff0000")
                    )

            for _ in range(30):
                if not self.app_running:
                    return
                time.sleep(1)

    def _set_proxy_indicator(self, status):
        if not hasattr(self, "proxy_status_light"):
            return
        color = "gray"
        if status is True:
            color = "#00ff00"
        elif status is False:
            color = "#ff0000"
        self.proxy_status_canvas.itemconfig(self.proxy_status_light, fill=color)

    # --- Logging Tools ---
    def get_log_folder(self):
        folder = str(self.config.get("log_folder", "")).strip()
        if folder and os.path.isdir(folder):
            return folder
        return APP_DIR

    def get_stats_file(self):
        return os.path.join(self.get_log_folder(), "bzr_stats.csv")

    def cleanup_logs(self):
        """Delete daily chat/event logs older than the retention period."""
        try:
            retention = max(1, int(self.config.get("log_retention", 7) or 7))
            cutoff = datetime.now().date() - timedelta(days=retention)
            folder = self.get_log_folder()
            for fname in os.listdir(folder):
                m = re.fullmatch(r"bzr_log_(\d{4}-\d{2}-\d{2})\.txt", fname)
                if not m:
                    continue
                try:
                    day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                except ValueError:
                    continue
                if day < cutoff:
                    os.remove(os.path.join(folder, fname))
        except Exception as e:
            print(f"Log cleanup failed: {e}")
        # Re-check a few times a day for long-running sessions.
        self.root.after(6 * 3600 * 1000, self.cleanup_logs)

    def toggle_stats_logging(self):
        self.save_ui_config()
        if self.stats_enabled_var.get():
            self.start_stats_logger()
        self.draw_stats()

    def start_stats_logger(self):
        # Runs on the Tk timer (UI thread) so it never races lobby updates.
        if self.stats_after_id is None:
            self.stats_after_id = self.root.after(1000, self._stats_tick)

    def _stats_tick(self):
        self.stats_after_id = None
        if not self.app_running or not self.config.get("stats_enabled", False):
            return
        try:
            if self.lobbies:
                filename = self.get_stats_file()
                file_exists = os.path.isfile(filename)

                with open(filename, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    if not file_exists:
                        writer.writerow(
                            [
                                "Timestamp",
                                "LobbyID",
                                "Name",
                                "Map",
                                "Players",
                                "MaxPlayers",
                                "Type",
                            ]
                        )

                    timestamp = datetime.now().isoformat()
                    for lid, lobby in self.lobbies.items():
                        if not isinstance(lobby, dict):
                            continue
                        meta = lobby.get("metadata", {})
                        if not isinstance(meta, dict):
                            meta = {}
                        users = lobby.get("users", {})

                        writer.writerow(
                            [
                                timestamp,
                                lid,
                                meta.get("name", "Unknown"),
                                extract_map_name_from_metadata(meta, default="?"),
                                len(users) if isinstance(users, dict) else 0,
                                lobby.get("memberLimit", 0),
                                meta.get("gameType", "?"),
                            ]
                        )
        except Exception as e:
            print(f"Stats log error: {e}")

        # Log every 60 seconds
        self.stats_after_id = self.root.after(60 * 1000, self._stats_tick)

    # --- Discord Integration ---
    def toggle_discord_relay(self):
        self.save_ui_config()
        self.discord_bot_id = None  # token may have changed; re-resolve
        if self.discord_enabled_var.get():
            if not self.discord_thread or not self.discord_thread.is_alive():
                self.discord_thread = threading.Thread(
                    target=self.discord_polling_loop, daemon=True
                )
                self.discord_thread.start()
                self.log("Discord Relay Started.")

    def _discord_request(self, token, path, payload=None):
        """Call the Discord REST API. Runs on worker threads only."""
        data = None
        method = "GET"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            method = "POST"
        req = urllib.request.Request(
            f"https://discord.com/api/v10{path}", data=data, method=method
        )
        req.add_header("Authorization", f"Bot {token}")
        req.add_header("User-Agent", "BZLobbyMonitor/1.0")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.open_url(req) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Rate limited: wait as long as Discord asks before retrying.
                try:
                    retry = float(json.loads(e.read().decode("utf-8")).get("retry_after", 1))
                except Exception:
                    retry = 1.0
                time.sleep(min(max(retry, 0.5), 30))
            raise
        return json.loads(body.decode("utf-8")) if body else None

    def _resolve_discord_bot_id(self, token):
        me = self._discord_request(token, "/users/@me")
        self.discord_bot_id = str(me.get("id")) if me and me.get("id") else None
        return me

    def test_discord_connection(self):
        token = self.discord_token_var.get().strip()
        if not token:
            return

        def worker():
            try:
                me = self._resolve_discord_bot_id(token)
                username = me.get("username")
                self.log(f"Discord Bot Authenticated: {username}")
                self.call_in_ui(
                    messagebox.showinfo,
                    "Success",
                    f"Connected as {username} (ID: {self.discord_bot_id})",
                )
            except Exception as e:
                self.call_in_ui(
                    messagebox.showerror, "Error", f"Discord Connection Failed:\n{e}"
                )

        threading.Thread(target=worker, daemon=True).start()

    def send_to_discord(self, message=None, embed=None):
        token = self.discord_token_var.get().strip()
        chan_id = self.discord_channel_id_var.get().strip()
        if not token or not chan_id.isdigit():
            return
        # allowed_mentions blocks @everyone/@here/role pings from relayed chat.
        payload = build_discord_message_payload(message=message, embed=embed)

        def _send():
            try:
                self._discord_request(token, f"/channels/{chan_id}/messages", payload)
            except Exception as e:
                print(f"Discord Send Error: {e}")

        threading.Thread(target=_send, daemon=True).start()

    def post_lobby_status(self):
        if not self.connected:
            self.flash_button_text(self.discord_status_btn, "Not Connected")
            return

        if self.current_lobby_id is None:
            self.flash_button_text(self.discord_status_btn, "Not in Lobby")
            return

        lobby = self.lobbies.get(str(self.current_lobby_id))
        if not lobby:
            return

        meta = lobby.get("metadata", {})
        name = meta.get("name", "Unknown Lobby")
        if "~~" in name:
            name = name.split("~~")[-1]

        users = lobby.get("users", {})
        player_count = f"{len(users)}/{lobby.get('memberLimit', '?')}"

        map_name = extract_map_name_from_metadata(meta, default="Unknown")
        settings_summary = format_game_settings_summary(meta.get("gameSettings", ""))

        embed = {
            "title": f"🎮 {name}",
            "color": 0x00FF00,
            "fields": [
                {"name": "Map", "value": map_name, "inline": True},
                {"name": "Players", "value": player_count, "inline": True},
                {"name": "ID", "value": str(self.current_lobby_id), "inline": True},
            ],
            "footer": {
                "text": f"Battlezone Lobby Monitor • {datetime.now().strftime('%H:%M')}"
            },
        }
        if settings_summary:
            embed["fields"].append(
                {"name": "Settings", "value": settings_summary, "inline": False}
            )

        # Add join link if host is steam
        url = build_steam_join_url(self.current_lobby_id, lobby)
        if url:
            embed["description"] = f"[**Click to Join via Steam**]({url})"

        self.send_to_discord(embed=embed)
        self.log("Posted lobby status to Discord.")

    def discord_polling_loop(self):
        last_id = None
        last_error = None

        # Reads self.config (kept current by save_ui_config), never Tk variables:
        # this runs on a worker thread.
        while self.app_running and self.config.get("discord_enabled", False):
            try:
                token = str(self.config.get("discord_token", "")).strip()
                chan_id = str(self.config.get("discord_channel_id", "")).strip()
                if not token or not chan_id.isdigit():
                    time.sleep(5)
                    continue

                # We must know our own id, or our relayed posts would echo back.
                if not self.discord_bot_id:
                    self._resolve_discord_bot_id(token)

                if last_id is None:
                    # Start from the newest message so old history isn't replayed.
                    msgs = self._discord_request(token, f"/channels/{chan_id}/messages?limit=1") or []
                    last_id = msgs[0].get("id") if msgs else "0"
                    continue

                msgs = self._discord_request(
                    token, f"/channels/{chan_id}/messages?limit=50&after={last_id}"
                ) or []

                # Snowflake ids sort chronologically; process oldest first.
                for m in sorted(msgs, key=lambda m: int(m.get("id", 0))):
                    chat_line = should_relay_discord_message(
                        m,
                        bot_id=self.discord_bot_id,
                        relay_to_lobby_enabled=self.config.get("discord_relay_to_lobby", True),
                        connected=self.connected,
                        current_lobby_id=self.current_lobby_id,
                        target_lobby_id=self.config.get("discord_lobby_id", ""),
                    )
                    if chat_line:
                        self.call_in_ui(self.send_chat_message, chat_line[:500])
                    last_id = m.get("id", last_id)
                last_error = None
            except Exception as e:
                if str(e) != last_error:
                    last_error = str(e)
                    self.log(f"Discord relay error: {e}")
            time.sleep(2)

    # --- Bot & RPC & Stats ---
    def start_bot_loop(self):
        threading.Thread(target=self.bot_loop, daemon=True).start()

    def bot_loop(self):
        while self.app_running:
            if self.connected and self.current_lobby_id is not None:
                # Standard Announcements
                if self.config.get("bot_announce_enabled", False):
                    interval = int(self.config.get("bot_announce_interval", 5) or 5) * 60
                    if time.time() - self.last_announce_time > interval:
                        msg = self.config.get("bot_announce_msg", "")
                        if msg:
                            self.call_in_ui(self.send_chat_message, msg)
                            self.last_announce_time = time.time()

                # Timed Event Announcements
                if self.config.get("bot_event_enabled", False):
                    try:
                        start_str = self.config.get("bot_event_start", "")
                        end_str = self.config.get("bot_event_end", "")
                        if start_str and end_str:
                            now = datetime.now()
                            fmt = "%Y-%m-%d %H:%M"
                            start_dt = datetime.strptime(start_str, fmt)
                            end_dt = datetime.strptime(end_str, fmt)

                            if start_dt <= now <= end_dt:
                                evt_interval = (
                                    int(self.config.get("bot_event_interval", 10) or 10) * 60
                                )
                                if (
                                    time.time() - self.last_event_announce_time
                                    > evt_interval
                                ):
                                    evt_msg = self.config.get("bot_event_msg", "")
                                    if evt_msg:
                                        self.call_in_ui(self.send_chat_message, evt_msg)
                                        self.last_event_announce_time = time.time()
                    except ValueError:
                        pass  # Invalid date format
                    except Exception as e:
                        print(f"Event bot error: {e}")

            time.sleep(10)

    def check_auto_claim(self):
        if not self.config.get("auto_claim_enabled", False):
            return
        if not self.connected:
            return
        if self.current_lobby_id is not None:
            return

        if time.time() - self.last_claim_attempt < 10:
            return

        target_name = self.config.get("auto_claim_name", "")
        if not target_name:
            return

        found = False
        for lid, lobby in self.lobbies.items():
            meta = lobby.get("metadata", {}) if isinstance(lobby, dict) else {}
            clean_name = clean_lobby_name(meta.get("name"), default="")
            if clean_name.lower() == target_name.lower():
                found = True
                break

        if not found:
            self.log(f"Auto-Claim: Lobby '{target_name}' missing. Creating...")

            # Force name reclaim in case we were using a backup name (e.g. !BRIDGE(1))
            bot_name = self.config.get("auto_claim_bot_name", "")
            if bot_name and self.ws:
                self.log(f"Reclaiming identity: {bot_name}")
                self.ws.send(
                    json.dumps(
                        {
                            "type": "SetPlayerData",
                            "content": {"key": "name", "value": bot_name},
                        }
                    )
                )
                self.ws.send(
                    json.dumps(
                        {
                            "type": "SetPlayerData",
                            "content": {"key": "playerName", "value": bot_name},
                        }
                    )
                )

            self.create_lobby(target_name)
            self.last_claim_attempt = time.time()

    def get_geo_info(self, ip):
        if ip in self.geo_cache:
            return self.geo_cache[ip]
        try:
            if not ipaddress.ip_address(str(ip)).is_global:
                return None  # private/LAN addresses have no useful location
        except ValueError:
            return None
        # One request per address, and back off after failures (ip-api allows
        # 45 requests/minute).
        if ip in self.geo_pending or time.time() - self.geo_failed.get(ip, 0) < 600:
            return None
        if len(self.geo_pending) >= 4:
            return None
        self.geo_pending.add(ip)

        def _fetch():
            try:
                req = f"http://ip-api.com/json/{ip}?fields=status,countryCode,timezone,offset"
                with self.open_url(req) as r:
                    data = json.loads(r.read().decode())
                if data.get("status") == "success":
                    info = f"[{data.get('countryCode')}] {data.get('timezone')}"
                    self.geo_cache[ip] = info
                    # Refresh UI if this player is currently shown
                    self.call_in_ui(self.on_lobby_select, None)
                else:
                    self.geo_failed[ip] = time.time()
            except Exception:
                self.geo_failed[ip] = time.time()
            finally:
                self.geo_pending.discard(ip)

        threading.Thread(target=_fetch, daemon=True).start()
        return None

    def init_rpc(self):
        if not HAS_RPC:
            return
        client_id = self.config.get("rpc_client_id", "").strip()
        if not client_id:
            self.log(
                "Discord RPC: No Client ID set. Please enter your Application Client ID in Bot Settings."
            )
            return
        try:
            self.rpc = Presence(client_id)
            self.rpc.connect()
            self.log("Discord RPC Connected.")
        except Exception as e:
            self.log(f"RPC Error: {e}")

    def toggle_rpc(self):
        self.save_ui_config()
        if self.rpc_enabled_var.get():
            if not self.rpc:
                self.init_rpc()
        else:
            if self.rpc:
                self.rpc.close()
                self.rpc = None

    def update_rpc(self, state, details):
        if self.rpc and self.config.get("rpc_enabled", False):
            try:
                self.rpc.update(
                    state=state,
                    details=details,
                    large_image="bz98_icon",
                    large_text="Battlezone 98 Redux",
                )
            except Exception:
                pass

    def draw_stats(self):
        """Reload the stats CSV in the background, then redraw."""
        if not self.config.get("stats_enabled", False):
            self.stats_points = None
            self._render_stats()
            return
        if self.stats_loading:
            return
        self.stats_loading = True
        filename = self.get_stats_file()

        def load():
            points = []
            try:
                if os.path.exists(filename):
                    with open(filename, "r", encoding="utf-8", newline="") as f:
                        reader = csv.reader(f)
                        next(reader, None)  # Skip header
                        # One snapshot per minute; sum all lobbies in a snapshot.
                        points = aggregate_recent_player_counts(reader, bucket_minutes=1)
                else:
                    points = None
            except Exception as e:
                print(f"Stats read error: {e}")
            self.call_in_ui(self._stats_loaded, points)

        threading.Thread(target=load, daemon=True).start()

    def _stats_loaded(self, points):
        self.stats_loading = False
        self.stats_points = points
        self._render_stats()

    def _render_stats(self):
        self.stats_canvas.delete("all")
        w = self.stats_canvas.winfo_width()
        h = self.stats_canvas.winfo_height()

        if not self.config.get("stats_enabled", False):
            self.stats_canvas.create_text(
                w / 2,
                h / 2,
                text="Statistics logging is DISABLED.\nEnable 'Game Stats Logging' in Configuration.",
                fill="red",
                justify="center",
                font=("Consolas", 12, "bold"),
            )
            return

        if w < 50:
            return

        sorted_pts = self.stats_points
        if sorted_pts is None:
            self.stats_canvas.create_text(
                w / 2, h / 2, text="No stats data found.", fill="white"
            )
            return
        if not sorted_pts:
            return

        max_p = max(p[1] for p in sorted_pts) or 10

        # Draw
        pad = 40
        prev_x, prev_y = None, None
        start_time = sorted_pts[0][0]
        total_seconds = (sorted_pts[-1][0] - start_time).total_seconds() or 1

        for dt, count in sorted_pts:
            secs = (dt - start_time).total_seconds()
            x = pad + (secs / total_seconds) * (w - 2 * pad)
            y = h - pad - (count / max_p) * (h - 2 * pad)

            if prev_x is not None:
                self.stats_canvas.create_line(
                    prev_x, prev_y, x, y, fill=self.colors["highlight"], width=2
                )
            prev_x, prev_y = x, y

        self.stats_canvas.create_text(
            pad,
            h - pad + 15,
            text=start_time.strftime("%H:%M"),
            fill="gray",
            anchor="w",
        )
        self.stats_canvas.create_text(
            w - pad,
            h - pad + 15,
            text=sorted_pts[-1][0].strftime("%H:%M"),
            fill="gray",
            anchor="e",
        )
        self.stats_canvas.create_text(
            pad - 5, pad, text=str(max_p), fill="gray", anchor="e"
        )
        self.stats_canvas.create_text(
            pad - 5, h - pad, text="0", fill="gray", anchor="e"
        )

    def make_login_packet(self, server_addr_bytes, port, rel_seq):
        # Construct 0x13 Message
        msg = bytearray()
        msg.append(0x13)

        # Server Address (Inverted IP for BZCC RakNet)
        srv_ip = server_addr_bytes[1:5]
        inv_srv_ip = bytes([b ^ 0xFF for b in srv_ip])
        msg.extend(b"\x04" + inv_srv_ip + server_addr_bytes[5:])

        # Internal IPs (List of 10)
        local_ips = []
        try:
            # Get actual local IPs to satisfy server validation
            local_ips = [
                ip
                for ip in socket.gethostbyname_ex(socket.gethostname())[2]
                if not ip.startswith("127.")
            ]
        except Exception:
            pass
        if not local_ips:
            local_ips = ["0.0.0.0"]

        for i in range(10):
            msg.append(0x04)  # Family AF_INET
            if i < len(local_ips):
                try:
                    ip_bytes = socket.inet_aton(local_ips[i])
                    msg.extend(bytes([b ^ 0xFF for b in ip_bytes]))
                except Exception:
                    msg.extend(b"\xff" * 4)
            else:
                msg.extend(b"\xff" * 4)
            msg.extend(port.to_bytes(2, "big"))

        msg.extend((0).to_bytes(16, "big"))  # Timestamps

        # Wrap in RakNet Frame (Reliable Ordered)
        # Header: ID(1) + Seq(3) + Flags(1) + Len(2) + RelSeq(3) + OrderIndex(3) + OrderChannel(1)
        frame = bytearray()
        frame.append(0x84)
        frame.extend((0).to_bytes(3, "little"))  # Seq (patched later)
        frame.append(0x60)  # Reliability 3 (Reliable Ordered)
        frame.extend((len(msg) * 8).to_bytes(2, "big"))  # Length in bits
        frame.extend(rel_seq.to_bytes(3, "little"))
        frame.extend((0).to_bytes(3, "little"))  # Order Index
        frame.append(0)  # Order Channel
        frame.extend(msg)

        return bytes(frame)

    def start_http_lobby_poll(self):
        # At most one poll in flight; a slow server must not pile up threads.
        if self.http_poll_inflight:
            return
        self.http_poll_inflight = True
        threading.Thread(target=self.poll_http_lobby, daemon=True).start()

    def poll_http_lobby(self):
        try:
            url = "http://battlezone99mp.webdev.rebellion.co.uk/lobbyServer/"
            req = urllib.request.Request(url, method="GET")

            with self.open_url(req, timeout=5) as f:
                res = json.load(f)
            if isinstance(res, dict) and "GET" in res:
                games = res["GET"]
                self.log(f"HTTP Lobby: Found {len(games)} games.")
                self.call_in_ui(self.process_bzcc_data, res)
        except Exception as e:
            self.log(f"HTTP Poll Failed: {e}")
        finally:
            self.http_poll_inflight = False


def main():
    root = tk.Tk()
    BZLobbyMonitor(root)
    root.mainloop()


if __name__ == "__main__":
    main()

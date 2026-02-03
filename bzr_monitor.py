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
from datetime import datetime, timedelta
import tarfile
import zipfile
import subprocess
import shutil
import base64

if sys.platform == 'win32':
    import winreg
    import winsound

try:
    import pystray
    from pystray import MenuItem as item, Icon
    HAS_TRAY = True
except ImportError:
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
    if not hasattr(websocket, 'WebSocketApp'):
        print("WARNING: Incorrect 'websocket' package detected. Please install 'websocket-client'.", file=sys.stderr)
        websocket = None
except ImportError:
    websocket = None

# Try to import PIL for images
try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

CONFIG_FILE = "bzr_monitor_config.json"

class BZLobbyMonitor:
    def __init__(self, root):
        self.root = root
        self.root.title("Battlezone Redux Lobby Monitor")
        self.root.geometry("1000x700")
        self.root.minsize(800, 600)
        
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            icon_path = os.path.join(base_dir, "bzrmon.ico")
            if os.path.exists(icon_path):
                self.root.iconbitmap(icon_path)
        except: pass
        
        self.lobbies = {}
        self.ws = None
        self.ws_thread = None
        self.connected = False
        self.should_run = True
        self.current_lobby_id = None
        self.my_id = None
        self.image_cache = {}
        self.pending_fetches = set()
        self.discord_bot_id = None
        self.discord_thread = None
        self.muted_users = set()
        self.geo_cache = {}
        self.rpc = None
        self.last_announce_time = time.time()
        self.last_welcome_times = {}
        self.last_claim_attempt = 0
        self.tray_icon = None
        self.tor_process = None
        self.raknet_query = None # Custom payload for RakNet connected state
        self.tx_rel_seq = -1 # Reliable Message Sequence Number
        
        self.colors = {}
        self.load_config()
        self.load_custom_fonts()
        self.setup_styles()
        self.setup_ui()
        self.apply_config()
        
        # Start stats logger if enabled
        if self.config.get("stats_enabled", False):
            self.start_stats_logger()
        self.start_proxy_monitor()
        self.start_bot_loop()
        
        if HAS_RPC and self.config.get("rpc_enabled", False):
            self.init_rpc()
            
        if HAS_TRAY:
            self.setup_tray()
        
        if not websocket:
            messagebox.showerror("Missing Dependency", "Please install 'websocket-client' to use this tool.\npip install websocket-client")
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
            "auto_claim_enabled": False,
            "auto_claim_name": "default",
            "auto_claim_bot_name": "",
            "rpc_enabled": False,
            "rpc_client_id": "133570000000000000", # Placeholder
            "sound_join": "",
            "sound_mention": "",
            "sound_griefer": ""
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    self.config.update(json.load(f))
            except: pass

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
                except: pass
        else:
            self.custom_font_name = "Monospace"

    def setup_styles(self):
        self.colors = {
            "bg": "#0a0a0a", "fg": "#d4d4d4",
            "highlight": "#00ff00", "dark_highlight": "#004400", "accent": "#00ffff"
        }
        c = self.colors
        
        style = ttk.Style()
        style.theme_use('default')
        
        main_font = (self.custom_font_name, 10)
        bold_font = (self.custom_font_name, 11, "bold")
        
        style.configure(".", background=c["bg"], foreground=c["fg"], font=main_font, bordercolor=c["dark_highlight"])
        style.configure("TFrame", background=c["bg"])
        style.configure("TNotebook", background=c["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background="#1a1a1a", foreground=c["fg"], padding=[10, 2])
        style.map("TNotebook.Tab", background=[("selected", c["dark_highlight"])], foreground=[("selected", c["highlight"])])
        style.configure("TLabelframe", background=c["bg"], bordercolor=c["highlight"])
        style.configure("TLabelframe.Label", background=c["bg"], foreground=c["highlight"], font=bold_font)
        style.configure("TLabel", background=c["bg"], foreground=c["fg"])
        style.configure("TEntry", fieldbackground="#1a1a1a", foreground=c["accent"], insertcolor=c["highlight"])
        style.configure("TButton", background="#1a1a1a", foreground=c["fg"])
        style.map("TButton", background=[("active", c["dark_highlight"])], foreground=[("active", c["highlight"])])
        style.configure("TCheckbutton", background=c["bg"], foreground=c["fg"], indicatorcolor="#1a1a1a", indicatoron=True)
        style.map("TCheckbutton", indicatorcolor=[("selected", c["highlight"])])
        style.configure("Treeview", background="#0a0a0a", foreground=c["fg"], fieldbackground="#0a0a0a", rowheight=25)
        style.map("Treeview", background=[("selected", c["accent"])], foreground=[("selected", "#000000")])
        style.configure("Treeview.Heading", background="#1a1a1a", foreground=c["fg"], font=bold_font)
        
        self.root.configure(bg=c["bg"])

    def flash_button_text(self, button, text, duration=2000):
        if not button: return
        try:
            original_text = button.cget("text")
            button.config(text=text)
            self.root.after(duration, lambda: button.config(text=original_text) if button.winfo_exists() else None)
        except: pass

    def save_config(self):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.config, f, indent=4)
        except: pass

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
            "<Configure>",
            lambda e: canvas.configure(
                scrollregion=canvas.bbox("all")
            )
        )

        window_id = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        def _configure_canvas(event):
            canvas.itemconfig(window_id, width=event.width)
        canvas.bind('<Configure>', _configure_canvas)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
            
        def _bind_mousewheel(event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
        def _unbind_mousewheel(event):
            canvas.unbind_all("<MouseWheel>")
            
        scrollable_frame.bind('<Enter>', _bind_mousewheel)
        scrollable_frame.bind('<Leave>', _unbind_mousewheel)
        
        return scrollable_frame

    def setup_lobby_tab(self):
        # Top Bar: Connection
        top_frame = ttk.Frame(self.lobby_tab, padding=5)
        top_frame.pack(fill="x")
        
        ttk.Label(top_frame, text="Game:").pack(side="left", padx=2)
        self.game_var = tk.StringVar()
        self.game_combo = ttk.Combobox(top_frame, textvariable=self.game_var, state="readonly", width=28)
        self.game_combo['values'] = ("Battlezone 98 Redux", "Battlezone Combat Commander")
        self.game_combo.current(0)
        self.game_combo.pack(side="left", padx=2)
        self.game_combo.bind("<<ComboboxSelected>>", self.on_game_select)
        
        ttk.Label(top_frame, text="Name:").pack(side="left", padx=2)
        self.name_var = tk.StringVar(value="BZMonitorUser")
        ttk.Entry(top_frame, textvariable=self.name_var, width=15).pack(side="left", padx=2)
        
        ttk.Label(top_frame, text="Host:").pack(side="left", padx=2)
        self.host_var = tk.StringVar(value="battlezone98mp.webdev.rebellion.co.uk:1337")
        ttk.Entry(top_frame, textvariable=self.host_var, width=40).pack(side="left", padx=2)
        
        ttk.Label(top_frame, text="Key:").pack(side="left", padx=2)
        self.key_var = tk.StringVar(value="")
        ttk.Entry(top_frame, textvariable=self.key_var, width=20).pack(side="left", padx=2)
        
        self.connect_btn = ttk.Button(top_frame, text="Connect", command=self.toggle_connection)
        self.connect_btn.pack(side="left", padx=5)
        
        self.status_var = tk.StringVar(value="Disconnected")
        ttk.Label(top_frame, textvariable=self.status_var, foreground=self.colors["accent"]).pack(side="right", padx=5)

        self.current_lobby_var = tk.StringVar(value="In Lounge")
        ttk.Label(top_frame, textvariable=self.current_lobby_var, foreground="cyan").pack(side="right", padx=5)

        # Action Bar: Lobby Controls
        action_frame = ttk.Frame(self.lobby_tab, padding=5)
        action_frame.pack(fill="x")
        
        ttk.Label(action_frame, text="New Lobby:").pack(side="left", padx=2)
        self.new_lobby_var = tk.StringVar(value="MyLobby")
        ttk.Entry(action_frame, textvariable=self.new_lobby_var, width=15).pack(side="left", padx=2)
        self.create_btn = ttk.Button(action_frame, text="Create", command=self.create_lobby)
        self.create_btn.pack(side="left", padx=2)
        
        ttk.Separator(action_frame, orient="vertical").pack(side="left", padx=5, fill="y")
        
        self.join_btn = ttk.Button(action_frame, text="Join Selected", command=self.join_selected_lobby)
        self.join_btn.pack(side="left", padx=2)
        self.leave_btn = ttk.Button(action_frame, text="Refresh Lounge", command=self.leave_or_refresh_lounge)
        self.leave_btn.pack(side="left", padx=2)
        self.steam_join_btn = ttk.Button(action_frame, text="Join (Steam)", command=self.join_steam_lobby)
        self.steam_join_btn.pack(side="left", padx=2)
        self.discord_status_btn = ttk.Button(action_frame, text="Post Status (Discord)", command=self.post_lobby_status)
        self.discord_status_btn.pack(side="left", padx=2)
        self.ping_btn = ttk.Button(action_frame, text="Ping", command=self.ping_server)
        self.ping_btn.pack(side="left", padx=2)
        self.debug_btn = ttk.Button(action_frame, text="RakNet Debug", command=self.open_raknet_debugger)
        self.debug_btn.pack(side="left", padx=2)

        # Filters
        ttk.Label(action_frame, text="| Filters:").pack(side="left", padx=5)
        self.filter_locked_var = tk.BooleanVar(value=self.config.get("filter_locked", False))
        self.filter_full_var = tk.BooleanVar(value=self.config.get("filter_full", False))
        ttk.Checkbutton(action_frame, text="Hide Locked", variable=self.filter_locked_var, command=self.refresh_tree).pack(side="left", padx=2)
        ttk.Checkbutton(action_frame, text="Hide Full", variable=self.filter_full_var, command=self.refresh_tree).pack(side="left", padx=2)

        # Main Content: PanedWindow
        paned = ttk.PanedWindow(self.lobby_tab, orient="vertical")
        paned.pack(fill="both", expand=True, padx=5, pady=5)
        
        # Top Pane: Lobby List
        lobby_frame = ttk.LabelFrame(paned, text="Lobbies", padding=5)
        paned.add(lobby_frame, weight=3)
        
        columns = ("ID", "Name", "Map", "Owner", "Players", "Type", "Version", "Locked", "Private")
        self.tree = ttk.Treeview(lobby_frame, columns=columns, show="headings")
        
        for col in columns:
            self.tree.heading(col, text=col, command=lambda c=col: self.sort_tree(c, False))
            self.tree.column(col, width=100)
        self.tree.column("Name", width=200)
        self.tree.column("Map", width=120)
        self.tree.column("ID", width=60)
        self.tree.column("Players", width=60)
        
        scrollbar = ttk.Scrollbar(lobby_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        self.tree.bind("<<TreeviewSelect>>", self.on_lobby_select)

        # Bottom Pane: Details & Logs
        bottom_pane = ttk.PanedWindow(paned, orient="horizontal")
        paned.add(bottom_pane, weight=2)
        
        # 1. Lobby Details (Left)
        details_frame = ttk.LabelFrame(bottom_pane, text="Lobby Details", padding=5)
        bottom_pane.add(details_frame, weight=1)
        
        self.preview_label = ttk.Label(details_frame, text="No Preview", anchor="center", background="#000000")
        self.preview_label.pack(side="top", fill="x", pady=(0, 5))
        
        self.lobby_details_text = tk.Text(details_frame, height=10, width=30, state="disabled", 
                                          bg="#050505", fg=self.colors["fg"], insertbackground=self.colors["highlight"], font=("Consolas", 9))
        self.lobby_details_text.pack(fill="both", expand=True)
        
        # 2. Chat & Logs (Middle)
        log_frame = ttk.LabelFrame(bottom_pane, text="Chat & Logs", padding=5)
        bottom_pane.add(log_frame, weight=2)
        
        self.log_text = tk.Text(log_frame, height=10, width=40, state="disabled",
                                bg="#050505", fg=self.colors["fg"], insertbackground=self.colors["highlight"], font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)
        
        chat_input_frame = ttk.Frame(log_frame)
        chat_input_frame.pack(fill="x", pady=2)
        
        self.chat_var = tk.StringVar()
        self.chat_entry = ttk.Entry(chat_input_frame, textvariable=self.chat_var)
        self.chat_entry.pack(side="left", fill="x", expand=True)
        self.chat_entry.bind("<Return>", self.send_chat)
        
        self.send_chat_btn = ttk.Button(chat_input_frame, text="Send", command=self.send_chat)
        self.send_chat_btn.pack(side="right", padx=2)

        # 3. Player Details (Right)
        player_frame = ttk.LabelFrame(bottom_pane, text="Player Details", padding=5)
        bottom_pane.add(player_frame, weight=1)
        
        self.player_details_text = tk.Text(player_frame, height=10, width=30, state="disabled",
                                           bg="#050505", fg=self.colors["fg"], insertbackground=self.colors["highlight"], font=("Consolas", 9))
        self.player_details_text.pack(fill="both", expand=True)
        self.player_details_text.bind("<Button-3>", self.show_player_context_menu)

        # Configure tags for links
        for widget in [self.lobby_details_text, self.player_details_text, self.log_text]:
            widget.tag_config("link", foreground=self.colors["accent"], underline=1)
            widget.tag_bind("link", "<Enter>", lambda e, w=widget: w.config(cursor="hand2"))
            widget.tag_bind("link", "<Leave>", lambda e, w=widget: w.config(cursor=""))
            widget.tag_bind("link", "<Button-1>", self.on_link_click)
            widget.tag_config("griefer", foreground="red", font=("Segoe UI", 9, "bold"))
            widget.tag_config("friend", foreground=self.colors["highlight"], font=("Segoe UI", 9, "bold"))

        # Chat specific tags
        self.log_text.tag_config("timestamp", foreground="#888888")
        self.log_text.tag_config("author", foreground=self.colors["highlight"], font=(self.custom_font_name, 9, "bold"))
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
        
        self.auto_reconnect_var = tk.BooleanVar(value=self.config.get("auto_reconnect", False))
        ttk.Checkbutton(conn_frame, text="Auto-Reconnect on Disconnect", variable=self.auto_reconnect_var, command=self.save_ui_config).pack(side="left")
        
        ttk.Label(conn_frame, text="Delay (s):").pack(side="left", padx=(10, 2))
        self.reconnect_delay_var = tk.IntVar(value=self.config.get("reconnect_delay", 10))
        ttk.Spinbox(conn_frame, from_=1, to=300, textvariable=self.reconnect_delay_var, width=5, command=self.save_ui_config).pack(side="left")
        
        # --- Proxy Settings ---
        proxy_frame = ttk.LabelFrame(container, text="Public IP Masking (Proxy)", padding=10)
        proxy_frame.pack(fill="x", pady=5)
        
        self.proxy_enabled_var = tk.BooleanVar(value=self.config["proxy_enabled"])
        ttk.Checkbutton(proxy_frame, text="Enable Proxy Connection (BZ98R WebSocket Only)", variable=self.proxy_enabled_var, command=self.save_ui_config).pack(anchor="w")
        
        self.ip_safety_var = tk.BooleanVar(value=self.config.get("ip_safety", False))
        ttk.Checkbutton(proxy_frame, text="IP Safety (Block connection if proxy fails)", variable=self.ip_safety_var, command=self.save_ui_config).pack(anchor="w")
        
        p_grid = ttk.Frame(proxy_frame)
        p_grid.pack(fill="x", pady=5)
        
        ttk.Label(p_grid, text="Host:").pack(side="left")
        self.proxy_host_var = tk.StringVar(value=self.config["proxy_host"])
        ttk.Entry(p_grid, textvariable=self.proxy_host_var, width=20).pack(side="left", padx=5)
        
        ttk.Label(p_grid, text="Port:").pack(side="left")
        self.proxy_port_var = tk.StringVar(value=self.config["proxy_port"])
        ttk.Entry(p_grid, textvariable=self.proxy_port_var, width=8).pack(side="left", padx=5)
        
        ttk.Button(p_grid, text="Find Free Proxy", command=self.find_free_proxy).pack(side="left", padx=10)
        ttk.Button(p_grid, text="Use Tor", command=self.set_tor_proxy).pack(side="left", padx=5)
        ttk.Button(p_grid, text="Test Proxy", command=self.test_proxy).pack(side="left", padx=5)
        
        self.tor_status_var = tk.StringVar(value="Tor: Stopped")
        self.tor_status_label = tk.Label(p_grid, textvariable=self.tor_status_var, fg="#666666", bg=self.colors["bg"], font=("Segoe UI", 9))
        self.tor_status_label.pack(side="left", padx=5)
        
        ttk.Label(p_grid, text="Status:").pack(side="left", padx=(10, 2))
        self.proxy_status_canvas = tk.Canvas(p_grid, width=20, height=20, highlightthickness=0, bg=self.colors["bg"])
        self.proxy_status_canvas.pack(side="left")
        self.proxy_status_light = self.proxy_status_canvas.create_oval(4, 4, 16, 16, fill="gray", outline="#666")
        
        # --- Window Settings ---
        win_frame = ttk.LabelFrame(container, text="Window Behavior", padding=10)
        win_frame.pack(fill="x", pady=5)
        
        self.min_close_var = tk.BooleanVar(value=self.config["minimize_on_close"])
        ttk.Checkbutton(win_frame, text="Minimize to Taskbar on Close (Passive Mode)", variable=self.min_close_var, command=self.apply_config).pack(anchor="w")
        self.startup_var = tk.BooleanVar(value=self.config.get("run_on_startup", False))
        ttk.Checkbutton(win_frame, text="Run on Windows Startup", variable=self.startup_var, command=self.apply_config).pack(anchor="w")
        
        ttk.Button(win_frame, text="Quit Application", command=self.quit_app).pack(anchor="w", pady=5)
        
        # --- Logging Settings ---
        log_frame = ttk.LabelFrame(container, text="Logging & Analytics", padding=10)
        log_frame.pack(fill="x", pady=5)
        
        self.log_enabled_var = tk.BooleanVar(value=self.config["logging_enabled"])
        ttk.Checkbutton(log_frame, text="Enable Chat/Event Logging (File)", variable=self.log_enabled_var, command=self.save_ui_config).pack(anchor="w")
        
        folder_frame = ttk.Frame(log_frame)
        folder_frame.pack(fill="x", pady=2)
        ttk.Label(folder_frame, text="Log Folder:").pack(side="left")
        self.log_folder_var = tk.StringVar(value=self.config.get("log_folder", ""))
        ttk.Entry(folder_frame, textvariable=self.log_folder_var).pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(folder_frame, text="Browse", command=self.browse_log_folder).pack(side="left")
        
        ret_frame = ttk.Frame(log_frame)
        ret_frame.pack(fill="x", pady=2)
        ttk.Label(ret_frame, text="Log Retention (Days):").pack(side="left")
        self.log_ret_var = tk.IntVar(value=self.config["log_retention"])
        ttk.Spinbox(ret_frame, from_=1, to=365, textvariable=self.log_ret_var, width=5, command=self.save_ui_config).pack(side="left", padx=5)
        
        self.stats_enabled_var = tk.BooleanVar(value=self.config["stats_enabled"])
        ttk.Checkbutton(log_frame, text="Enable Game Stats Logging (CSV for Charts)", variable=self.stats_enabled_var, command=self.toggle_stats_logging).pack(anchor="w", pady=5)
        
        # --- Alerts Settings ---
        alert_frame = ttk.LabelFrame(container, text="Alerts & Notifications", padding=10)
        alert_frame.pack(fill="x", pady=5)
        
        self.alert_new_lobby_var = tk.BooleanVar(value=self.config.get("alert_new_lobby", False))
        ttk.Checkbutton(alert_frame, text="Alert on New Lobby Created", variable=self.alert_new_lobby_var, command=self.save_ui_config).pack(anchor="w")
        
        self.alert_player_join_var = tk.BooleanVar(value=self.config.get("alert_player_join", False))
        ttk.Checkbutton(alert_frame, text="Alert on Player Join (Any Lobby)", variable=self.alert_player_join_var, command=self.save_ui_config).pack(anchor="w")
        
        self.alert_watch_only_var = tk.BooleanVar(value=self.config.get("alert_watch_only", False))
        ttk.Checkbutton(alert_frame, text="Only Alert for Watched Players", variable=self.alert_watch_only_var, command=self.save_ui_config).pack(anchor="w", padx=20)
        
        ttk.Label(alert_frame, text="Watched Players (Name or ID, one per line):").pack(anchor="w", padx=20)
        self.watch_list_text = tk.Text(alert_frame, height=4, width=40, 
                                       bg="#1a1a1a", fg=self.colors["accent"], insertbackground=self.colors["highlight"], font=("Consolas", 9))
        self.watch_list_text.pack(fill="x", padx=20, pady=(0, 5))
        self.watch_list_text.insert("1.0", self.config.get("watch_list", ""))
        
        self.alert_griefer_var = tk.BooleanVar(value=self.config.get("alert_griefer", False))
        ttk.Checkbutton(alert_frame, text="Alert on CaptChoes (Known Griefer/Troll)", variable=self.alert_griefer_var, command=self.save_ui_config).pack(anchor="w")
        
        self.alert_disconnect_var = tk.BooleanVar(value=self.config.get("alert_disconnect", False))
        ttk.Checkbutton(alert_frame, text="Alert on Connection Lost", variable=self.alert_disconnect_var, command=self.save_ui_config).pack(anchor="w")
        
        opts_frame = ttk.Frame(alert_frame)
        opts_frame.pack(fill="x", pady=5)
        self.alert_sound_var = tk.BooleanVar(value=self.config.get("alert_sound", False))
        ttk.Checkbutton(opts_frame, text="Play Sound", variable=self.alert_sound_var, command=self.save_ui_config).pack(side="left", padx=(0, 10))
        self.alert_flash_var = tk.BooleanVar(value=self.config.get("alert_flash", False))
        ttk.Checkbutton(opts_frame, text="Flash Window", variable=self.alert_flash_var, command=self.save_ui_config).pack(side="left")

        # --- Social & Audio ---
        social_frame = ttk.LabelFrame(container, text="Social & Audio", padding=10)
        social_frame.pack(fill="x", pady=5)

        ttk.Label(social_frame, text="Friend List (Name or ID, one per line):").pack(anchor="w")
        self.friend_list_text = tk.Text(social_frame, height=4, width=40, 
                                       bg="#1a1a1a", fg=self.colors["highlight"], insertbackground=self.colors["highlight"], font=("Consolas", 9))
        self.friend_list_text.pack(fill="x", pady=(0, 5))
        self.friend_list_text.insert("1.0", self.config.get("friend_list", ""))
        
        # --- Security ---
        sec_frame = ttk.LabelFrame(container, text="Security & Auto-Ban", padding=10)
        sec_frame.pack(fill="x", pady=5)
        ttk.Label(sec_frame, text="Auto-Ban List (Name, ID, or IP - one per line):").pack(anchor="w")
        self.ban_list_text = tk.Text(sec_frame, height=4, width=40, 
                                       bg="#1a1a1a", fg="#ff5555", insertbackground=self.colors["highlight"], font=("Consolas", 9))
        self.ban_list_text.pack(fill="x", pady=(0, 5))
        self.ban_list_text.insert("1.0", self.config.get("ban_list", ""))

        ttk.Label(social_frame, text="Custom Audio Alerts (.wav):").pack(anchor="w", pady=(5, 0))
        
        def browse_wav(var):
            f = filedialog.askopenfilename(filetypes=[("WAV Audio", "*.wav")])
            if f: var.set(f)
            self.save_ui_config()

        for lbl, key in [("Player Join:", "sound_join"), ("Chat Mention:", "sound_mention"), ("Griefer:", "sound_griefer")]:
            f = ttk.Frame(social_frame)
            f.pack(fill="x", pady=1)
            ttk.Label(f, text=lbl, width=15).pack(side="left")
            var = tk.StringVar(value=self.config.get(key, ""))
            setattr(self, f"{key}_var", var)
            ttk.Entry(f, textvariable=var).pack(side="left", fill="x", expand=True, padx=5)
            ttk.Button(f, text="...", width=3, command=lambda v=var: browse_wav(v)).pack(side="left")

    def setup_discord_tab(self):
        container = ttk.Frame(self.discord_tab, padding=20)
        container.pack(fill="both", expand=True)
        
        # Settings
        settings_frame = ttk.LabelFrame(container, text="Discord Settings", padding=10)
        settings_frame.pack(fill="x", pady=5)
        
        self.discord_enabled_var = tk.BooleanVar(value=self.config.get("discord_enabled", False))
        ttk.Checkbutton(settings_frame, text="Enable Discord Relay", variable=self.discord_enabled_var, command=self.toggle_discord_relay).pack(anchor="w")
        
        grid = ttk.Frame(settings_frame)
        grid.pack(fill="x", pady=5)
        
        ttk.Label(grid, text="Bot Token:").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        self.discord_token_var = tk.StringVar(value=self.config.get("discord_token", ""))
        ttk.Entry(grid, textvariable=self.discord_token_var, width=50, show="*").grid(row=0, column=1, sticky="w", padx=5, pady=2)
        
        ttk.Label(grid, text="Channel ID:").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        self.discord_channel_id_var = tk.StringVar(value=self.config.get("discord_channel_id", ""))
        ttk.Entry(grid, textvariable=self.discord_channel_id_var, width=20).grid(row=1, column=1, sticky="w", padx=5, pady=2)
        
        ttk.Label(grid, text="Lobby ID to Relay:").grid(row=2, column=0, sticky="w", padx=5, pady=2)
        self.discord_lobby_id_var = tk.StringVar(value=self.config.get("discord_lobby_id", ""))
        ttk.Entry(grid, textvariable=self.discord_lobby_id_var, width=20).grid(row=2, column=1, sticky="w", padx=5, pady=2)
        
        # Options
        opts_frame = ttk.LabelFrame(container, text="Relay Options", padding=10)
        opts_frame.pack(fill="x", pady=5)
        
        self.discord_to_discord_var = tk.BooleanVar(value=self.config.get("discord_relay_to_discord", True))
        ttk.Checkbutton(opts_frame, text="Relay Lobby Chat -> Discord", variable=self.discord_to_discord_var, command=self.save_ui_config).pack(anchor="w")
        
        self.discord_to_lobby_var = tk.BooleanVar(value=self.config.get("discord_relay_to_lobby", True))
        ttk.Checkbutton(opts_frame, text="Relay Discord Chat -> Lobby", variable=self.discord_to_lobby_var, command=self.save_ui_config).pack(anchor="w")
        
        btn_frame = ttk.Frame(container)
        btn_frame.pack(fill="x", pady=10)
        ttk.Button(btn_frame, text="Test Discord Connection", command=self.test_discord_connection).pack(side="left")
        ttk.Button(btn_frame, text="Save Discord Config", command=self.save_ui_config).pack(side="left", padx=10)

    def setup_bot_tab(self):
        scroll_frame = self.create_scrollable_frame(self.bot_tab)
        container = ttk.Frame(scroll_frame, padding=20)
        container.pack(fill="both", expand=True)
        
        # Auto-Greeter
        greet_frame = ttk.LabelFrame(container, text="Auto-Greeter", padding=10)
        greet_frame.pack(fill="x", pady=5)
        
        self.bot_enabled_var = tk.BooleanVar(value=self.config.get("bot_enabled", False))
        ttk.Checkbutton(greet_frame, text="Enable Auto-Welcome (When hosting/present)", variable=self.bot_enabled_var, command=self.save_ui_config).pack(anchor="w")
        
        ttk.Label(greet_frame, text="Message ({player} = username):").pack(anchor="w", pady=(5,0))
        self.bot_welcome_var = tk.StringVar(value=self.config.get("bot_welcome_msg", ""))
        ttk.Entry(greet_frame, textvariable=self.bot_welcome_var, width=60).pack(fill="x", pady=2)
        
        cd_frame = ttk.Frame(greet_frame)
        cd_frame.pack(fill="x", pady=2)
        ttk.Label(cd_frame, text="Cooldown (s):").pack(side="left")
        self.bot_welcome_cooldown_var = tk.IntVar(value=self.config.get("bot_welcome_cooldown", 60))
        ttk.Spinbox(cd_frame, from_=0, to=3600, textvariable=self.bot_welcome_cooldown_var, width=5).pack(side="left", padx=5)
        
        # Announcements
        ann_frame = ttk.LabelFrame(container, text="Timed Announcements", padding=10)
        ann_frame.pack(fill="x", pady=5)
        
        self.bot_announce_enabled_var = tk.BooleanVar(value=self.config.get("bot_announce_enabled", False))
        ttk.Checkbutton(ann_frame, text="Enable Timed Announcements", variable=self.bot_announce_enabled_var, command=self.save_ui_config).pack(anchor="w")
        
        ttk.Label(ann_frame, text="Message:").pack(anchor="w", pady=(5,0))
        self.bot_announce_msg_var = tk.StringVar(value=self.config.get("bot_announce_msg", ""))
        ttk.Entry(ann_frame, textvariable=self.bot_announce_msg_var, width=60).pack(fill="x", pady=2)
        
        ttk.Label(ann_frame, text="Interval (Minutes):").pack(anchor="w", pady=(5,0))
        self.bot_announce_int_var = tk.IntVar(value=self.config.get("bot_announce_interval", 5))
        ttk.Spinbox(ann_frame, from_=1, to=120, textvariable=self.bot_announce_int_var, width=5).pack(anchor="w", pady=2)
        
        # Auto-Claim
        claim_frame = ttk.LabelFrame(container, text="Auto-Claim Lobby", padding=10)
        claim_frame.pack(fill="x", pady=5)
        
        self.auto_claim_enabled_var = tk.BooleanVar(value=self.config.get("auto_claim_enabled", False))
        ttk.Checkbutton(claim_frame, text="Enable Auto-Claim (Recreate if missing)", variable=self.auto_claim_enabled_var, command=self.save_ui_config).pack(anchor="w")
        
        ttk.Label(claim_frame, text="Lobby Name:").pack(side="left")
        self.auto_claim_name_var = tk.StringVar(value=self.config.get("auto_claim_name", "default"))
        ttk.Entry(claim_frame, textvariable=self.auto_claim_name_var, width=20).pack(side="left", padx=5)
        
        ttk.Label(claim_frame, text="Bot Name:").pack(side="left", padx=(5, 0))
        self.auto_claim_bot_name_var = tk.StringVar(value=self.config.get("auto_claim_bot_name", ""))
        ttk.Entry(claim_frame, textvariable=self.auto_claim_bot_name_var, width=15).pack(side="left", padx=5)
        
        # RPC Settings
        rpc_frame = ttk.LabelFrame(container, text="Discord Rich Presence (Local)", padding=10)
        rpc_frame.pack(fill="x", pady=5)
        
        self.rpc_enabled_var = tk.BooleanVar(value=self.config.get("rpc_enabled", False))
        ttk.Checkbutton(rpc_frame, text="Enable Rich Presence", variable=self.rpc_enabled_var, command=self.toggle_rpc).pack(anchor="w")
        
        ttk.Label(rpc_frame, text="Client ID (Optional):").pack(anchor="w")
        self.rpc_id_var = tk.StringVar(value=self.config.get("rpc_client_id", "133570000000000000"))
        ttk.Entry(rpc_frame, textvariable=self.rpc_id_var).pack(fill="x", pady=2)
        
        ttk.Button(container, text="Save Bot Settings", command=self.save_ui_config).pack(pady=10)

    def setup_stats_tab(self):
        container = ttk.Frame(self.stats_tab, padding=10)
        container.pack(fill="both", expand=True)
        
        ctrl = ttk.Frame(container)
        ctrl.pack(fill="x", pady=5)
        ttk.Button(ctrl, text="Refresh Graph", command=self.draw_stats).pack(side="left")
        ttk.Label(ctrl, text="Active Players (Last 24h)").pack(side="left", padx=10)
        
        self.stats_canvas = tk.Canvas(container, bg="#1a1a1a", highlightthickness=0)
        self.stats_canvas.pack(fill="both", expand=True)
        self.stats_canvas.bind("<Configure>", lambda e: self.draw_stats())

    def setup_about_tab(self):
        container = ttk.Frame(self.about_tab, padding=40)
        container.pack(fill="both", expand=True)
        
        # Title
        ttk.Label(container, text="Battlezone Redux Lobby Monitor", 
                 font=(self.custom_font_name, 20, "bold"), 
                 foreground=self.colors["highlight"]).pack(pady=(0, 30))
        
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
        
        lbl = ttk.Label(info_frame, text=disclaimer_text, font=(self.custom_font_name, 11), 
                        wraplength=600, justify="center")
        lbl.pack(expand=True)
        
        ttk.Label(container, text="Developed by the Community", 
                 font=(self.custom_font_name, 9, "italic"), 
                 foreground="gray").pack(side="bottom", pady=10)

    def sort_tree(self, col, reverse):
        l = [(self.tree.set(k, col), k) for k in self.tree.get_children('')]
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
        self.config["reconnect_delay"] = self.reconnect_delay_var.get()
        self.config["proxy_host"] = self.proxy_host_var.get()
        self.config["proxy_port"] = self.proxy_port_var.get()
        # proxy_type is managed by buttons, not directly exposed in this UI save
        self.config["minimize_on_close"] = self.min_close_var.get()
        self.config["logging_enabled"] = self.log_enabled_var.get()
        self.config["log_retention"] = self.log_ret_var.get()
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
        self.config["bot_welcome_cooldown"] = self.bot_welcome_cooldown_var.get()
        self.config["bot_announce_enabled"] = self.bot_announce_enabled_var.get()
        self.config["bot_announce_msg"] = self.bot_announce_msg_var.get()
        self.config["bot_announce_interval"] = self.bot_announce_int_var.get()
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
        if sys.platform == 'win32':
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)
                if enable:
                    if getattr(sys, 'frozen', False):
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
        elif sys.platform.startswith('linux'):
            try:
                autostart_dir = os.path.expanduser("~/.config/autostart")
                desktop_file = os.path.join(autostart_dir, "bzr_monitor.desktop")
                if enable:
                    if not os.path.exists(autostart_dir): os.makedirs(autostart_dir)
                    exec_cmd = sys.executable if getattr(sys, 'frozen', False) else f"{sys.executable} {os.path.abspath(sys.argv[0])}"
                    content = f"[Desktop Entry]\nType=Application\nName=Battlezone Lobby Monitor\nExec={exec_cmd}\nHidden=false\nNoDisplay=false\nX-GNOME-Autostart-enabled=true\nComment=Start BZR Monitor\n"
                    with open(desktop_file, "w") as f: f.write(content)
                else:
                    if os.path.exists(desktop_file): os.remove(desktop_file)
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
        
        if alert_type == "new_lobby" and self.config.get("alert_new_lobby"): should_alert = True
        elif alert_type == "player_join" and self.config.get("alert_player_join"):
            sound_key = "sound_join"
            if self.config.get("alert_watch_only"):
                watch_list = self.config.get("watch_list", "").lower().splitlines()
                # data is player name
                if data and any(w.strip() in str(data).lower() for w in watch_list if w.strip()):
                    should_alert = True
            else:
                should_alert = True
        elif alert_type == "griefer_join" and self.config.get("alert_griefer"): should_alert = True
        elif alert_type == "griefer_join" and self.config.get("alert_griefer"): 
            should_alert = True
            sound_key = "sound_griefer"
        elif alert_type == "disconnect" and self.config.get("alert_disconnect"): should_alert = True
            
        if should_alert:
            if self.config.get("alert_sound"): self.root.bell()
            if self.config.get("alert_sound"): 
                self.play_custom_sound(sound_key)
            if self.config.get("alert_flash"): self.flash_window()

    def play_custom_sound(self, config_key):
        played = False
        if config_key:
            path = self.config.get(config_key, "")
            if path and os.path.exists(path) and sys.platform == 'win32':
                try:
                    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                    played = True
                except: pass
        
        if not played:
            self.root.bell()

    def flash_window(self):
        if sys.platform == 'win32':
            try:
                ctypes.windll.user32.FlashWindow(int(self.root.wm_frame(), 16), True)
            except: pass
        else:
            try:
                self.root.wm_attributes("-demands-attention", True)
            except: pass

    def quit_app(self):
        self.should_run = False
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
        if not HAS_TRAY: return
        
        def show_window(icon, item):
            self.root.after(0, self.root.deiconify)

        def quit_tray(icon, item):
            self.root.after(0, self.quit_app)

        image = None
        if HAS_PIL:
            # Create a simple icon if none exists
            try:
                # Try to load existing icon
                base_dir = os.path.dirname(os.path.abspath(__file__))
                icon_path = os.path.join(base_dir, "bzrmon.ico")
                if not os.path.exists(icon_path):
                    icon_path = os.path.join(base_dir, "bzrtex.ico") # Reuse existing if available
                if not os.path.exists(icon_path):
                    icon_path = os.path.join(base_dir, "wb.ico")
                
                if os.path.exists(icon_path):
                    image = Image.open(icon_path)
                else:
                    image = Image.new('RGB', (64, 64), color = (0, 255, 0))
            except: pass

        if image:
            menu = (item('Show', show_window), item('Quit', quit_tray))
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
        msg = {"type": "CreateLobby", "content": {
            "name": full_name, 
            "isPrivate": False, 
            "memberLimit": 20000,
            "password": ""
        }}
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
            
        val = self.tree.item(selected_items[0])['values'][0]
        try:
            lid = int(val)
        except ValueError:
            lid = str(val)
            
        msg = {"type": "DoJoinLobby", "content": {"id": lid, "password": ""}}
        if self.ws and self.connected:
            self.ws.send(json.dumps(msg))
            self.log(f"Requesting Join Lobby: {lid}")

    def join_steam_lobby(self):
        selected_items = self.tree.selection()
        if not selected_items:
            self.flash_button_text(self.steam_join_btn, "Select Lobby")
            return
            
        lid = str(self.tree.item(selected_items[0])['values'][0])
        lobby = self.lobbies.get(lid)
        if not lobby: return
        
        host_steam_id = '76561198104781489' # Default fallback
        owner_id = str(lobby.get("owner", ""))
        
        if owner_id.startswith('S'):
             host_steam_id = owner_id[1:]
        
        url = f"steam://rungame/301650/{host_steam_id}/+connect_lobby=B{lid}"
        self.log(f"Opening Steam URL: {url}")
        webbrowser.open(url)

    def show_player_context_menu(self, event):
        try:
            index = self.player_details_text.index(f"@{event.x},{event.y}")
            line = self.player_details_text.get(f"{index} linestart", f"{index} lineend")
            match = re.search(r" - (.*?) \(ID: (.*?)\)", line)
            if match:
                name = match.group(1)
                uid = match.group(2)
                menu = tk.Menu(self.root, tearoff=0)
                menu.add_command(label=f"Add '{name}' to Watch List", command=lambda: self.add_to_watch_list(name))
                menu.add_command(label=f"Add ID '{uid}' to Watch List", command=lambda: self.add_to_watch_list(uid))
                
                menu.add_command(label=f"Add '{name}' to Friends", command=lambda: self.add_to_friend_list(name))
                menu.add_command(label=f"Add '{name}' to Ban List", command=lambda: self.add_to_ban_list(name))
                
                menu.add_separator()
                menu.add_command(label=f"Whisper '{name}'", command=lambda: self.whisper_user(name))
                
                if uid in self.muted_users:
                    menu.add_command(label=f"Unmute '{name}'", command=lambda: self.toggle_mute(uid, name))
                else:
                    menu.add_command(label=f"Mute '{name}'", command=lambda: self.toggle_mute(uid, name))
                
                # Check ownership
                if self.current_lobby_id is not None:
                    lobby = self.lobbies.get(str(self.current_lobby_id))
                    if lobby:
                        owner = str(lobby.get("owner"))
                        if str(self.my_id) == owner and str(self.my_id) != str(uid):
                             menu.add_separator()
                             menu.add_command(label=f"Kick '{name}'", command=lambda: self.kick_user(uid, name))
                
                menu.post(event.x_root, event.y_root)
        except Exception as e: pass

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
        msg = simpledialog.askstring("Whisper", f"Message to {name}:")
        if msg:
            # BZ98R uses /t for tell/whisper usually
            self.ws.send(json.dumps({"type": "DoSendChat", "content": f"/t {name} {msg}"}))
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
        if messagebox.askyesno("Kick User", f"Are you sure you want to kick {name}?"):
            self.ws.send(json.dumps({"type": "DoKickUser", "content": int(uid) if str(uid).isdigit() else uid}))
            self.log(f"Kicked {name}")

    def leave_or_refresh_lounge(self):
        if not self.connected:
            self.flash_button_text(self.leave_btn, "Not Connected")
            return
            
        if self.current_lobby_id is not None:
            msg = {"type": "DoExitLobby", "content": self.current_lobby_id}
            self.log(f"Requesting Exit Lobby: {self.current_lobby_id}")
        else:
            msg = {"type": "DoEnterLounge", "content": True}
            self.log("Requesting Refresh Lounge...")
        
        if self.ws and self.connected:
            self.ws.send(json.dumps(msg))

    def send_chat(self, event=None):
        if not self.connected:
            if hasattr(self, 'send_chat_btn'):
                self.flash_button_text(self.send_chat_btn, "Not Connected")
            return
            
        text = self.chat_var.get()
        if not text:
            if hasattr(self, 'send_chat_btn'):
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
                    webbrowser.open(url)
        except Exception as e:
            self.log(f"Error opening link: {e}")

    def insert_link(self, widget, text, url):
        widget.insert("end", text, ("link", f"url:{url}"))

    def log(self, message):
        self.root.after(0, lambda: self._log_impl(message))

    def log_chat(self, author, text):
        self.root.after(0, lambda: self._log_chat_impl(author, text))

    def _log_chat_impl(self, author, text):
        self.log_text.config(state="normal")
        
        # Timestamp & Author
        ts = datetime.now().strftime("[%H:%M:%S]")
        self.log_text.insert("end", f"{ts} ", "timestamp")
        self.log_text.insert("end", f"[{author}]: ", "author")
        
        # Parse URLs and Mentions
        parts = re.split(r'(https?://\S+)', text)
        my_name = self.name_var.get().lower()
        
        for part in parts:
            if part.startswith("http"):
                self.log_text.insert("end", part, ("link", f"url:{part}"))
            else:
                # Check mentions (case-insensitive)
                if my_name and my_name in part.lower():
                    self.log_text.insert("end", part, "mention")
                    if self.config.get("alert_sound"): self.play_custom_sound("sound_mention")
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
                folder = self.config.get("log_folder", "")
                if not folder or not os.path.exists(folder):
                    folder = "."
                filename = os.path.join(folder, f"bzr_log_{datetime.now().strftime('%Y-%m-%d')}.txt")
                timestamp = datetime.now().strftime("[%H:%M:%S]")
                with open(filename, "a", encoding="utf-8") as f:
                    f.write(f"{timestamp} {message}\n")
            except: pass

    def toggle_connection(self):
        if self.connected:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        host = self.host_var.get()
        if not host:
            messagebox.showerror("Error", "Host is required")
            return
            
        if self.config.get("ip_safety", False):
            if not self.config.get("proxy_enabled", False):
                messagebox.showerror("IP Safety", "IP Safety is enabled but Proxy is disabled.\nConnection blocked.")
                return
            
            p_host = self.config.get("proxy_host", "")
            p_port = self.config.get("proxy_port", "")
            if not p_host or not p_port:
                messagebox.showerror("IP Safety", "Proxy configuration missing.\nConnection blocked.")
                return
                
            self.log("IP Safety: Verifying proxy...")
            self.root.update()
            if not self._test_proxy_connection(p_host, p_port):
                self._set_proxy_indicator(False)
                messagebox.showerror("IP Safety", "Proxy connection failed.\nConnection blocked.")
                self.log("IP Safety: Proxy check failed.")
                return
            self._set_proxy_indicator(True)
            self.log("IP Safety: Proxy verified.")
            
        self.should_run = True
        self.connect_btn.config(text="Disconnect")
        
        game = self.game_var.get()
        if game == "Battlezone 98 Redux":
            url = f"ws://{host}"
            self.log(f"Connecting to {url}...")
            self.status_var.set("Connecting...")
            self.ws_thread = threading.Thread(target=self.run_ws, args=(url,))
        elif host.startswith("http"):
            self.log(f"Starting BZCC HTTP Monitor on {host}...")
            self.status_var.set("Monitoring (HTTP)...")
            self.ws_thread = threading.Thread(target=self.run_bzcc_http, args=(host,))
        else:
            self.log(f"Starting RakNet Monitor on {host}...")
            self.status_var.set("Monitoring (UDP)...")
            self.ws_thread = threading.Thread(target=self.run_raknet, args=(host,))
            
        self.ws_thread.daemon = True
        self.ws_thread.start()

    def disconnect(self):
        self.should_run = False
        if self.ws:
            self.ws.close()
        self.connected = False
        self.connect_btn.config(text="Connect")
        self.status_var.set("Disconnected")
        self.log("Disconnected.")

    def open_raknet_debugger(self):
        win = tk.Toplevel(self.root)
        win.title("RakNet Packet Debugger")
        win.geometry("600x400")
        
        # Controls
        ctrl = ttk.Frame(win, padding=10)
        ctrl.pack(fill="x")
        
        ttk.Label(ctrl, text="Host:").pack(side="left")
        host_val = self.host_var.get()
        if "http" in host_val: host_val = "battlezone99mp.webdev.rebellion.co.uk:61111"
        host_ent = ttk.Entry(ctrl, width=30)
        host_ent.insert(0, host_val)
        host_ent.pack(side="left", padx=5)
        
        ttk.Label(ctrl, text="Hex Payload:").pack(side="left")
        payload_ent = ttk.Entry(ctrl, width=30)
        payload_ent.insert(0, "01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00") # Default Ping
        payload_ent.pack(side="left", padx=5)
        
        # Log
        log_area = tk.Text(win, bg="#000", fg="#0f0", font=("Consolas", 9))
        log_area.pack(fill="both", expand=True, padx=10, pady=10)
        
        def clean_payload(p_str):
            # Remove whitespace/newlines
            clean = re.sub(r'[^0-9a-fA-F]', '', p_str)
            
            # Heuristic: Detect UDP Header targeting port 61111 (0xeeb7)
            # If user pasted full Wireshark dump, look for the destination port
            if len(clean) > 60 and "eeb7" in clean.lower():
                idx = clean.lower().find("eeb7")
                # UDP Header: Src(2) Dst(2) Len(2) Sum(2) -> Payload
                # 'eeb7' is Dst(2). Payload starts 4 bytes (8 chars) after it.
                payload_idx = idx + 8
                if payload_idx < len(clean):
                    extracted = clean[payload_idx:]
                    log_area.insert("end", f"Auto-Stripped Headers. Payload: {len(extracted)//2} bytes\n")
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
                    log_area.insert("end", "-"*40 + "\n")
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
            except:
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
        ttk.Button(ctrl, text="0x09", width=5, command=load_connect).pack(side="left", padx=2)
        ttk.Button(ctrl, text="0x13", width=5, command=load_login).pack(side="left", padx=2)
        ttk.Button(ctrl, text="0x60", width=5, command=load_query).pack(side="left", padx=2)
        ttk.Button(ctrl, text="Set as Monitor Query", command=set_query).pack(side="left", padx=5)

    def register_direct_lobby(self, addr, status="Online"):
        lid = f"direct_{addr[0]}_{addr[1]}"
        self.lobbies[lid] = {
            "id": lid,
            "metadata": {
                "name": f"Direct: {addr[0]}", 
                "gameType": "BZCC (RakNet)", 
                "map": status,
                "gameSettings": "*Unknown*"
            },
            "users": {},
            "memberLimit": 0,
            "isLocked": False,
            "isPrivate": False,
            "owner": "Unknown"
        }
        self.root.after(0, self.refresh_tree)

    def parse_raknet_frames(self, data):
        try:
            frames = []
            offset = 4 # Skip ID(1) + Seq(3)
            while offset < len(data):
                flags = data[offset]
                offset += 1
                reliability = (flags >> 5) & 0x07
                is_split = (flags & 0x10) != 0
                
                if offset + 2 > len(data): break
                length_bits = (data[offset] << 8) | data[offset+1]
                offset += 2
                length_bytes = (length_bits + 7) // 8
                
                if reliability in [2, 3, 4]: offset += 3 # Reliable Message Number
                if reliability in [1, 4]: offset += 4 # Sequencing Index (3) + Order Channel (1)
                if reliability == 3: offset += 4 # Ordering Index (3) + Order Channel (1)
                if is_split: offset += 10
                
                if offset + length_bytes > len(data): break
                frames.append(data[offset : offset+length_bytes])
                offset += length_bytes
            return frames
        except: return []

    def patch_raknet_packet(self, pkt, new_rel_seq=None):
        if len(pkt) < 10: return pkt
        
        # 1. Parse Header to find Body Offset
        flags = pkt[4]
        reliability = (flags >> 5) & 0x07
        is_split = (flags & 0x10) != 0
        
        header_len = 7 # ID(1)+Seq(3)+Flags(1)+Len(2)
        has_rel_seq = reliability in [2, 3, 4]
        
        if has_rel_seq:
            if new_rel_seq is not None:
                # Patch Reliable Sequence Number (Bytes 7,8,9)
                rel_bytes = new_rel_seq.to_bytes(3, 'little')
                pkt = pkt[:7] + rel_bytes + pkt[10:]
            header_len += 3
            
        if reliability in [1, 4]: header_len += 4
        if reliability == 3: header_len += 4
        if is_split: header_len += 10
            
        if len(pkt) <= header_len: return pkt
        
        # 2. Patch GUID only for Connection Request (0x09)
        msg_id = pkt[header_len]
        if msg_id == 0x09:
            # 0x09 Structure: ID(1) + GUID(8) + Time(8) + Sec(1)
            guid_offset = header_len + 1
            if len(pkt) >= guid_offset + 8:
                pkt = pkt[:guid_offset] + self.client_guid + pkt[guid_offset+8:]
                
        return pkt

    def run_raknet(self, host_str):
        host, port = host_str.split(":") if ":" in host_str else (host_str, 61111)
        try:
            port = int(port)
        except ValueError:
            port = 61111
            
        self.connected = True
        self.root.after(0, lambda: self.status_var.set("Monitoring (UDP)"))
        
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        
        # RakNet Unconnected Ping Structure:
        magic = b'\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78'
        # Generate a persistent GUID for this session
        self.client_guid = random.getrandbits(64).to_bytes(8, 'big')
        
        def make_ping(ptype):
            current_time = int(time.time() * 1000) & 0xFFFFFFFFFFFFFFFF
            return ptype + current_time.to_bytes(8, 'big') + magic + self.client_guid

        def make_ocr1():
            # Open Connection Request 1 (0x05)
            # ID(1) + Magic(16) + Protocol(1) + Padding(1446) = 1464 bytes
            return b'\x05' + magic + b'\x06' + (b'\x00' * 1446)

        def make_ocr2(server_addr_bytes, mtu):
            # Open Connection Request 2 (0x07)
            # ID(1) + Magic(16) + ServerAddress(7) + MTU(2) + ClientGUID(8)
            return b'\x07' + magic + server_addr_bytes + mtu + self.client_guid

        pkt_mode = 0 # 0:0x01, 1:0x02, 2:0x05, 3:0x07, 4:Connected
        server_mtu = b'\x05\xd4' # Default 1492
        last_send_time = 0
        self.tx_seq = 0
        self.tx_rel_seq = -1
        query_log_counter = 0
        
        # Packet Templates (Raw)
        pkt_connect = bytes.fromhex("8400000040009000000009040000001384b9fa00000000000503e100")
        pkt_login = bytes.fromhex("840000006002f00000000000000013047f000001456a047f000001456a047f000001456a047f000001456a047f000001456a047f000001456a047f000001456a047f000001456a047f000001456a047f000001456a0000000000000000")
        pkt_query = bytes.fromhex("8400000040001900000060e41f80")
        
        # Pre-calculate server address bytes for OCR2 (IPv4)
        try:
            ip_bytes = socket.inet_aton(socket.gethostbyname(host))
            port_bytes = port.to_bytes(2, 'big')
            server_addr_bytes = b'\x04' + ip_bytes + port_bytes
        except:
            server_addr_bytes = b'\x04\x7f\x00\x00\x01' + port.to_bytes(2, 'big')
        
        while self.should_run:
            try:
                # Rate limit sending to avoid flooding
                if time.time() - last_send_time > 1.0:
                    pkt = None
                    
                    if pkt_mode == 0: pkt = make_ping(b'\x01')
                    elif pkt_mode == 1: pkt = make_ping(b'\x02')
                    elif pkt_mode == 2: pkt = make_ocr1()
                    elif pkt_mode == 3: pkt = make_ocr2(server_addr_bytes, server_mtu)
                    
                    elif pkt_mode == 4: # Send Connect (0x09)
                        self.tx_rel_seq = (self.tx_rel_seq + 1) & 0xFFFFFF
                        pkt = self.patch_raknet_packet(pkt_connect, self.tx_rel_seq)
                        self.log("Sending Connect (0x09)...")
                        
                    elif pkt_mode == 5: # Send Login (0x13)
                        self.tx_rel_seq = (self.tx_rel_seq + 1) & 0xFFFFFF
                        pkt = self.patch_raknet_packet(pkt_login, self.tx_rel_seq)
                        self.log("Sending Login (0x13)...")
                        pkt_mode = 6 # Advance to query immediately
                        
                    elif pkt_mode == 6: # Send Query (0x60)
                        self.tx_rel_seq = (self.tx_rel_seq + 1) & 0xFFFFFF
                        base = self.raknet_query if self.raknet_query else pkt_query
                        pkt = self.patch_raknet_packet(base, self.tx_rel_seq)
                        self.log("Sending Query (0x60)...")
                        pkt_mode = 7 # Wait/Idle
                        
                    elif pkt_mode == 7: # Idle / Refresh
                        if time.time() - last_send_time > 15.0:
                            pkt_mode = 6 # Re-query
                            continue
                        else:
                            pkt = make_ping(b'\x01') # Keep-alive

                    if pkt:
                        # Patch Packet Sequence Number (Bytes 1-3 Little Endian)
                        if 0x80 <= pkt[0] <= 0x8F:
                            seq_bytes = self.tx_seq.to_bytes(3, 'little')
                            pkt = pkt[0:1] + seq_bytes + pkt[4:]
                            self.tx_seq = (self.tx_seq + 1) & 0xFFFFFF
                        
                        sock.sendto(pkt, (host, port))
                        
                    last_send_time = time.time()
                
                try:
                    data, addr = sock.recvfrom(4096)
                    if data:
                        pid = data[0]
                        
                        if pid == 0x1C: # Unconnected Pong
                            if len(data) > 33:
                                payload = data[33:]
                                server_info = payload.decode('utf-8', errors='ignore')
                                self.log(f"RakNet Pong from {addr}: {server_info}")
                            else:
                                self.log(f"RakNet Pong (No Data) from {addr}")
                                if pkt_mode < 2:
                                    pkt_mode += 1
                                    self.log(f"Switching to mode {pkt_mode}...")
                            
                            self.register_direct_lobby(addr, "Ping OK")
                            
                        elif pid == 0x06: # Open Connection Reply 1
                            # ID(1) + Magic(16) + ServerGUID(8) + Sec(1) + MTU(2)
                            if len(data) >= 28:
                                server_mtu = data[26:28]
                                if pkt_mode < 3:
                                    pkt_mode = 3
                                    self.log(f"RX Reply 1. MTU: {int.from_bytes(server_mtu, 'big')}. Sending OCR2...")
                            
                            self.register_direct_lobby(addr, "Handshake (1/2)")

                        elif pid == 0x08: # Open Connection Reply 2
                            self.log(f"RX Open Connection Reply 2 from {addr}. Connection Established!")
                            self.register_direct_lobby(addr, "Connected")
                            if pkt_mode < 4:
                                pkt_mode = 4
                                self.log("Handshake Complete. Switching to Connected Mode (4).")
                            
                        elif 0x80 <= pid <= 0x8F: # RakNet Frame Set (Data)
                            # 1. Extract Sequence Number (Bytes 1-3 Little Endian)
                            seq_bytes = data[1:4]
                            seq_num = int.from_bytes(seq_bytes, 'little')
                            
                            # 2. Send ACK (0xC0)
                            # Structure: ID(C0) + Count(00 01) + Equal(01) + Seq(3 bytes)
                            ack_pkt = b'\xC0\x00\x01\x01' + seq_bytes
                            sock.sendto(ack_pkt, (host, port))
                            
                            self.log(f"RX FrameSet {len(data)}b (ID: {hex(pid)}) Seq: {seq_num} -> Sent ACK")
                            
                            frames = self.parse_raknet_frames(data)
                            for i, frame in enumerate(frames):
                                if not frame: continue
                                msg_id = frame[0]
                                # self.log(f"  Msg {i}: ID {hex(msg_id)} ({len(frame)}b)")
                                
                                if msg_id == 0x10:
                                    self.log("  -> Connection Request Accepted")
                                    if pkt_mode == 4:
                                        pkt_mode = 5 # Proceed to Login
                                elif msg_id == 0x61:
                                    self.log("  -> Game List Response (0x61)")
                                    payload = frame[1:]
                                    if len(payload) >= 4:
                                        count = int.from_bytes(payload[:4], 'little')
                                        self.log(f"  -> Lobby Count: {count}")
                                        if len(payload) > 4:
                                            self.log(f"  -> Raw Data: {payload[4:].hex()}")
                                    else:
                                        self.log(f"  -> Data ({len(payload)}b): {payload.hex()}")
                        
                        else:
                            self.log(f"RX {len(data)}b from {addr} (ID: {hex(pid)})")
                except socket.timeout:
                    pass
                
                time.sleep(5)
            except Exception as e:
                self.log(f"RakNet Error: {e}")
                break
        
        sock.close()
        self.connected = False
        self.root.after(0, lambda: self.status_var.set("Disconnected"))
        self.root.after(0, lambda: self.connect_btn.config(text="Connect"))
        self.log("RakNet Monitor Stopped.")
        
        if self.should_run and self.config.get("auto_reconnect", False):
            delay = self.config.get("reconnect_delay", 10)
            self.log(f"Auto-reconnecting in {delay}s...")
            self.root.after(delay * 1000, self.connect)

    def run_bzcc_http(self, url):
        self.connected = True
        
        while self.should_run:
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'BZLobbyMonitor/1.0'})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read().decode('utf-8'))
                    self.process_bzcc_data(data)
            except Exception as e:
                self.log(f"HTTP Error: {e}")
            
            for _ in range(15): # Poll every 15s
                if not self.should_run: break
                time.sleep(1)
        
        self.connected = False
        self.root.after(0, lambda: self.status_var.set("Disconnected"))
        self.root.after(0, lambda: self.connect_btn.config(text="Connect"))
        self.log("BZCC Monitor Stopped.")

    def process_bzcc_data(self, data):
        # Map BZCC JSON (Model.cs) to internal lobby structure
        games = data.get("GET", [])
        new_lobbies = {}
        
        for g in games:
            lid = g.get("g") # GUID
            if not lid: continue
            
            # Decode Name (Base64)
            raw_name = g.get("n") or ""
            try:
                name = base64.b64decode(raw_name).decode('cp1252', errors='ignore').rstrip('\x00')
            except:
                name = raw_name
                
            map_name = g.get("m", "Unknown")
            
            users = {}
            players = g.get("pl") or []
            for p in players:
                pid = p.get("i", "Unknown")
                p_raw = p.get("n") or ""
                try:
                    p_name = base64.b64decode(p_raw).decode('cp1252', errors='ignore').rstrip('\x00')
                except:
                    p_name = p_raw
                users[pid] = {"name": p_name, "id": pid, "team": p.get("t"), "score": p.get("s")}
            
            lobby = {
                "id": lid,
                "metadata": {
                    "name": name, "gameType": "BZCC", "map": map_name, 
                    "gameSettings": f"*{map_name}*", "ready": f"*{map_name}*"
                },
                "users": users,
                "memberLimit": g.get("pm", 0),
                "isLocked": str(g.get("l")) == "1",
                "isPrivate": str(g.get("k")) == "1",
                "owner": users[next(iter(users))]["id"] if users else "Unknown"
            }
            new_lobbies[str(lid)] = lobby
            
        self.lobbies = new_lobbies
        self.root.after(0, self.refresh_tree)

    def run_ws(self, url):
        # websocket.enableTrace(True)
        
        proxy_opts = {}
        if self.config.get("proxy_enabled", False):
            host = self.config.get("proxy_host", "").strip()
            port = self.config.get("proxy_port", "").strip()
            ptype = self.config.get("proxy_type", "http")
            if host and port:
                proxy_opts["http_proxy_host"] = host
                proxy_opts["http_proxy_port"] = port
                proxy_opts["proxy_type"] = ptype
                self.log(f"Using Proxy: {host}:{port} ({ptype})")
        
        self.ws = websocket.WebSocketApp(url,
                                  on_open=self.on_open,
                                  on_message=self.on_message,
                                  on_error=self.on_error,
                                  on_close=self.on_close)
        
        self.ws.run_forever(**proxy_opts)

    def on_open(self, ws):
        self.connected = True
        self.root.after(0, lambda: self.status_var.set("Connected"))
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
                "clientVersion": "2.2.301"
            }
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
                success = content.get('success')
                self.my_id = content.get('id')
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

    def on_error(self, ws, error):
        self.log(f"WebSocket Error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        self.connected = False
        self.root.after(0, lambda: self.status_var.set("Disconnected"))
        self.root.after(0, lambda: self.connect_btn.config(text="Connect"))
        self.log("WebSocket Closed.")
        if self.should_run:
            self.trigger_alert("disconnect")
            
            if self.config.get("auto_reconnect", False):
                delay = self.config.get("reconnect_delay", 10)
                self.log(f"Auto-reconnecting in {delay}s...")
                self.root.after(delay * 1000, self.connect)
        
    def set_player_data(self):
        name = self.name_var.get()
        
        if self.config.get("auto_claim_enabled", False):
            bot_name = self.config.get("auto_claim_bot_name", "")
            if bot_name:
                name = bot_name
        
        if not name: name = f"User_{self.my_id}"
        
        # Send player data updates similar to ConnectionManager.js
        updates = [
            {"key": "name", "value": name},
            {"key": "playerName", "value": name},
            {"key": "clientVersion", "value": "2.2.301"},
            {"key": "authType", "value": "web"}
        ]
        for u in updates:
            self.ws.send(json.dumps({"type": "SetPlayerData", "content": u}))

    def handle_lobby_list(self, data):
        # Handle different data structures (lobbies object directly or inside data)
        if "lobbies" in data:
            new_lobbies = data.get("lobbies", {})
        else:
            new_lobbies = data
        self.lobbies = new_lobbies
        self.log(f"Received Full Lobby List: {len(self.lobbies)} lobbies.")
        self.root.after(0, self.refresh_tree)
        self.root.after(0, self.check_and_update_current_lobby)
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
            if self.config.get("alert_griefer", False):
                self.check_griefer_join(str(lid), lobby)
                
            if str(lid) not in self.lobbies:
                self.trigger_alert("new_lobby")
            self.lobbies[str(lid)] = lobby
            self.check_auto_ban_lobby(str(lid))
        self.log(f"Lobbies Updated: {list(changed_lobbies.keys())}")
        self.root.after(0, self.refresh_tree)
        self.root.after(0, self.check_and_update_current_lobby)

    def check_griefer_join(self, lid, new_lobby_data):
        griefer_id = "S76561198297657246"
        new_users = new_lobby_data.get("users", {})
        
        if griefer_id in new_users:
            # Check if already present in old data
            old_lobby = self.lobbies.get(lid)
            is_new = True
            if old_lobby:
                old_users = old_lobby.get("users", {})
                if griefer_id in old_users:
                    is_new = False
            
            if is_new:
                name = new_users[griefer_id].get("name", "Unknown")
                self.log(f"WARNING: Griefer {name} detected in lobby {lid}")
                self.trigger_alert("griefer_join", name)

    def handle_lobby_removed(self, data):
        lid = str(data.get("id"))
        if lid in self.lobbies:
            del self.lobbies[lid]
            self.root.after(0, self.refresh_tree)
            self.log(f"Lobby Removed: {lid}")
            self.root.after(2000, self.check_auto_claim)
        if self.current_lobby_id is not None and self.current_lobby_id == int(lid):
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
        author = chat.get('author')
        if not author:
            author = chat.get('speakerId', 'Unknown')
        
        speaker_id = chat.get('speakerId')
        if speaker_id and str(speaker_id) in self.muted_users:
            return
        
        text = chat.get('text', '')
        self.log(f"[CHAT] {author}: {text}")
        self.log_chat(author, text)
        
        # Relay to Discord
        if self.config.get("discord_enabled", False) and self.config.get("discord_relay_to_discord", True):
            target_lobby = self.discord_lobby_id_var.get()
            if str(self.current_lobby_id) == str(target_lobby):
                self.send_to_discord(message=f"**{author}**: {text}")

    def handle_member_list_changed(self, data):
        member = data.get("member")
        lid = data.get("lobbyId")
        uid = data.get("id", member)
        action = "left" if data.get("removed") else "joined"
        self.log(f"User {member} {action} lobby {lid}")
        if not data.get("removed"):
            self.trigger_alert("player_join", member)
            self.check_auto_ban_join(lid, uid, member)
            
            # Bot Welcome
            if self.config.get("bot_enabled", False) and self.connected:
                if self.current_lobby_id == int(lid):
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
        if not lobby: return
        
        owner = str(lobby.get("owner", ""))
        if owner != str(self.my_id): return # We only ban in our lobbies
        
        ban_list = self.config.get("ban_list", "").lower().splitlines()
        ban_list = [b.strip() for b in ban_list if b.strip()]
        
        if str(uid).lower() in ban_list or name.lower() in ban_list:
            self._auto_kick(uid, name, "Auto-Ban (ID/Name Match)")

    def check_auto_ban_lobby(self, lid):
        # Deep check on lobby update (Includes IPs)
        lobby = self.lobbies.get(str(lid))
        if not lobby: return
        
        owner = str(lobby.get("owner", ""))
        if owner != str(self.my_id): return
        
        ban_list = self.config.get("ban_list", "").lower().splitlines()
        ban_list = [b.strip() for b in ban_list if b.strip()]
        if not ban_list: return

        users = lobby.get("users", {})
        for uid, u_data in users.items():
            if str(uid) == str(self.my_id): continue
            
            name = u_data.get("name", "").lower()
            ip = u_data.get("ipAddress", "").lower()
            
            if (str(uid).lower() in ban_list or 
                name in ban_list or 
                (ip and ip != "unknown" and ip in ban_list)):
                self._auto_kick(uid, u_data.get("name", "Unknown"), "Auto-Ban (IP/ID Match)")

    def _auto_kick(self, uid, name, reason):
        self.log(f"!!! KICKING {name} (ID: {uid}) - Reason: {reason} !!!")
        self.ws.send(json.dumps({"type": "DoKickUser", "content": int(uid) if str(uid).isdigit() else uid}))

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
            {"key": "name", "value": full_name}
        ]
        for m in meta_updates:
            self.ws.send(json.dumps({"type": "SetLobbyData", "content": m}))

    def check_and_update_current_lobby(self):
        if not self.my_id:
            self.update_current_lobby(None)
            return

        found_lobby_id = None
        for lid, lobby in self.lobbies.items():
            if self.my_id in lobby.get("users", {}):
                found_lobby_id = int(lid)
                break
        self.update_current_lobby(found_lobby_id)

    def update_current_lobby(self, lobby_id):
        self.current_lobby_id = lobby_id
        if lobby_id is not None:
            lobby_name = self.lobbies.get(str(lobby_id), {}).get("metadata", {}).get("name", f"ID: {lobby_id}")
            self.root.after(0, lambda: self.current_lobby_var.set(f"In Lobby: {lobby_name}"))
            self.root.after(0, lambda: self.leave_btn.config(text="Leave Lobby"))
            self.update_rpc(f"In Lobby: {lobby_name}", "Playing Battlezone 98 Redux")
        else:
            self.root.after(0, lambda: self.current_lobby_var.set("In Lounge"))
            self.root.after(0, lambda: self.leave_btn.config(text="Refresh Lounge"))
            self.update_rpc("In Lounge", "Browsing Lobbies")

    def refresh_tree(self):
        # Save selection
        selected_items = self.tree.selection()
        selected_id = None
        if selected_items:
            selected_id = self.tree.item(selected_items[0])['values'][0]

        # Clear
        for item in self.tree.get_children():
            self.tree.delete(item)
            
        # Repopulate
        friends = self.config.get("friend_list", "").lower().splitlines()
        for lid, lobby in self.lobbies.items():
            meta = lobby.get("metadata", {})
            raw_name = meta.get("name", "Unknown")
            
            # Clean up name display (remove ~chat~pub~~ prefix)
            if "~~" in raw_name:
                name = raw_name.split("~~")[-1]
            else:
                name = raw_name
                
            owner = lobby.get("owner", "Unknown")
            if owner == -1: owner = "none"
            
            # Calculate player count
            users = lobby.get("users", {})
            
            # Check for friends
            has_friend = False
            for uid, u_data in users.items():
                u_name = u_data.get('name', '').lower()
                if any(f.strip() in u_name or f.strip() in str(uid).lower() for f in friends if f.strip()):
                    has_friend = True
                    break
            
            if self.filter_locked_var.get() and lobby.get("isLocked"): continue
            if self.filter_full_var.get() and len(users) >= lobby.get('memberLimit', 0): continue
            
            player_count = f"{len(users)}/{lobby.get('memberLimit', '?')}"
            
            game_type = meta.get("gameType", "?")
            version = lobby.get("clientVersion", "?")
            locked = "Yes" if lobby.get("isLocked") else "No"
            is_private = "Yes" if lobby.get("isPrivate") else "No"
            
            # Parse Map Name
            map_name = "?"
            game_settings = meta.get("gameSettings", "")
            ready = meta.get("ready", "")
            
            if ready:
                parts = ready.split('*')
                if len(parts) >= 2: map_name = parts[1]
            elif game_settings:
                parts = game_settings.split('*')
                if len(parts) >= 2: map_name = parts[1]
            if map_name == "unknown": map_name = "?"
            
            tags = ("friend",) if has_friend else ()
            self.tree.insert("", "end", values=(lid, name, map_name, owner, player_count, game_type, version, locked, is_private), tags=tags)

        # Restore selection if possible
        if selected_id:
            for item in self.tree.get_children():
                if str(self.tree.item(item)['values'][0]) == str(selected_id):
                    self.tree.selection_set(item)
                    break

    def on_lobby_select(self, event):
        selected_items = self.tree.selection()
        if not selected_items:
            return
            
        lid = str(self.tree.item(selected_items[0])['values'][0])
        lobby = self.lobbies.get(lid)
        
        if lobby:
            self.update_lobby_details(lobby)
            self.update_player_details(lobby)

    def update_lobby_details(self, lobby):
        self.lobby_details_text.config(state="normal")
        self.lobby_details_text.delete("1.0", "end")
        
        # Reset preview
        self.preview_label.config(image="", text="No Preview")
        
        # Check for cached image to display in label
        l_meta = lobby.get('metadata', {})
        game_settings = l_meta.get('gameSettings')
        mod_id = None
        if game_settings:
             parts = game_settings.split('*')
             if len(parts) > 3 and parts[3] not in ['0', '']:
                 mod_id = parts[3]
        
        if mod_id and mod_id in self.image_cache:
            self.preview_label.config(image=self.image_cache[mod_id], text="")
        elif mod_id:
            self.preview_label.config(text="Loading Preview...")
            if mod_id not in self.pending_fetches:
                self.fetch_image(mod_id, is_mod=True)
        
        self.lobby_details_text.insert("end", f"Lobby ID: {lobby.get('id')}\n")
        
        self.lobby_details_text.insert("end", f"Name: {l_meta.get('name')}\n")
        self.lobby_details_text.insert("end", f"Created: {lobby.get('createdTime')}\n")
        
        # Parse Game Settings from Lobby Metadata
        if game_settings:
             parts = game_settings.split('*')
             if len(parts) > 1:
                 self.lobby_details_text.insert("end", f"Map: {parts[1]}\n")
             if len(parts) > 3 and parts[3] not in ['0', '']:
                 mod_id = parts[3]
                         
                 self.lobby_details_text.insert("end", f"Mod ID: {mod_id} (")
                 self.insert_link(self.lobby_details_text, "Workshop", f"https://steamcommunity.com/sharedfiles/filedetails/?id={mod_id}")
                 self.lobby_details_text.insert("end", ")\n")
        else:
             self.lobby_details_text.insert("end", f"Game Settings: {game_settings}\n")

        if str(l_meta.get("launched")) == "1":
             self.lobby_details_text.insert("end", f"Status: Launched\n")
             
        self.lobby_details_text.config(state="disabled")

    def update_player_details(self, lobby):
        self.player_details_text.config(state="normal")
        self.player_details_text.delete("1.0", "end")
        
        users = lobby.get("users", {})
        friends = self.config.get("friend_list", "").lower().splitlines()
        for uid, user in users.items():
            user_name = user.get('name', 'Unknown')
            user_meta = user.get('metadata', {})
            
            # Fallback to metadata if root name is unknown
            if user_name == 'unknown' or not user_name:
                user_name = user_meta.get('name', 'Unknown')
                
            self.player_details_text.insert("end", f" - {user_name} (ID: {uid})\n")
            is_friend = any(f.strip() in user_name.lower() or f.strip() in str(uid).lower() for f in friends if f.strip())
            
            self.player_details_text.insert("end", f" - {user_name} (ID: {uid})", "friend" if is_friend else "")
            if is_friend:
                self.player_details_text.insert("end", " [FRIEND]", "friend")
            self.player_details_text.insert("end", "\n")
            self.player_details_text.insert("end", f"   IP: {user.get('ipAddress')}\n")
            self.player_details_text.insert("end", f"   Auth: {user.get('authType')}\n")
            
            # Geo Lookup
            ip = user.get('ipAddress')
            if ip and ip != "unknown":
                geo = self.get_geo_info(ip)
                if geo:
                    self.player_details_text.insert("end", f"   Loc: {geo}\n")
            
            if uid.startswith('S'):
                steam_id = uid[1:]
                
                if HAS_PIL:
                    if steam_id in self.image_cache:
                        self.player_details_text.image_create("end", image=self.image_cache[steam_id])
                        self.player_details_text.insert("end", " ")
                    elif steam_id not in self.pending_fetches:
                        self.fetch_image(steam_id, is_mod=False)
                
                self.player_details_text.insert("end", "   Profile: ")
                self.insert_link(self.player_details_text, f"{steam_id}", f"https://steamcommunity.com/profiles/{steam_id}")
                
                if steam_id == "76561198297657246":
                    self.player_details_text.insert("end", " [KNOWN GRIEFER]", "griefer")
                    
                self.player_details_text.insert("end", "\n")
            
            # Extended User Info
            if user_meta:
                if 'team' in user_meta:
                    self.player_details_text.insert("end", f"   Team: {user_meta['team']}\n")
                if 'vehicle' in user_meta:
                    self.player_details_text.insert("end", f"   Vehicle: {user_meta['vehicle']}\n")
                
                # Parse Ready String for Map info (often on host)
                ready = user_meta.get('ready')
                if ready:
                    r_parts = ready.split('*')
                    if len(r_parts) > 1:
                        self.player_details_text.insert("end", f"   Ready Map: {r_parts[1]}\n")
                if user_meta.get('launched') == "1":
                    self.player_details_text.insert("end", f"   Status: Launched\n")
            
            # Network Info
            wan = user.get('wanAddress')
            if wan and wan != 'unknown':
                self.player_details_text.insert("end", f"   WAN: {wan}\n")
            
            lans = user.get('lanAddresses')
            if lans:
                if isinstance(lans, list):
                    lan_str = ", ".join(lans)
                else:
                    lan_str = str(lans)
                if lan_str:
                    self.player_details_text.insert("end", f"   LAN: {lan_str}\n")
            
            self.player_details_text.insert("end", "-"*30 + "\n")
            
        self.player_details_text.config(state="disabled")

    def fetch_image(self, target_id, is_mod):
        self.pending_fetches.add(target_id)
        threading.Thread(target=self._fetch_image_worker, args=(target_id, is_mod), daemon=True).start()

    def _fetch_image_worker(self, target_id, is_mod):
        try:
            image_url = None
            if is_mod:
                url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={target_id}"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req) as r:
                    html = r.read().decode('utf-8')
                    # Try to find preview image
                    thumb = re.search(r'id="ActualImage"\s+src="([^"]+)"', html)
                    if not thumb: 
                        thumb = re.search(r'<link rel="image_src" href="([^"]+)">', html)
                    if thumb:
                        image_url = thumb.group(1)
            else:
                # Fetch Steam Profile XML
                url = f"https://steamcommunity.com/profiles/{target_id}?xml=1"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req) as r:
                    xml = r.read().decode('utf-8')
                    # Simple regex for avatarMedium
                    avatar = re.search(r'<avatarMedium><!\[CDATA\[(.*?)\]\]></avatarMedium>', xml)
                    if avatar:
                        image_url = avatar.group(1)

            if image_url:
                with urllib.request.urlopen(image_url) as r:
                    data = r.read()
                    self.root.after(0, lambda: self._cache_image(target_id, data, is_mod))
            else:
                self.pending_fetches.discard(target_id)

        except Exception as e:
            print(f"Image fetch failed for {target_id}: {e}")
            self.pending_fetches.discard(target_id)

    def _cache_image(self, target_id, data, is_mod):
        try:
            img = Image.open(BytesIO(data))
            img.thumbnail((50, 50), Image.Resampling.LANCZOS)
            self.image_cache[target_id] = ImageTk.PhotoImage(img)
            if is_mod: # Larger preview for lobby
                img_large = Image.open(BytesIO(data))
                img_large.thumbnail((280, 160), Image.Resampling.LANCZOS)
                self.image_cache[target_id] = ImageTk.PhotoImage(img_large)
            # Refresh current view if applicable
            self.on_lobby_select(None)
        except Exception as e:
            print(f"Error caching image: {e}")
        finally:
            self.pending_fetches.discard(target_id)

    # --- Proxy Tools ---
    def set_tor_proxy(self):
        # Check for pysocks
        try:
            import socks
        except ImportError:
            messagebox.showerror("Missing Dependency", "To use Tor (SOCKS5), you must install 'pysocks'.\nRun: pip install pysocks")
            return

        if sys.platform == 'win32':
            self.manage_tor_windows()
        else:
            # Linux/Mac: Assume system Tor
            self.proxy_host_var.set("127.0.0.1")
            self.proxy_port_var.set("9050")
            self.proxy_enabled_var.set(True)
            self.config["proxy_type"] = "socks5"
            self.save_ui_config()
            messagebox.showinfo("Tor Proxy", "Proxy set to 127.0.0.1:9050 (SOCKS5).\nEnsure the 'tor' service is running on your system.")

    def manage_tor_windows(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        bin_dir = os.path.join(base_dir, "bin", "tor")
        tor_exe = os.path.join(bin_dir, "tor.exe")
        
        if not os.path.exists(tor_exe):
            if messagebox.askyesno("Tor Not Found", "Tor Expert Bundle is missing.\nDownload and configure it automatically?"):
                self.download_tor(bin_dir)
            else:
                return

        if os.path.exists(tor_exe):
            self.start_tor(tor_exe, bin_dir)
            
        self.proxy_host_var.set("127.0.0.1")
        self.proxy_port_var.set("9050")
        self.proxy_enabled_var.set(True)
        self.config["proxy_type"] = "socks5"
        self.save_ui_config()
        self.log("Tor Proxy Configured (127.0.0.1:9050)")

    def download_tor(self, target_dir):
        self.log("Downloading Tor Expert Bundle...")
        # URL for Tor Expert Bundle Windows x86_64 (Stable)
        url = "https://archive.torproject.org/tor-package-archive/torbrowser/15.0.5/tor-expert-bundle-windows-x86_64-15.0.5.tar.gz"
        self.log(f"URL: {url}")
        
        try:
            if not os.path.exists(target_dir):
                os.makedirs(target_dir)
                
            tar_path = os.path.join(target_dir, "tor.tar.gz")
            urllib.request.urlretrieve(url, tar_path)
            
            self.log("Extracting Tor...")
            with tarfile.open(tar_path, "r:gz") as tar:
                # Flatten structure: extract 'tor/tor.exe' directly to bin/tor/
                for member in tar.getmembers():
                    if "tor.exe" in member.name or "dll" in member.name:
                        member.name = os.path.basename(member.name) # Strip paths
                        tar.extract(member, target_dir)
            
            os.remove(tar_path)
            
            # Create default torrc
            torrc_path = os.path.join(target_dir, "torrc")
            data_dir = os.path.join(target_dir, "data")
            if not os.path.exists(data_dir): os.makedirs(data_dir)
            
            with open(torrc_path, "w") as f:
                f.write(f"SocksPort 9050\nDataDirectory {os.path.abspath(data_dir)}\n")
                
            self.log("Tor installed successfully.")
        except Exception as e:
            self.log(f"Tor Download Error: {e}")
            messagebox.showerror("Error", f"Failed to download Tor:\n{e}")

    def start_tor(self, exe_path, cwd):
        if self.tor_process:
            return # Already running
            
        self.log("Starting Tor process...")
        try:
            # Hide console window
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            self.tor_process = subprocess.Popen(
                [exe_path, "-f", "torrc"], 
                cwd=cwd,
                startupinfo=startupinfo,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
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
        self.log("Searching for free proxies...")
        threading.Thread(target=self._find_proxy_worker, daemon=True).start()

    def _find_proxy_worker(self):
        try:
            # Fetch from a public list (HTTP proxies)
            url = "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req) as r:
                data = r.read().decode('utf-8')
                proxies = [line.strip() for line in data.split('\n') if line.strip()]
            
            if not proxies:
                self.log("No proxies found in list.")
                return

            # Test random proxies until one works
            random.shuffle(proxies)
            for proxy in proxies[:10]: # Try up to 10
                host, port = proxy.split(':')
                self.log(f"Testing proxy {host}:{port}...")
                if self._test_proxy_connection(host, port, ptype="http"):
                    self.root.after(0, lambda h=host, p=port: self._set_proxy_ui(h, p))
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
                except:
                    return False
            else:
                proxy_handler = urllib.request.ProxyHandler({'http': f"{host}:{port}", 'https': f"{host}:{port}"})
                opener = urllib.request.build_opener(proxy_handler)
                opener.open("http://www.google.com", timeout=5)
                return True
        except:
            return False

    def test_proxy(self):
        host = self.proxy_host_var.get()
        port = self.proxy_port_var.get()
        ptype = self.config.get("proxy_type", "http")
        if not host or not port: return
        
        def run_test():
            self.root.after(0, lambda: self._set_proxy_indicator(None))
            success = False
            try:
                if ptype == "socks5":
                    import socks
                    s = socks.socksocket()
                    s.set_proxy(socks.SOCKS5, host, int(port))
                    s.settimeout(10)
                    s.connect(("api.ipify.org", 80))
                    s.sendall(b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n")
                    response = b""
                    while True:
                        data = s.recv(4096)
                        if not data: break
                        response += data
                    s.close()
                    body = response.split(b"\r\n\r\n")[1].decode('utf-8')
                    self.log(f"Proxy Test (SOCKS5): SUCCESS. IP: {body}")
                    success = True
                    self.root.after(0, lambda: messagebox.showinfo("Proxy Verified", f"SOCKS5 Proxy is working.\nExternal IP: {body}"))
                elif self._test_proxy_connection(host, port):
                    # For HTTP, try to get IP as well
                    req = urllib.request.Request("http://api.ipify.org")
                    req.set_proxy(f"{host}:{port}", "http")
                    with urllib.request.urlopen(req, timeout=10) as r:
                        ip = r.read().decode('utf-8')
                        self.log(f"Proxy Test (HTTP): SUCCESS. IP: {ip}")
                        self.root.after(0, lambda: messagebox.showinfo("Proxy Verified", f"HTTP Proxy is working.\nExternal IP: {ip}"))
                    success = True
            except Exception as e:
                self.log(f"Proxy Test Failed: {e}")

            if success:
                self.root.after(0, lambda: self._set_proxy_indicator(True))
            else:
                self.root.after(0, lambda: self._set_proxy_indicator(False))
        threading.Thread(target=run_test, daemon=True).start()

    def _set_proxy_ui(self, host, port):
        self.proxy_host_var.set(host)
        self.proxy_port_var.set(port)
        self.proxy_enabled_var.set(True)
        self.config["proxy_type"] = "http" # Public lists are usually HTTP
        self.save_ui_config()
        self._set_proxy_indicator(True)

    def start_proxy_monitor(self):
        threading.Thread(target=self._proxy_monitor_loop, daemon=True).start()

    def _proxy_monitor_loop(self):
        while self.should_run:
            if self.config.get("proxy_enabled", False):
                host = self.config.get("proxy_host", "")
                port = self.config.get("proxy_port", "")
                if host and port:
                    res = self._test_proxy_connection(host, port)
                    self.root.after(0, lambda r=res: self._set_proxy_indicator(r))
                else:
                    self.root.after(0, lambda: self._set_proxy_indicator(None))
            else:
                self.root.after(0, lambda: self._set_proxy_indicator(None))
            
            if self.tor_process:
                if self.tor_process.poll() is not None:
                    self.tor_process = None
                    self.log("Tor process terminated unexpectedly.")
                    self.root.after(0, lambda: self.tor_status_var.set("Tor: Stopped"))
                    self.root.after(0, lambda: self.tor_status_label.config(fg="#ff0000"))
            
            for _ in range(30):
                if not self.should_run: return
                time.sleep(1)

    def _set_proxy_indicator(self, status):
        if not hasattr(self, 'proxy_status_light'): return
        color = "gray"
        if status is True: color = "#00ff00"
        elif status is False: color = "#ff0000"
        self.proxy_status_canvas.itemconfig(self.proxy_status_light, fill=color)

    # --- Logging Tools ---
    def cleanup_logs(self):
        retention = self.config.get("log_retention", 7)
        # Implementation left simple: user can manually delete for now or expand later
        pass

    def toggle_stats_logging(self):
        self.save_ui_config()
        if self.stats_enabled_var.get():
            self.start_stats_logger()
        self.draw_stats()

    def start_stats_logger(self):
        threading.Thread(target=self._stats_logger_loop, daemon=True).start()

    def _stats_logger_loop(self):
        while self.should_run and self.config.get("stats_enabled", False):
            try:
                if self.lobbies:
                    filename = "bzr_stats.csv"
                    file_exists = os.path.isfile(filename)
                    
                    with open(filename, "a", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f)
                        if not file_exists:
                            writer.writerow(["Timestamp", "LobbyID", "Name", "Map", "Players", "MaxPlayers", "Type"])
                        
                        timestamp = datetime.now().isoformat()
                        for lid, lobby in self.lobbies.items():
                            meta = lobby.get("metadata", {})
                            users = lobby.get("users", {})
                            
                            # Parse map
                            map_name = "?"
                            if "ready" in meta: map_name = meta["ready"].split('*')[1] if '*' in meta["ready"] else "?"
                            
                            writer.writerow([
                                timestamp,
                                lid,
                                meta.get("name", "Unknown"),
                                map_name,
                                len(users),
                                lobby.get("memberLimit", 0),
                                meta.get("gameType", "?")
                            ])
            except Exception as e:
                print(f"Stats log error: {e}")
            
            # Log every 60 seconds
            for _ in range(60):
                if not self.should_run or not self.config.get("stats_enabled", False): return
                time.sleep(1)

    # --- Discord Integration ---
    def toggle_discord_relay(self):
        self.save_ui_config()
        if self.discord_enabled_var.get():
            if not self.discord_thread or not self.discord_thread.is_alive():
                self.discord_thread = threading.Thread(target=self.discord_polling_loop, daemon=True)
                self.discord_thread.start()
                self.log("Discord Relay Started.")
        
    def test_discord_connection(self):
        token = self.discord_token_var.get()
        if not token: return
        
        try:
            req = urllib.request.Request("https://discord.com/api/v10/users/@me")
            req.add_header("Authorization", f"Bot {token}")
            req.add_header("User-Agent", "BZLobbyMonitor/1.0")
            
            with urllib.request.urlopen(req) as r:
                data = json.loads(r.read().decode('utf-8'))
                self.discord_bot_id = data.get("id")
                username = data.get("username")
                messagebox.showinfo("Success", f"Connected as {username} (ID: {self.discord_bot_id})")
                self.log(f"Discord Bot Authenticated: {username}")
        except Exception as e:
            messagebox.showerror("Error", f"Discord Connection Failed:\n{e}")

    def send_to_discord(self, message=None, embed=None):
        token = self.discord_token_var.get()
        chan_id = self.discord_channel_id_var.get()
        if not token or not chan_id: return
        
        def _send():
            try:
                url = f"https://discord.com/api/v10/channels/{chan_id}/messages"
                payload = {}
                if message: payload["content"] = message
                if embed: payload["embeds"] = [embed]
                data = json.dumps(payload).encode('utf-8')
                req = urllib.request.Request(url, data=data, method="POST")
                req.add_header("Authorization", f"Bot {token}")
                req.add_header("Content-Type", "application/json")
                req.add_header("User-Agent", "BZLobbyMonitor/1.0")
                with urllib.request.urlopen(req) as r: pass
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
        if not lobby: return
        
        meta = lobby.get("metadata", {})
        name = meta.get("name", "Unknown Lobby")
        if "~~" in name: name = name.split("~~")[-1]
        
        users = lobby.get("users", {})
        player_count = f"{len(users)}/{lobby.get('memberLimit', '?')}"
        
        map_name = "Unknown"
        if "ready" in meta:
            parts = meta["ready"].split('*')
            if len(parts) >= 2: map_name = parts[1]
        elif "gameSettings" in meta:
            parts = meta["gameSettings"].split('*')
            if len(parts) >= 2: map_name = parts[1]
            
        embed = {
            "title": f"🎮 {name}",
            "color": 0x00ff00,
            "fields": [
                {"name": "Map", "value": map_name, "inline": True},
                {"name": "Players", "value": player_count, "inline": True},
                {"name": "ID", "value": str(self.current_lobby_id), "inline": True}
            ],
            "footer": {"text": f"Battlezone Lobby Monitor • {datetime.now().strftime('%H:%M')}"}
        }
        
        # Add join link if host is steam
        owner_id = str(lobby.get("owner", ""))
        if owner_id.startswith('S'):
            steam_id = owner_id[1:]
            url = f"steam://rungame/301650/{steam_id}/+connect_lobby=B{self.current_lobby_id}"
            embed["description"] = f"[**Click to Join via Steam**]({url})"
            
        self.send_to_discord(embed=embed)
        self.log("Posted lobby status to Discord.")

    def discord_polling_loop(self):
        last_id = None
        
        # Initial fetch to get the latest message ID so we don't spam old messages
        try:
            token = self.discord_token_var.get()
            chan_id = self.discord_channel_id_var.get()
            if token and chan_id:
                url = f"https://discord.com/api/v10/channels/{chan_id}/messages?limit=1"
                req = urllib.request.Request(url)
                req.add_header("Authorization", f"Bot {token}")
                req.add_header("User-Agent", "BZLobbyMonitor/1.0")
                with urllib.request.urlopen(req) as r:
                    msgs = json.loads(r.read().decode('utf-8'))
                    if msgs: last_id = msgs[0].get("id")
        except: pass

        while self.should_run and self.discord_enabled_var.get():
            try:
                token = self.discord_token_var.get()
                chan_id = self.discord_channel_id_var.get()
                if not token or not chan_id:
                    time.sleep(5)
                    continue
                
                url = f"https://discord.com/api/v10/channels/{chan_id}/messages?limit=5"
                if last_id:
                    url += f"&after={last_id}"
                
                req = urllib.request.Request(url)
                req.add_header("Authorization", f"Bot {token}")
                req.add_header("User-Agent", "BZLobbyMonitor/1.0")
                
                with urllib.request.urlopen(req) as r:
                    msgs = json.loads(r.read().decode('utf-8'))
                    
                    # Process from oldest to newest
                    for m in reversed(msgs):
                        msg_id = m.get("id")
                        author = m.get("author", {})
                        author_id = author.get("id")
                        content = m.get("content", "")
                        
                        if last_id and author_id != self.discord_bot_id:
                            if self.discord_to_lobby_var.get() and self.connected:
                                target_lobby = self.discord_lobby_id_var.get()
                                if str(self.current_lobby_id) == str(target_lobby):
                                    sender = author.get("username")
                                    chat_line = f"[Discord] {sender}: {content}"
                                    self.ws.send(json.dumps({"type": "DoSendChat", "content": chat_line}))
                        
                        last_id = msg_id
            except Exception as e: pass
            time.sleep(2)

    # --- Bot & RPC & Stats ---
    def start_bot_loop(self):
        threading.Thread(target=self.bot_loop, daemon=True).start()

    def bot_loop(self):
        while self.should_run:
            if self.config.get("bot_announce_enabled", False) and self.connected and self.current_lobby_id is not None:
                interval = self.config.get("bot_announce_interval", 5) * 60
                if time.time() - self.last_announce_time > interval:
                    msg = self.config.get("bot_announce_msg", "")
                    if msg:
                        self.send_chat_message(msg)
                        self.last_announce_time = time.time()
            time.sleep(10)

    def check_auto_claim(self):
        if not self.config.get("auto_claim_enabled", False): return
        if not self.connected: return
        if self.current_lobby_id is not None: return
        
        if time.time() - self.last_claim_attempt < 10: return

        target_name = self.config.get("auto_claim_name", "")
        if not target_name: return

        found = False
        for lid, lobby in self.lobbies.items():
            meta = lobby.get("metadata", {})
            raw_name = meta.get("name", "")
            clean_name = raw_name.split("~~")[-1] if "~~" in raw_name else raw_name
            if clean_name.lower() == target_name.lower():
                found = True
                break
        
        if not found:
            self.log(f"Auto-Claim: Lobby '{target_name}' missing. Creating...")
            
            # Force name reclaim in case we were using a backup name (e.g. !BRIDGE(1))
            bot_name = self.config.get("auto_claim_bot_name", "")
            if bot_name:
                self.log(f"Reclaiming identity: {bot_name}")
                self.ws.send(json.dumps({"type": "SetPlayerData", "content": {"key": "name", "value": bot_name}}))
                self.ws.send(json.dumps({"type": "SetPlayerData", "content": {"key": "playerName", "value": bot_name}}))
            
            self.create_lobby(target_name)
            self.last_claim_attempt = time.time()

    def get_geo_info(self, ip):
        if ip in self.geo_cache: return self.geo_cache[ip]
        
        def _fetch():
            try:
                with urllib.request.urlopen(f"http://ip-api.com/json/{ip}") as r:
                    data = json.loads(r.read().decode())
                    if data.get("status") == "success":
                        info = f"[{data.get('countryCode')}] {data.get('timezone')}"
                        self.geo_cache[ip] = info
                        # Refresh UI if this player is currently shown
                        self.root.after(0, lambda: self.on_lobby_select(None))
            except: pass
        
        threading.Thread(target=_fetch, daemon=True).start()
        return None

    def init_rpc(self):
        if not HAS_RPC: return
        try:
            client_id = self.config.get("rpc_client_id", "133570000000000000")
            self.rpc = Presence(client_id)
            self.rpc.connect()
            self.log("Discord RPC Connected.")
        except Exception as e:
            self.log(f"RPC Error: {e}")

    def toggle_rpc(self):
        self.save_ui_config()
        if self.rpc_enabled_var.get():
            if not self.rpc: self.init_rpc()
        else:
            if self.rpc: 
                self.rpc.close()
                self.rpc = None

    def update_rpc(self, state, details):
        if self.rpc and self.config.get("rpc_enabled", False):
            try:
                self.rpc.update(state=state, details=details, large_image="bz98_icon", large_text="Battlezone 98 Redux")
            except: pass

    def draw_stats(self):
        self.stats_canvas.delete("all")
        w = self.stats_canvas.winfo_width()
        h = self.stats_canvas.winfo_height()
        
        if not self.config.get("stats_enabled", False):
            self.stats_canvas.create_text(w/2, h/2, text="Statistics logging is DISABLED.\nEnable 'Game Stats Logging' in Configuration.", fill="red", justify="center", font=("Consolas", 12, "bold"))
            return

        if w < 50: return
        
        filename = "bzr_stats.csv"
        if not os.path.exists(filename):
            self.stats_canvas.create_text(w/2, h/2, text="No stats data found.", fill="white")
            return
            
        data_points = {} # timestamp -> total_players
        
        try:
            with open(filename, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                next(reader, None) # Skip header
                for row in reader:
                    if len(row) < 5: continue
                    ts_str = row[0]
                    try:
                        dt = datetime.fromisoformat(ts_str)
                        # Filter last 24h
                        if datetime.now() - dt > timedelta(hours=24): continue
                        
                        players = int(row[4])
                        
                        # Group by minute to reduce noise
                        key = dt.replace(second=0, microsecond=0)
                        if key not in data_points: data_points[key] = 0
                        # Since CSV has one row per lobby, we need to sum them for the same timestamp
                        # But timestamps are exact per snapshot. 
                        # We can assume unique timestamps per snapshot.
                        # Actually, let's just take the max players seen in a 5-min window?
                        # Simpler: Just plot raw points.
                        # Better: Group by snapshot. A snapshot shares the exact ISO string.
                        if ts_str not in data_points: data_points[ts_str] = 0
                        data_points[ts_str] += players
                    except: pass
        except: pass
        
        if not data_points: return
        
        sorted_pts = sorted([(datetime.fromisoformat(k), v) for k, v in data_points.items()], key=lambda x: x[0])
        
        max_p = max([p[1] for p in sorted_pts]) if sorted_pts else 10
        if max_p == 0: max_p = 10
        
        # Draw
        pad = 40
        prev_x, prev_y = None, None
        start_time = sorted_pts[0][0]
        total_seconds = (sorted_pts[-1][0] - start_time).total_seconds()
        if total_seconds == 0: total_seconds = 1
        
        for dt, count in sorted_pts:
            secs = (dt - start_time).total_seconds()
            x = pad + (secs / total_seconds) * (w - 2*pad)
            y = h - pad - (count / max_p) * (h - 2*pad)
            
            if prev_x is not None:
                self.stats_canvas.create_line(prev_x, prev_y, x, y, fill=self.colors["highlight"], width=2)
            prev_x, prev_y = x, y
            
        self.stats_canvas.create_text(pad, h-pad+15, text=start_time.strftime("%H:%M"), fill="gray", anchor="w")
        self.stats_canvas.create_text(w-pad, h-pad+15, text=sorted_pts[-1][0].strftime("%H:%M"), fill="gray", anchor="e")
        self.stats_canvas.create_text(pad-5, pad, text=str(max_p), fill="gray", anchor="e")
        self.stats_canvas.create_text(pad-5, h-pad, text="0", fill="gray", anchor="e")

if __name__ == "__main__":
    root = tk.Tk()
    app = BZLobbyMonitor(root)
    root.mainloop()

    # --- Discord Integration ---
    def toggle_discord_relay(self):
        self.save_ui_config()
        if self.discord_enabled_var.get():
            if not self.discord_thread or not self.discord_thread.is_alive():
                self.discord_thread = threading.Thread(target=self.discord_polling_loop, daemon=True)
                self.discord_thread.start()
                self.log("Discord Relay Started.")
        
    def test_discord_connection(self):
        token = self.discord_token_var.get()
        if not token: return
        
        try:
            req = urllib.request.Request("https://discord.com/api/v10/users/@me")
            req.add_header("Authorization", f"Bot {token}")
            req.add_header("User-Agent", "BZLobbyMonitor/1.0")
            
            with urllib.request.urlopen(req) as r:
                data = json.loads(r.read().decode('utf-8'))
                self.discord_bot_id = data.get("id")
                username = data.get("username")
                messagebox.showinfo("Success", f"Connected as {username} (ID: {self.discord_bot_id})")
                self.log(f"Discord Bot Authenticated: {username}")
        except Exception as e:
            messagebox.showerror("Error", f"Discord Connection Failed:\n{e}")

    def send_to_discord(self, message=None, embed=None):
        token = self.discord_token_var.get()
        chan_id = self.discord_channel_id_var.get()
        if not token or not chan_id: return
        
        def _send():
            try:
                url = f"https://discord.com/api/v10/channels/{chan_id}/messages"
                payload = {}
                if message: payload["content"] = message
                if embed: payload["embeds"] = [embed]
                data = json.dumps(payload).encode('utf-8')
                req = urllib.request.Request(url, data=data, method="POST")
                req.add_header("Authorization", f"Bot {token}")
                req.add_header("Content-Type", "application/json")
                req.add_header("User-Agent", "BZLobbyMonitor/1.0")
                with urllib.request.urlopen(req) as r: pass
            except Exception as e:
                print(f"Discord Send Error: {e}")
        threading.Thread(target=_send, daemon=True).start()

    def post_lobby_status(self):
        if self.current_lobby_id is None:
            messagebox.showinfo("Info", "Not in a lobby.")
            return
            
        lobby = self.lobbies.get(str(self.current_lobby_id))
        if not lobby: return
        
        meta = lobby.get("metadata", {})
        name = meta.get("name", "Unknown Lobby")
        if "~~" in name: name = name.split("~~")[-1]
        
        users = lobby.get("users", {})
        player_count = f"{len(users)}/{lobby.get('memberLimit', '?')}"
        
        map_name = "Unknown"
        if "ready" in meta:
            parts = meta["ready"].split('*')
            if len(parts) >= 2: map_name = parts[1]
        elif "gameSettings" in meta:
            parts = meta["gameSettings"].split('*')
            if len(parts) >= 2: map_name = parts[1]
            
        embed = {
            "title": f"🎮 {name}",
            "color": 0x00ff00,
            "fields": [
                {"name": "Map", "value": map_name, "inline": True},
                {"name": "Players", "value": player_count, "inline": True},
                {"name": "ID", "value": str(self.current_lobby_id), "inline": True}
            ],
            "footer": {"text": f"Battlezone Lobby Monitor • {datetime.now().strftime('%H:%M')}"}
        }
        
        # Add join link if host is steam
        owner_id = str(lobby.get("owner", ""))
        if owner_id.startswith('S'):
            steam_id = owner_id[1:]
            url = f"steam://rungame/301650/{steam_id}/+connect_lobby=B{self.current_lobby_id}"
            embed["description"] = f"[**Click to Join via Steam**]({url})"
            
        self.send_to_discord(embed=embed)
        self.log("Posted lobby status to Discord.")

    def discord_polling_loop(self):
        last_id = None
        
        # Initial fetch to get the latest message ID so we don't spam old messages
        try:
            token = self.discord_token_var.get()
            chan_id = self.discord_channel_id_var.get()
            if token and chan_id:
                url = f"https://discord.com/api/v10/channels/{chan_id}/messages?limit=1"
                req = urllib.request.Request(url)
                req.add_header("Authorization", f"Bot {token}")
                req.add_header("User-Agent", "BZLobbyMonitor/1.0")
                with urllib.request.urlopen(req) as r:
                    msgs = json.loads(r.read().decode('utf-8'))
                    if msgs: last_id = msgs[0].get("id")
        except: pass

        while self.should_run and self.discord_enabled_var.get():
            try:
                token = self.discord_token_var.get()
                chan_id = self.discord_channel_id_var.get()
                if not token or not chan_id:
                    time.sleep(5)
                    continue
                
                url = f"https://discord.com/api/v10/channels/{chan_id}/messages?limit=5"
                if last_id:
                    url += f"&after={last_id}"
                
                req = urllib.request.Request(url)
                req.add_header("Authorization", f"Bot {token}")
                req.add_header("User-Agent", "BZLobbyMonitor/1.0")
                
                with urllib.request.urlopen(req) as r:
                    msgs = json.loads(r.read().decode('utf-8'))
                    
                    # Process from oldest to newest
                    for m in reversed(msgs):
                        msg_id = m.get("id")
                        author = m.get("author", {})
                        author_id = author.get("id")
                        content = m.get("content", "")
                        
                        if last_id and author_id != self.discord_bot_id:
                            if self.discord_to_lobby_var.get() and self.connected:
                                target_lobby = self.discord_lobby_id_var.get()
                                if str(self.current_lobby_id) == str(target_lobby):
                                    sender = author.get("username")
                                    chat_line = f"[Discord] {sender}: {content}"
                                    self.ws.send(json.dumps({"type": "DoSendChat", "content": chat_line}))
                        
                        last_id = msg_id
            except Exception as e: pass
            time.sleep(2)

    # --- Bot & RPC & Stats ---
    def start_bot_loop(self):
        threading.Thread(target=self.bot_loop, daemon=True).start()

    def bot_loop(self):
        while self.should_run:
            if self.config.get("bot_announce_enabled", False) and self.connected and self.current_lobby_id is not None:
                interval = self.config.get("bot_announce_interval", 5) * 60
                if time.time() - self.last_announce_time > interval:
                    msg = self.config.get("bot_announce_msg", "")
                    if msg:
                        self.send_chat_message(msg)
                        self.last_announce_time = time.time()
            time.sleep(10)

    def get_geo_info(self, ip):
        if ip in self.geo_cache: return self.geo_cache[ip]
        
        def _fetch():
            try:
                with urllib.request.urlopen(f"http://ip-api.com/json/{ip}?fields=status,countryCode,timezone,offset") as r:
                    data = json.loads(r.read().decode())
                    if data.get("status") == "success":
                        offset = data.get('offset', 0)
                        hours = int(offset / 3600)
                        minutes = int((abs(offset) % 3600) / 60)
                        utc_str = f"UTC{'+' if hours >= 0 else ''}{hours}:{minutes:02d}"
                        info = f"[{data.get('countryCode')}] {data.get('timezone')} ({utc_str})"
                        self.geo_cache[ip] = info
                        # Refresh UI if this player is currently shown
                        self.root.after(0, lambda: self.on_lobby_select(None))
            except: pass
        
        threading.Thread(target=_fetch, daemon=True).start()
        return None

    def init_rpc(self):
        if not HAS_RPC: return
        try:
            client_id = self.config.get("rpc_client_id", "133570000000000000")
            self.rpc = Presence(client_id)
            self.rpc.connect()
            self.log("Discord RPC Connected.")
        except Exception as e:
            self.log(f"RPC Error: {e}")

    def toggle_rpc(self):
        self.save_ui_config()
        if self.rpc_enabled_var.get():
            if not self.rpc: self.init_rpc()
        else:
            if self.rpc: 
                self.rpc.close()
                self.rpc = None

    def update_rpc(self, state, details):
        if self.rpc and self.config.get("rpc_enabled", False):
            try:
                self.rpc.update(state=state, details=details, large_image="bz98_icon", large_text="Battlezone 98 Redux")
            except: pass

    def draw_stats(self):
        self.stats_canvas.delete("all")
        w = self.stats_canvas.winfo_width()
        h = self.stats_canvas.winfo_height()
        if w < 50: return
        
        filename = "bzr_stats.csv"
        if not os.path.exists(filename):
            self.stats_canvas.create_text(w/2, h/2, text="No stats data found.", fill="white")
            return
            
        data_points = {} # timestamp -> total_players
        
        try:
            with open(filename, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                next(reader, None) # Skip header
                for row in reader:
                    if len(row) < 5: continue
                    ts_str = row[0]
                    try:
                        dt = datetime.fromisoformat(ts_str)
                        # Filter last 24h
                        if datetime.now() - dt > timedelta(hours=24): continue
                        
                        players = int(row[4])
                        
                        # Group by minute to reduce noise
                        key = dt.replace(second=0, microsecond=0)
                        if key not in data_points: data_points[key] = 0
                        # Since CSV has one row per lobby, we need to sum them for the same timestamp
                        # But timestamps are exact per snapshot. 
                        # We can assume unique timestamps per snapshot.
                        # Actually, let's just take the max players seen in a 5-min window?
                        # Simpler: Just plot raw points.
                        # Better: Group by snapshot. A snapshot shares the exact ISO string.
                        if ts_str not in data_points: data_points[ts_str] = 0
                        data_points[ts_str] += players
                    except: pass
        except: pass
        
        if not data_points: return
        
        sorted_pts = sorted([(datetime.fromisoformat(k), v) for k, v in data_points.items()], key=lambda x: x[0])
        
        max_p = max([p[1] for p in sorted_pts]) if sorted_pts else 10
        if max_p == 0: max_p = 10
        
        # Draw
        pad = 40
        prev_x, prev_y = None, None
        start_time = sorted_pts[0][0]
        total_seconds = (sorted_pts[-1][0] - start_time).total_seconds()
        if total_seconds == 0: total_seconds = 1
        
        for dt, count in sorted_pts:
            secs = (dt - start_time).total_seconds()
            x = pad + (secs / total_seconds) * (w - 2*pad)
            y = h - pad - (count / max_p) * (h - 2*pad)
            
            if prev_x is not None:
                self.stats_canvas.create_line(prev_x, prev_y, x, y, fill=self.colors["highlight"], width=2)
            prev_x, prev_y = x, y
            
        self.stats_canvas.create_text(pad, h-pad+15, text=start_time.strftime("%H:%M"), fill="gray", anchor="w")
        self.stats_canvas.create_text(w-pad, h-pad+15, text=sorted_pts[-1][0].strftime("%H:%M"), fill="gray", anchor="e")
        self.stats_canvas.create_text(pad-5, pad, text=str(max_p), fill="gray", anchor="e")
        self.stats_canvas.create_text(pad-5, h-pad, text="0", fill="gray", anchor="e")

if __name__ == "__main__":
    root = tk.Tk()
    app = BZLobbyMonitor(root)
    root.mainloop()

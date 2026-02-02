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

if sys.platform == 'win32':
    import winreg

# Try to import pypresence for Discord RPC
try:
    from pypresence import Presence
    HAS_RPC = True
except ImportError:
    HAS_RPC = False

# Try to import websocket-client
try:
    import websocket
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
            "bot_announce_enabled": False,
            "bot_announce_msg": "Join our Discord!",
            "bot_announce_interval": 5,
            "rpc_enabled": False,
            "rpc_client_id": "133570000000000000" # Placeholder
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
        
        self.setup_lobby_tab()
        self.setup_config_tab()
        self.setup_discord_tab()
        self.setup_bot_tab()
        self.setup_stats_tab()

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
        ttk.Button(action_frame, text="Create", command=self.create_lobby).pack(side="left", padx=2)
        
        ttk.Separator(action_frame, orient="vertical").pack(side="left", padx=5, fill="y")
        
        ttk.Button(action_frame, text="Join Selected", command=self.join_selected_lobby).pack(side="left", padx=2)
        self.leave_btn = ttk.Button(action_frame, text="Refresh Lounge", command=self.leave_or_refresh_lounge)
        self.leave_btn.pack(side="left", padx=2)
        ttk.Button(action_frame, text="Join (Steam)", command=self.join_steam_lobby).pack(side="left", padx=2)
        ttk.Button(action_frame, text="Post Status (Discord)", command=self.post_lobby_status).pack(side="left", padx=2)
        ttk.Button(action_frame, text="Ping", command=self.ping_server).pack(side="left", padx=2)

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
        
        ttk.Button(chat_input_frame, text="Send", command=self.send_chat).pack(side="right", padx=2)

        # 3. Player Details (Right)
        player_frame = ttk.LabelFrame(bottom_pane, text="Player Details", padding=5)
        bottom_pane.add(player_frame, weight=1)
        
        self.player_details_text = tk.Text(player_frame, height=10, width=30, state="disabled",
                                           bg="#050505", fg=self.colors["fg"], insertbackground=self.colors["highlight"], font=("Consolas", 9))
        self.player_details_text.pack(fill="both", expand=True)
        self.player_details_text.bind("<Button-3>", self.show_player_context_menu)

        # Configure tags for links
        for widget in [self.lobby_details_text, self.player_details_text]:
            widget.tag_config("link", foreground=self.colors["accent"], underline=1)
            widget.tag_bind("link", "<Enter>", lambda e, w=widget: w.config(cursor="hand2"))
            widget.tag_bind("link", "<Leave>", lambda e, w=widget: w.config(cursor=""))
            widget.tag_bind("link", "<Button-1>", self.on_link_click)
            widget.tag_config("griefer", foreground="red", font=("Segoe UI", 9, "bold"))

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
        ttk.Button(p_grid, text="Test Proxy", command=self.test_proxy).pack(side="left", padx=5)
        
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
        self.config["bot_announce_enabled"] = self.bot_announce_enabled_var.get()
        self.config["bot_announce_msg"] = self.bot_announce_msg_var.get()
        self.config["bot_announce_interval"] = self.bot_announce_int_var.get()
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
        if sys.platform != 'win32': return
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

    def on_window_close_attempt(self):
        self.root.iconify()

    def trigger_alert(self, alert_type, data=None):
        should_alert = False
        if alert_type == "new_lobby" and self.config.get("alert_new_lobby"): should_alert = True
        elif alert_type == "player_join" and self.config.get("alert_player_join"):
            if self.config.get("alert_watch_only"):
                watch_list = self.config.get("watch_list", "").lower().splitlines()
                # data is player name
                if data and any(w.strip() in str(data).lower() for w in watch_list if w.strip()):
                    should_alert = True
            else:
                should_alert = True
        elif alert_type == "griefer_join" and self.config.get("alert_griefer"): should_alert = True
        elif alert_type == "disconnect" and self.config.get("alert_disconnect"): should_alert = True
            
        if should_alert:
            if self.config.get("alert_sound"): self.root.bell()
            if self.config.get("alert_flash"): self.flash_window()

    def flash_window(self):
        if sys.platform == 'win32':
            try:
                ctypes.windll.user32.FlashWindow(int(self.root.wm_frame(), 16), True)
            except: pass

    def quit_app(self):
        self.should_run = False
        self.save_config()
        self.root.destroy()
        sys.exit(0)

    def on_game_select(self, event):
        game = self.game_var.get()
        if game == "Battlezone 98 Redux":
            self.host_var.set("battlezone98mp.webdev.rebellion.co.uk:1337")
        elif game == "Battlezone Combat Commander":
            self.host_var.set("battlezone99mp.webdev.rebellion.co.uk:61111")

    def create_lobby(self):
        name = self.new_lobby_var.get()
        if not name: return
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
        selected_items = self.tree.selection()
        if not selected_items:
            messagebox.showinfo("Info", "No lobby selected.")
            return
            
        # Prevent joining a lobby if already in one
        if self.current_lobby_id is not None:
            messagebox.showwarning("Warning", "You are already in a lobby. Please leave it first.")
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
        if not selected_items: return
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
        if self.current_lobby_id is not None:
            msg = {"type": "DoExitLobby", "content": self.current_lobby_id}
            self.log(f"Requesting Exit Lobby: {self.current_lobby_id}")
        else:
            msg = {"type": "DoEnterLounge", "content": True}
            self.log("Requesting Refresh Lounge...")
        
        if self.ws and self.connected:
            self.ws.send(json.dumps(msg))

    def send_chat(self, event=None):
        text = self.chat_var.get()
        if not text: return
        self.send_chat_message(text)
        self.chat_var.set("")

    def send_chat_message(self, text):
        msg = {"type": "DoSendChat", "content": text}
        if self.ws and self.connected:
            self.ws.send(json.dumps(msg))
            
    def ping_server(self):
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

    def _log_impl(self, message):
        # UI Log
        self.log_text.config(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        
        # File Log
        if self.config.get("logging_enabled", False):
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
        # ID (0x01) + Time (8 bytes) + Magic (16 bytes) + GUID (8 bytes)
        magic = b'\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78'
        
        while self.should_run:
            try:
                current_time = int(time.time() * 1000) & 0xFFFFFFFFFFFFFFFF
                ping_pkt = b'\x01' + current_time.to_bytes(8, 'big') + magic + b'\x00'*8
                
                sock.sendto(ping_pkt, (host, port))
                
                try:
                    data, addr = sock.recvfrom(4096)
                    # 0x1C is Unconnected Pong
                    if data and data[0] == 0x1C:
                        # Pong Data starts at offset 33 (1+8+8+16)
                        server_info = data[33:].decode('utf-8', errors='ignore')
                        self.log(f"RakNet Pong from {addr}: {server_info}")
                    else:
                        self.log(f"RX {len(data)}b from {addr}")
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

    def run_ws(self, url):
        # websocket.enableTrace(True)
        
        proxy_opts = {}
        if self.config.get("proxy_enabled", False):
            host = self.config.get("proxy_host", "").strip()
            port = self.config.get("proxy_port", "").strip()
            if host and port:
                proxy_opts["http_proxy_host"] = host
                proxy_opts["http_proxy_port"] = port
                self.log(f"Using Proxy: {host}:{port}")
        
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
        
        # Relay to Discord
        if self.config.get("discord_enabled", False) and self.config.get("discord_relay_to_discord", True):
            target_lobby = self.discord_lobby_id_var.get()
            if str(self.current_lobby_id) == str(target_lobby):
                self.send_to_discord(message=f"**{author}**: {text}")

    def handle_member_list_changed(self, data):
        member = data.get("member")
        lid = data.get("lobbyId")
        action = "left" if data.get("removed") else "joined"
        self.log(f"User {member} {action} lobby {lid}")
        if not data.get("removed"):
            self.trigger_alert("player_join", member)
            
            # Bot Welcome
            if self.config.get("bot_enabled", False) and self.connected:
                if self.current_lobby_id == int(lid):
                     msg = self.config.get("bot_welcome_msg", "")
                     if msg:
                         final_msg = msg.replace("{player}", member)
                         self.send_chat_message(final_msg)
                         
        # Refresh details if we are looking at this lobby
        self.check_and_update_current_lobby()

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
            
            self.tree.insert("", "end", values=(lid, name, map_name, owner, player_count, game_type, version, locked, is_private))

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
        for uid, user in users.items():
            user_name = user.get('name', 'Unknown')
            user_meta = user.get('metadata', {})
            
            # Fallback to metadata if root name is unknown
            if user_name == 'unknown' or not user_name:
                user_name = user_meta.get('name', 'Unknown')
                
            self.player_details_text.insert("end", f" - {user_name} (ID: {uid})\n")
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
                if self._test_proxy_connection(host, port):
                    self.root.after(0, lambda h=host, p=port: self._set_proxy_ui(h, p))
                    self.log(f"Found working proxy: {host}:{port}")
                    return
            
            self.log("Could not find a working proxy in the sample.")
        except Exception as e:
            self.log(f"Proxy search failed: {e}")

    def _test_proxy_connection(self, host, port):
        try:
            # Try to connect to a reliable site via the proxy
            proxy_handler = urllib.request.ProxyHandler({'http': f"{host}:{port}", 'https': f"{host}:{port}"})
            opener = urllib.request.build_opener(proxy_handler)
            opener.open("http://www.google.com", timeout=5)
            return True
        except:
            return False

    def test_proxy(self):
        host = self.proxy_host_var.get()
        port = self.proxy_port_var.get()
        if not host or not port: return
        
        def run_test():
            self.root.after(0, lambda: self._set_proxy_indicator(None))
            if self._test_proxy_connection(host, port):
                self.log("Proxy Test: SUCCESS")
                self.root.after(0, lambda: self._set_proxy_indicator(True))
            else:
                self.log("Proxy Test: FAILED")
                self.root.after(0, lambda: self._set_proxy_indicator(False))
        threading.Thread(target=run_test, daemon=True).start()

    def _set_proxy_ui(self, host, port):
        self.proxy_host_var.set(host)
        self.proxy_port_var.set(port)
        self.proxy_enabled_var.set(True)
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

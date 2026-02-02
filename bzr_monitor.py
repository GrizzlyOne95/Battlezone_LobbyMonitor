"""
Battlezone 98 Redux Lobby Monitor
A tool for monitoring and interacting with Battlezone game lobbies.
Supports WebSocket (BZ98R) and RakNet (BZCC) protocols.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import json
import threading
import time
import sys
import webbrowser
import urllib.request
import re
from io import BytesIO
import socket

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

class BZLobbyMonitor:
    def __init__(self, root):
        self.root = root
        self.root.title("Battlezone Redux Lobby Monitor")
        self.root.geometry("1000x700")
        
        self.lobbies = {}
        self.ws = None
        self.ws_thread = None
        self.connected = False
        self.should_run = True
        self.current_lobby_id = None
        self.my_id = None
        self.image_cache = {}
        self.pending_fetches = set()
        
        self.setup_ui()
        
        if not websocket:
            messagebox.showerror("Missing Dependency", "Please install 'websocket-client' to use this tool.\npip install websocket-client")
            # We don't destroy root immediately to let user see the UI, but disable connect
            self.connect_btn.config(state="disabled")
            self.log("ERROR: 'websocket-client' library not found.")
            self.log("Run: pip install websocket-client")

    def setup_ui(self):
        # Top Bar: Connection
        top_frame = ttk.Frame(self.root, padding=5)
        top_frame.pack(fill="x")
        
        ttk.Label(top_frame, text="Game:").pack(side="left", padx=2)
        self.game_var = tk.StringVar()
        self.game_combo = ttk.Combobox(top_frame, textvariable=self.game_var, state="readonly", width=28)
        self.game_combo['values'] = ("Battlezone 98 Redux", "Battlezone Combat Commander")
        self.game_combo.current(0)
        self.game_combo.pack(side="left", padx=2)
        self.game_combo.bind("<<ComboboxSelected>>", self.on_game_select)
        
        ttk.Label(top_frame, text="Name:").pack(side="left", padx=2)
        self.name_var = tk.StringVar(value="PythonUser")
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
        ttk.Label(top_frame, textvariable=self.status_var).pack(side="right", padx=5)

        self.current_lobby_var = tk.StringVar(value="In Lounge")
        ttk.Label(top_frame, textvariable=self.current_lobby_var, foreground="cyan").pack(side="right", padx=5)

        # Action Bar: Lobby Controls
        action_frame = ttk.Frame(self.root, padding=5)
        action_frame.pack(fill="x")
        
        ttk.Label(action_frame, text="New Lobby:").pack(side="left", padx=2)
        self.new_lobby_var = tk.StringVar(value="MyLobby")
        ttk.Entry(action_frame, textvariable=self.new_lobby_var, width=15).pack(side="left", padx=2)
        ttk.Button(action_frame, text="Create", command=self.create_lobby).pack(side="left", padx=2)
        
        ttk.Separator(action_frame, orient="vertical").pack(side="left", padx=5, fill="y")
        
        ttk.Button(action_frame, text="Join Selected", command=self.join_selected_lobby).pack(side="left", padx=2)
        self.leave_btn = ttk.Button(action_frame, text="Refresh Lounge", command=self.leave_or_refresh_lounge)
        self.leave_btn.pack(side="left", padx=2)
        ttk.Button(action_frame, text="Ping", command=self.ping_server).pack(side="left", padx=2)

        # Main Content: PanedWindow
        paned = ttk.PanedWindow(self.root, orient="vertical")
        paned.pack(fill="both", expand=True, padx=5, pady=5)
        
        # Top Pane: Lobby List
        lobby_frame = ttk.LabelFrame(paned, text="Lobbies", padding=5)
        paned.add(lobby_frame, weight=3)
        
        columns = ("ID", "Name", "Map", "Owner", "Players", "Type", "Version", "Locked", "Private")
        self.tree = ttk.Treeview(lobby_frame, columns=columns, show="headings")
        
        for col in columns:
            self.tree.heading(col, text=col)
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
        
        self.lobby_details_text = tk.Text(details_frame, height=10, width=30, state="disabled")
        self.lobby_details_text.pack(fill="both", expand=True)
        
        # 2. Chat & Logs (Middle)
        log_frame = ttk.LabelFrame(bottom_pane, text="Chat & Logs", padding=5)
        bottom_pane.add(log_frame, weight=2)
        
        self.log_text = tk.Text(log_frame, height=10, width=40, state="disabled")
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
        
        self.player_details_text = tk.Text(player_frame, height=10, width=30, state="disabled")
        self.player_details_text.pack(fill="both", expand=True)

        # Configure tags for links
        for widget in [self.lobby_details_text, self.player_details_text]:
            widget.tag_config("link", foreground="blue", underline=1)
            widget.tag_bind("link", "<Enter>", lambda e, w=widget: w.config(cursor="hand2"))
            widget.tag_bind("link", "<Leave>", lambda e, w=widget: w.config(cursor=""))
            widget.tag_bind("link", "<Button-1>", self.on_link_click)
            widget.tag_config("griefer", foreground="red", font=("Segoe UI", 9, "bold"))

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
        msg = {"type": "DoSendChat", "content": text}
        if self.ws and self.connected:
            self.ws.send(json.dumps(msg))
            self.chat_var.set("")
            
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
        self.log_text.config(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

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

    def run_ws(self, url):
        # websocket.enableTrace(True)
        self.ws = websocket.WebSocketApp(url,
                                  on_open=self.on_open,
                                  on_message=self.on_message,
                                  on_error=self.on_error,
                                  on_close=self.on_close)
        
        self.ws.run_forever()

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
            self.lobbies[str(lid)] = lobby
        self.log(f"Lobbies Updated: {list(changed_lobbies.keys())}")
        self.root.after(0, self.refresh_tree)
        self.root.after(0, self.check_and_update_current_lobby)

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
        
        text = chat.get('text', '')
        self.log(f"[CHAT] {author}: {text}")

    def handle_member_list_changed(self, data):
        member = data.get("member")
        lid = data.get("lobbyId")
        action = "left" if data.get("removed") else "joined"
        self.log(f"User {member} {action} lobby {lid}")
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
        else:
            self.root.after(0, lambda: self.current_lobby_var.set("In Lounge"))
            self.root.after(0, lambda: self.leave_btn.config(text="Refresh Lounge"))

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
        
        self.lobby_details_text.insert("end", f"Lobby ID: {lobby.get('id')}\n")
        
        l_meta = lobby.get('metadata', {})
        self.lobby_details_text.insert("end", f"Name: {l_meta.get('name')}\n")
        self.lobby_details_text.insert("end", f"Created: {lobby.get('createdTime')}\n")
        
        # Parse Game Settings from Lobby Metadata
        game_settings = l_meta.get('gameSettings')
        if game_settings:
             parts = game_settings.split('*')
             if len(parts) > 1:
                 self.lobby_details_text.insert("end", f"Map: {parts[1]}\n")
             if len(parts) > 3 and parts[3] not in ['0', '']:
                 mod_id = parts[3]
                 
                 if HAS_PIL:
                     if mod_id in self.image_cache:
                         self.lobby_details_text.image_create("end", image=self.image_cache[mod_id])
                         self.lobby_details_text.insert("end", "\n")
                     elif mod_id not in self.pending_fetches:
                         self.fetch_image(mod_id, is_mod=True)
                         
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
                    self.root.after(0, lambda: self._cache_image(target_id, data))
            else:
                self.pending_fetches.discard(target_id)

        except Exception as e:
            print(f"Image fetch failed for {target_id}: {e}")
            self.pending_fetches.discard(target_id)

    def _cache_image(self, target_id, data):
        try:
            img = Image.open(BytesIO(data))
            img.thumbnail((50, 50), Image.Resampling.LANCZOS)
            self.image_cache[target_id] = ImageTk.PhotoImage(img)
            # Refresh current view if applicable
            self.on_lobby_select(None)
        except Exception as e:
            print(f"Error caching image: {e}")
        finally:
            self.pending_fetches.discard(target_id)

if __name__ == "__main__":
    root = tk.Tk()
    app = BZLobbyMonitor(root)
    root.mainloop()

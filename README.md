# Battlezone Redux Lobby Monitor

A comprehensive external tool for monitoring, interacting with, and automating Battlezone 98 Redux multiplayer lobbies. This application provides a standalone interface for chat, lobby management, and analytics without needing to launch the full game.

## Features

### 🎮 Lobby Management
*   **Real-Time Monitoring**: View all active lobbies, player counts, and map details instantly.
*   **Interaction**: Create, Join, and Leave lobbies directly from the desktop client.
*   **Player Insights**: View detailed player information including IP addresses, Authentication type (Steam/GOG), and Geo-Location lookup.
*   **Visual Previews**: Automatically fetches and displays Map and Mod thumbnails from the Steam Workshop.
*   **Filtering**: Options to hide locked or full lobbies.

### 🤖 Discord Integration
*   **Chat Relay**: Two-way communication bridge between a specific game lobby and a Discord channel.
*   **Rich Presence (RPC)**: Updates your local Discord profile status to show which lobby you are monitoring or hosting.
*   **Status Broadcasting**: Post rich embeds of the current lobby status (Map, Players, Join Link) to Discord with a single click.

### ⚙️ Automation & Bot Functions
*   **Auto-Greeter**: Automatically sends a customizable welcome message when players join a lobby you are hosting.
*   **Timed Announcements**: Broadcast periodic messages (e.g., community Discord links, server rules) to the lobby chat.
*   **Auto-Reconnect**: Automatically attempts to re-establish connection to the server if dropped.

### 🛡️ Security & Privacy
*   **Proxy Support**: Route WebSocket traffic through a custom or public proxy to mask your IP address.
*   **IP Safety Switch**: Optional configuration to block all connections if the proxy is not verified as working.
*   **Griefer Detection**: Built-in alerting system for known disruptive player IDs.
*   **Watch List**: Configure alerts (Sound/Flash) when specific players join any lobby.

### 📊 Analytics & Logging
*   **Activity Graph**: Visual chart displaying active player trends over the last 24 hours.
*   **CSV Statistics**: Logs game activity (Lobby counts, Player counts, Maps played) to CSV for external analysis.
*   **Event Logging**: Daily text logs of all chat messages and lobby events.

### 🎨 User Interface
*   **Themed UI**: Dark, high-contrast theme inspired by the Battlezone interface.
*   **Custom Fonts**: Supports the game's native font (BZONE.ttf) if installed.
*   **Background Operation**: Can be minimized to the system tray (passive mode) while continuing to log and alert.

## Installation

1.  **Install Python 3.x**: Ensure Python is installed on your system.
2.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
3.  **Run the Application**:
    ```bash
    python bzr_monitor.py
    ```

## Configuration

All settings can be managed via the **Configuration** tab in the application:

*   **Connection**: Set Auto-Reconnect delay.
*   **Proxy**: Configure Host/Port and enable IP Safety.
*   **Window**: Toggle "Minimize to Taskbar" and "Run on Startup".
*   **Logging**: Enable/Disable file logging and set retention period.
*   **Alerts**: Toggle sounds and window flashing for various events.

### Discord Setup
To use the Relay features, navigate to the **Discord Integration** tab:
1.  **Bot Token**: Enter your Discord Bot Token.
2.  **Channel ID**: The ID of the Discord channel to relay messages to/from.
3.  **Lobby ID**: The specific game lobby ID you wish to bridge.

## Requirements

*   `websocket-client`: For connecting to the BZ98R lobby server.
*   `Pillow`: For processing map and mod preview images.
*   `pypresence`: For Discord Rich Presence support.

## Supported Games

*   **Battlezone 98 Redux**: Full support (WebSocket).
*   **Battlezone Combat Commander**: Monitoring only (RakNet UDP).

---

*This tool is a community creation and is not officially affiliated with Rebellion.*
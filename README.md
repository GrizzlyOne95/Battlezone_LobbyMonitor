# Battlezone 98 Redux Lobby Monitor

A Python-based lobby monitor and administration tool for Battlezone 98 Redux and Battlezone Combat Commander.

## Features
- **Multi-Protocol Support**: Monitors Battlezone 98 Redux (WebSocket) and Battlezone Combat Commander (RakNet/UDP).
- **Lobby Management**: Create, join, and inspect lobbies.
- **Chat System**: Real-time chat monitoring and interaction.
- **Player Inspection**: View Steam IDs, IP addresses, and other metadata.
- **Visuals**: Displays map previews and user avatars (requires PIL).

## Installation
1. Install Python 3.x.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the tool:
   ```bash
   python bzr_monitor.py
   ```
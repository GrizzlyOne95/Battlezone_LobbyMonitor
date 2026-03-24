from __future__ import annotations

import base64
from datetime import datetime, timedelta
from typing import Iterable
import zlib


def clean_lobby_name(raw_name, default="Unknown"):
    if raw_name is None:
        return default

    text = str(raw_name)
    if "~~" in text:
        text = text.split("~~")[-1]
    return text or default


def extract_map_name_from_game_settings(game_settings, default="?"):
    if not game_settings:
        return default

    parts = str(game_settings).split("*")
    if len(parts) < 2:
        return default

    map_name = parts[1].strip()
    if not map_name or map_name.lower() == "unknown":
        return default
    return map_name


def extract_map_name_from_metadata(metadata, default="?"):
    if not isinstance(metadata, dict):
        return default

    ready_map = extract_map_name_from_game_settings(metadata.get("ready"), default=None)
    if ready_map is not None:
        return ready_map

    settings_map = extract_map_name_from_game_settings(metadata.get("gameSettings"), default=None)
    if settings_map is not None:
        return settings_map

    return default


def extract_workshop_mod_id(game_settings):
    if not game_settings:
        return None

    parts = str(game_settings).split("*")
    if len(parts) > 3 and parts[3] not in ["0", ""]:
        return parts[3]
    return None


def extract_lobby_version(lobby, default="?"):
    if not isinstance(lobby, dict):
        return default

    metadata = lobby.get("metadata", {})
    if isinstance(metadata, dict):
        version = metadata.get("version")
        if version not in [None, ""]:
            return version

    version = lobby.get("clientVersion")
    if version not in [None, ""]:
        return version

    return default


def stamp_lobby(lobby, source, now=None):
    if now is None:
        now = datetime.now()

    lobby["_source"] = source
    lobby["_last_seen"] = now.isoformat()
    return lobby


def get_lobby_source(lobby, default="?"):
    if not isinstance(lobby, dict):
        return default
    return lobby.get("_source") or default


def get_lobby_last_seen(lobby):
    if not isinstance(lobby, dict):
        return None

    raw = lobby.get("_last_seen")
    if not raw:
        return None

    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def format_age(dt, now=None, default="?"):
    if dt is None:
        return default
    if now is None:
        now = datetime.now()

    delta = max(0, int((now - dt).total_seconds()))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    return f"{delta // 3600}h"


def get_lobby_age_label(lobby, now=None, default="?"):
    return format_age(get_lobby_last_seen(lobby), now=now, default=default)


def is_lobby_stale(lobby, now=None):
    if now is None:
        now = datetime.now()

    last_seen = get_lobby_last_seen(lobby)
    if last_seen is None:
        return False

    source = get_lobby_source(lobby, default="")
    thresholds = {
        "BZR WS": 45,
        "BZCC HTTP": 45,
        "BZCC UDP": 20,
    }
    threshold = thresholds.get(source, 60)
    return (now - last_seen).total_seconds() > threshold


def get_lobby_network_label(lobby, default="-"):
    if not isinstance(lobby, dict):
        return default

    metadata = lobby.get("metadata", {})
    if not isinstance(metadata, dict):
        return default

    parts = []
    ping = metadata.get("pingMs")
    if ping not in [None, ""]:
        parts.append(f"{ping}ms")
    tps = metadata.get("tps")
    if tps not in [None, ""]:
        parts.append(f"{tps}tps")

    return " / ".join(parts) if parts else default


def get_lobby_status_flags(lobby, relay_status=None, now=None):
    if not isinstance(lobby, dict):
        return []

    metadata = lobby.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}

    flags = []
    if str(metadata.get("launched")) == "1":
        flags.append("Launched")
    if lobby.get("isLocked"):
        flags.append("Locked")
    if lobby.get("isPrivate"):
        flags.append("Private")
    if metadata.get("connectionStatus"):
        flags.append(str(metadata.get("connectionStatus")))
    if relay_status is True:
        flags.append("Relay On")
    elif relay_status is False:
        flags.append("Relay Off")
    if is_lobby_stale(lobby, now=now):
        flags.append("Stale")
    return flags


def aggregate_recent_player_counts(rows: Iterable[list[str]], now=None, window_hours=24, bucket_minutes=5):
    if now is None:
        now = datetime.now()

    cutoff = now - timedelta(hours=window_hours)
    data_points = {}

    for row in rows:
        if len(row) < 5:
            continue

        ts_str = row[0]
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt < cutoff:
                continue

            players = int(row[4])
        except (TypeError, ValueError):
            continue

        if bucket_minutes and bucket_minutes > 1:
            minute_bucket = (dt.minute // bucket_minutes) * bucket_minutes
            dt = dt.replace(minute=minute_bucket, second=0, microsecond=0)
        else:
            dt = dt.replace(second=0, microsecond=0)

        data_points[dt] = data_points.get(dt, 0) + players

    return sorted(data_points.items(), key=lambda item: item[0])


def decode_bzcc_name(raw_value):
    text = raw_value or ""
    if not isinstance(text, str):
        text = str(text)

    try:
        padded = text + ("=" * ((4 - len(text) % 4) % 4))
        decoded = base64.b64decode(padded)
        return decoded.split(b"\x00")[0].decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return text


def build_bzcc_lobby(game):
    lid = game.get("g")
    if not lid:
        return None

    map_name = game.get("m", "Unknown")
    users = {}
    for player in game.get("pl") or []:
        pid = player.get("i", "Unknown")
        users[pid] = {
            "name": decode_bzcc_name(player.get("n") or ""),
            "id": pid,
            "team": player.get("t"),
            "score": player.get("s"),
        }

    lobby = {
        "id": lid,
        "metadata": {
            "name": decode_bzcc_name(game.get("n") or ""),
            "gameType": "BZCC",
            "map": map_name,
            "gameSettings": f"*{map_name}*",
            "ready": f"*{map_name}*",
            "version": game.get("v", "?"),
            "typeId": game.get("gt", 0),
            "stateId": game.get("si", 0),
            "maxPlayers": game.get("pm", 0),
            "gameTimeMinutes": game.get("gtm"),
            "typeDetailId": game.get("gtd"),
            "pingMs": game.get("pg"),
            "maxPingMs": game.get("pgm"),
            "modsCrc": game.get("d"),
            "natType": game.get("t"),
            "mapModCrc": game.get("mm"),
            "tps": game.get("tps"),
        },
        "users": users,
        "memberLimit": game.get("pm", 0),
        "isLocked": str(game.get("l")) == "1",
        "isPrivate": str(game.get("k")) == "1",
        "owner": users[next(iter(users))]["id"] if users else "Unknown",
    }
    return str(lid), lobby


def should_relay_discord_message(
    message,
    *,
    bot_id,
    relay_to_lobby_enabled,
    connected,
    current_lobby_id,
    target_lobby_id,
):
    if not relay_to_lobby_enabled or not connected:
        return None

    if str(current_lobby_id) != str(target_lobby_id):
        return None

    if message.get("webhook_id"):
        return None

    content = message.get("content", "")
    if not content:
        return None

    author = message.get("author", {})
    if author.get("id") == bot_id:
        return None

    sender = author.get("username") or "Discord"
    return f"[Discord] {sender}: {content}"


def _decode_null_terminated(raw):
    if not raw:
        return ""
    idx = raw.find(b"\x00")
    if idx >= 0:
        raw = raw[:idx]
    return raw.decode("utf-8", errors="replace").strip()


def parse_bz2_unconnected_pong(data):
    try:
        if len(data) < 40:
            return None
        if int.from_bytes(data[:4], "little") != 0x1C:
            return None

        off = 4
        if data[off] != 0x00:
            return None
        off += 1

        off += 4  # pong echo
        off += 8  # server guid

        for _ in range(3):
            sw = data[off]
            if sw not in (0x00, 0xFF):
                return None
            off += 1

        if data[off] != 0x00:
            return None
        off += 1

        if len(data) < off + 12 + 10:
            return None

        off += 12

        data_version = data[off]
        off += 1
        bitfield_bits = int.from_bytes(data[off:off + 4], "little")
        off += 4
        off += 3  # time_limit, kill_limit, game_time_minutes
        off += 2  # max_ping
        game_version = int.from_bytes(data[off:off + 2], "little")
        off += 2
        compressed_len = int.from_bytes(data[off:off + 2], "little")
        off += 2

        if compressed_len <= 0 or len(data) < off + compressed_len:
            return None

        compressed_payload = data[off:off + compressed_len]
        try:
            inflated = zlib.decompress(compressed_payload)
        except Exception:
            try:
                inflated = zlib.decompress(compressed_payload[2:], -zlib.MAX_WBITS)
            except Exception:
                return None

        if len(inflated) < 1038:
            inflated += b"\x00" * (1038 - len(inflated))

        coff = 0

        def read_bytes(n):
            nonlocal coff
            chunk = inflated[coff:coff + n]
            coff += n
            if len(chunk) < n:
                chunk += b"\x00" * (n - len(chunk))
            return chunk

        session_name = _decode_null_terminated(read_bytes(44))
        map_name = _decode_null_terminated(read_bytes(32))
        mods = _decode_null_terminated(read_bytes(128))
        map_url = _decode_null_terminated(read_bytes(96))
        motd = _decode_null_terminated(read_bytes(128))

        cur_players = (bitfield_bits & 0x3C) >> 2
        max_players = (bitfield_bits & 0x3C0) >> 6
        tps = (bitfield_bits & 0x7C00) >> 10
        b_passworded = (bitfield_bits & 0x02) == 0x02
        b_locked_down = (bitfield_bits & 0x8000) == 0x8000
        cur_players = max(0, min(cur_players, 16))

        players = []
        for i in range(16):
            uname = _decode_null_terminated(read_bytes(33))
            kills = read_bytes(1)[0]
            deaths = read_bytes(1)[0]
            team = read_bytes(1)[0]
            score = int.from_bytes(read_bytes(2), "little", signed=True)
            if i < cur_players:
                players.append({
                    "id": f"P{i+1}",
                    "name": uname if uname else f"Player{i+1}",
                    "kills": kills,
                    "deaths": deaths,
                    "team": team,
                    "score": score,
                })

        return {
            "dataVersion": data_version,
            "gameVersion": game_version,
            "sessionName": session_name,
            "mapName": map_name,
            "mods": mods,
            "mapUrl": map_url,
            "motd": motd,
            "players": players,
            "curPlayers": cur_players,
            "maxPlayers": max_players if max_players > 0 else max(cur_players, len(players)),
            "bPassworded": b_passworded,
            "bLockedDown": b_locked_down,
            "tps": tps,
        }
    except Exception:
        return None


def parse_raknet_frames(data):
    try:
        frames = []
        offset = 4
        while offset < len(data):
            flags = data[offset]
            offset += 1
            reliability = (flags >> 5) & 0x07
            is_split = (flags & 0x10) != 0

            if offset + 2 > len(data):
                break
            length_bits = (data[offset] << 8) | data[offset + 1]
            offset += 2
            length_bytes = (length_bits + 7) // 8

            if reliability in [2, 3, 4]:
                offset += 3
            if reliability in [1, 4]:
                offset += 4
            if reliability == 3:
                offset += 4
            if is_split:
                offset += 10

            if offset + length_bytes > len(data):
                break
            frames.append(data[offset:offset + length_bytes])
            offset += length_bytes
        return frames
    except Exception:
        return []

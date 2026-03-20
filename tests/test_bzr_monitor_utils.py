import unittest
import zlib
from datetime import datetime, timedelta

from bzr_monitor_utils import (
    aggregate_recent_player_counts,
    build_bzcc_lobby,
    clean_lobby_name,
    decode_bzcc_name,
    extract_lobby_version,
    extract_map_name_from_game_settings,
    extract_map_name_from_metadata,
    extract_workshop_mod_id,
    format_age,
    get_lobby_age_label,
    get_lobby_network_label,
    get_lobby_source,
    get_lobby_status_flags,
    is_lobby_stale,
    parse_bz2_unconnected_pong,
    parse_raknet_frames,
    stamp_lobby,
    should_relay_discord_message,
)


class LobbyNameTests(unittest.TestCase):
    def test_clean_lobby_name_strips_prefix(self):
        self.assertEqual(clean_lobby_name("~chat~pub~~My Lobby"), "My Lobby")

    def test_clean_lobby_name_keeps_plain_name(self):
        self.assertEqual(clean_lobby_name("Plain Lobby"), "Plain Lobby")

    def test_clean_lobby_name_uses_default_for_none(self):
        self.assertEqual(clean_lobby_name(None), "Unknown")


class MapParsingTests(unittest.TestCase):
    def test_extract_map_name_prefers_ready(self):
        meta = {"ready": "*isoa*", "gameSettings": "*other*"}
        self.assertEqual(extract_map_name_from_metadata(meta), "isoa")

    def test_extract_map_name_falls_back_to_game_settings(self):
        meta = {"gameSettings": "*bdog*"}
        self.assertEqual(extract_map_name_from_metadata(meta), "bdog")

    def test_extract_map_name_unknown_becomes_default(self):
        self.assertEqual(extract_map_name_from_game_settings("*unknown*"), "?")

    def test_extract_workshop_mod_id_returns_fourth_segment(self):
        self.assertEqual(extract_workshop_mod_id("*map*mode*12345*"), "12345")

    def test_extract_workshop_mod_id_ignores_zero(self):
        self.assertIsNone(extract_workshop_mod_id("*map*mode*0*"))


class VersionTests(unittest.TestCase):
    def test_extract_lobby_version_prefers_metadata(self):
        lobby = {"metadata": {"version": "2.2.301"}, "clientVersion": "2.2.100"}
        self.assertEqual(extract_lobby_version(lobby), "2.2.301")

    def test_extract_lobby_version_falls_back_to_root(self):
        lobby = {"metadata": {}, "clientVersion": "2.2.100"}
        self.assertEqual(extract_lobby_version(lobby), "2.2.100")


class StatsAggregationTests(unittest.TestCase):
    def test_aggregate_recent_player_counts_sums_duplicate_timestamps(self):
        now = datetime(2026, 3, 20, 12, 0, 0)
        ts = now.isoformat()
        rows = [
            [ts, "1", "A", "map1", "3", "8", "BZR"],
            [ts, "2", "B", "map2", "5", "8", "BZR"],
        ]
        points = aggregate_recent_player_counts(rows, now=now)
        self.assertEqual(points, [(now, 8)])

    def test_aggregate_recent_player_counts_filters_old_and_invalid_rows(self):
        now = datetime(2026, 3, 20, 12, 0, 0)
        recent = now - timedelta(hours=1)
        old = now - timedelta(hours=30)
        rows = [
            [recent.isoformat(), "1", "A", "map1", "4", "8", "BZR"],
            [old.isoformat(), "2", "B", "map2", "9", "8", "BZR"],
            ["not-a-date", "3", "C", "map3", "2", "8", "BZR"],
            [recent.isoformat(), "4", "D", "map4", "bad-int", "8", "BZR"],
        ]
        points = aggregate_recent_player_counts(rows, now=now)
        self.assertEqual(points, [(recent, 4)])

    def test_aggregate_recent_player_counts_buckets_by_five_minutes(self):
        now = datetime(2026, 3, 20, 12, 10, 0)
        rows = [
            [datetime(2026, 3, 20, 12, 3, 0).isoformat(), "1", "A", "map1", "2", "8", "BZR"],
            [datetime(2026, 3, 20, 12, 4, 30).isoformat(), "2", "B", "map2", "5", "8", "BZR"],
        ]
        points = aggregate_recent_player_counts(rows, now=now)
        self.assertEqual(points, [(datetime(2026, 3, 20, 12, 0, 0), 7)])


class LobbyFreshnessTests(unittest.TestCase):
    def test_stamp_lobby_sets_source_and_age_fields(self):
        now = datetime(2026, 3, 20, 12, 0, 0)
        lobby = stamp_lobby({}, "BZR WS", now=now)
        self.assertEqual(get_lobby_source(lobby), "BZR WS")
        self.assertEqual(get_lobby_age_label(lobby, now=now), "0s")

    def test_is_lobby_stale_uses_source_threshold(self):
        now = datetime(2026, 3, 20, 12, 1, 0)
        lobby = stamp_lobby({}, "BZCC UDP", now=now - timedelta(seconds=30))
        self.assertTrue(is_lobby_stale(lobby, now=now))

    def test_get_lobby_network_label_formats_ping_and_tps(self):
        lobby = {"metadata": {"pingMs": 42, "tps": 20}}
        self.assertEqual(get_lobby_network_label(lobby), "42ms / 20tps")

    def test_get_lobby_status_flags_includes_state(self):
        lobby = stamp_lobby({"metadata": {"launched": "1"}, "isLocked": True}, "BZR WS", now=datetime(2026, 3, 20, 12, 0, 0))
        flags = get_lobby_status_flags(lobby, relay_status=True, now=datetime(2026, 3, 20, 12, 0, 10))
        self.assertIn("Launched", flags)
        self.assertIn("Locked", flags)
        self.assertIn("Relay On", flags)


class BzccDecodeTests(unittest.TestCase):
    def test_decode_bzcc_name_handles_base64(self):
        self.assertEqual(decode_bzcc_name("VGVzdE5hbWU="), "TestName")

    def test_decode_bzcc_name_falls_back_to_raw_value(self):
        self.assertEqual(decode_bzcc_name("not-base64%%%"), "not-base64%%%")

    def test_build_bzcc_lobby_decodes_name_and_players(self):
        game = {
            "g": "GUID123",
            "n": "TXkgTG9iYnk=",
            "m": "isoa",
            "v": "S1",
            "pm": 8,
            "l": 1,
            "k": 0,
            "pl": [
                {"i": "P1", "n": "QWxpY2U=", "t": 1, "s": 10},
                {"i": "P2", "n": "Qm9i", "t": 2, "s": 4},
            ],
        }
        lid, lobby = build_bzcc_lobby(game)
        self.assertEqual(lid, "GUID123")
        self.assertEqual(lobby["metadata"]["name"], "My Lobby")
        self.assertEqual(lobby["metadata"]["map"], "isoa")
        self.assertEqual(lobby["users"]["P1"]["name"], "Alice")
        self.assertEqual(lobby["owner"], "P1")


class DiscordRelayTests(unittest.TestCase):
    def test_should_relay_discord_message_builds_chat_line(self):
        message = {"author": {"id": "user-1", "username": "Alice"}, "content": "hello"}
        chat_line = should_relay_discord_message(
            message,
            bot_id="bot-1",
            relay_to_lobby_enabled=True,
            connected=True,
            current_lobby_id="42",
            target_lobby_id="42",
        )
        self.assertEqual(chat_line, "[Discord] Alice: hello")

    def test_should_relay_discord_message_rejects_bot_messages(self):
        message = {"author": {"id": "bot-1", "username": "RelayBot"}, "content": "echo"}
        chat_line = should_relay_discord_message(
            message,
            bot_id="bot-1",
            relay_to_lobby_enabled=True,
            connected=True,
            current_lobby_id="42",
            target_lobby_id="42",
        )
        self.assertIsNone(chat_line)


class RakNetParsingTests(unittest.TestCase):
    def test_parse_raknet_frames_extracts_unreliable_payload(self):
        packet = b"\x84\x00\x00\x00" + b"\x00\x00\x08" + b"\x61"
        self.assertEqual(parse_raknet_frames(packet), [b"\x61"])

    def test_parse_raknet_frames_extracts_reliable_ordered_payload(self):
        payload = b"\x10\x20"
        packet = (
            b"\x84\x00\x00\x00"
            + b"\x60"
            + (len(payload) * 8).to_bytes(2, "big")
            + b"\x01\x00\x00"
            + b"\x00\x00\x00\x00"
            + payload
        )
        self.assertEqual(parse_raknet_frames(packet), [payload])

    def test_parse_bz2_unconnected_pong_decodes_payload(self):
        def fixed_bytes(text, length):
            raw = text.encode("utf-8")
            return raw + b"\x00" + (b"\x00" * max(0, length - len(raw) - 1))

        inflated = bytearray()
        inflated.extend(fixed_bytes("Session One", 44))
        inflated.extend(fixed_bytes("isoa", 32))
        inflated.extend(fixed_bytes("modpack", 128))
        inflated.extend(fixed_bytes("http://example.com/map", 96))
        inflated.extend(fixed_bytes("Welcome!", 128))

        players = [
            ("Alpha", 3, 1, 1, 12),
            ("Bravo", 1, 2, 2, -4),
        ]
        for i in range(16):
            if i < len(players):
                name, kills, deaths, team, score = players[i]
            else:
                name, kills, deaths, team, score = ("", 0, 0, 0, 0)
            inflated.extend(fixed_bytes(name, 33))
            inflated.append(kills & 0xFF)
            inflated.append(deaths & 0xFF)
            inflated.append(team & 0xFF)
            inflated.extend(int(score).to_bytes(2, "little", signed=True))

        if len(inflated) < 1038:
            inflated.extend(b"\x00" * (1038 - len(inflated)))

        compressed = zlib.compress(bytes(inflated))
        bitfield = (2 << 2) | (6 << 6) | (20 << 10) | 0x02
        packet = bytearray()
        packet.extend((0x1C).to_bytes(4, "little"))
        packet.append(0x00)
        packet.extend((1234).to_bytes(4, "little"))
        packet.extend((5678).to_bytes(8, "little"))
        packet.extend(b"\x00\xff\x00")
        packet.append(0x00)
        packet.extend(bytes.fromhex("fefefefefdfdfdfd78563412"))
        packet.append(1)
        packet.extend(bitfield.to_bytes(4, "little"))
        packet.extend(b"\x00\x00\x00")
        packet.extend((250).to_bytes(2, "little"))
        packet.extend((301).to_bytes(2, "little"))
        packet.extend(len(compressed).to_bytes(2, "little"))
        packet.extend(compressed)

        parsed = parse_bz2_unconnected_pong(bytes(packet))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["sessionName"], "Session One")
        self.assertEqual(parsed["mapName"], "isoa")
        self.assertEqual(parsed["mods"], "modpack")
        self.assertEqual(parsed["curPlayers"], 2)
        self.assertEqual(parsed["maxPlayers"], 6)
        self.assertEqual(parsed["tps"], 20)
        self.assertTrue(parsed["bPassworded"])
        self.assertEqual(parsed["players"][0]["name"], "Alpha")
        self.assertEqual(parsed["players"][1]["score"], -4)

    def test_should_relay_discord_message_rejects_wrong_lobby(self):
        message = {"author": {"id": "user-1", "username": "Alice"}, "content": "hello"}
        chat_line = should_relay_discord_message(
            message,
            bot_id="bot-1",
            relay_to_lobby_enabled=True,
            connected=True,
            current_lobby_id="41",
            target_lobby_id="42",
        )
        self.assertIsNone(chat_line)


if __name__ == "__main__":
    unittest.main()

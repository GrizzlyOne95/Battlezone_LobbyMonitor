import unittest

from bzr_monitor_utils import (
    build_bzcc_lobby,
    build_discord_message_payload,
    is_safe_link,
    list_matches,
    parse_id_list,
    parse_raknet_frames,
    raknet_frame_header_extra,
    should_relay_discord_message,
)


def relay(message, bot_id="bot-1"):
    return should_relay_discord_message(
        message,
        bot_id=bot_id,
        relay_to_lobby_enabled=True,
        connected=True,
        current_lobby_id=42,
        target_lobby_id="42",
    )


class DiscordSafetyTests(unittest.TestCase):
    def test_payload_disables_all_mentions(self):
        payload = build_discord_message_payload(message="**x**: @everyone hi")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(payload["content"], "**x**: @everyone hi")

    def test_payload_with_embed_only(self):
        payload = build_discord_message_payload(embed={"title": "t"})
        self.assertNotIn("content", payload)
        self.assertEqual(payload["embeds"], [{"title": "t"}])
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_payload_truncates_to_discord_limit(self):
        payload = build_discord_message_payload(message="a" * 5000)
        self.assertEqual(len(payload["content"]), 2000)

    def test_relay_refuses_when_bot_id_unknown(self):
        message = {"author": {"id": "user-1", "username": "Alice"}, "content": "hi"}
        self.assertIsNone(relay(message, bot_id=None))

    def test_relay_skips_own_messages_with_numeric_id(self):
        message = {"author": {"id": 123, "username": "Bot"}, "content": "echo"}
        self.assertIsNone(relay(message, bot_id="123"))

    def test_relay_skips_webhooks(self):
        message = {"webhook_id": "w", "author": {"id": "u"}, "content": "hi"}
        self.assertIsNone(relay(message))

    def test_relay_matches_int_and_str_lobby_ids(self):
        message = {"author": {"id": "user-1", "username": "Alice"}, "content": "hi"}
        self.assertEqual(relay(message), "[Discord] Alice: hi")


class LinkSafetyTests(unittest.TestCase):
    def test_allows_web_and_steam(self):
        self.assertTrue(is_safe_link("https://steamcommunity.com/x"))
        self.assertTrue(is_safe_link("HTTP://example.com"))
        self.assertTrue(is_safe_link("steam://rungame/301650/1/+connect_lobby=B1"))

    def test_blocks_other_schemes(self):
        self.assertFalse(is_safe_link("file:///C:/Windows/System32/calc.exe"))
        self.assertFalse(is_safe_link("javascript:alert(1)"))
        self.assertFalse(is_safe_link(None))


class ListMatchingTests(unittest.TestCase):
    def test_parse_id_list_strips_and_lowercases(self):
        self.assertEqual(parse_id_list(" Alice \n\nS123\n"), ["alice", "s123"])
        self.assertEqual(parse_id_list(None), [])

    def test_list_matches_name_or_id(self):
        entries = parse_id_list("alice\nS999")
        self.assertTrue(list_matches(entries, "AliceBZ", "S1"))
        self.assertTrue(list_matches(entries, "Bob", "S999"))
        self.assertFalse(list_matches(entries, "Bob", "S1"))
        self.assertFalse(list_matches(entries, None, ""))


class BzccLobbyFlagTests(unittest.TestCase):
    def game(self, **kw):
        base = {"g": "G1", "n": "", "m": "map", "pl": [{"i": 7, "n": ""}]}
        base.update(kw)
        return base

    def test_password_marks_locked_and_private(self):
        _, lobby = build_bzcc_lobby(self.game(k="1", l="0"))
        self.assertTrue(lobby["isLocked"])
        self.assertTrue(lobby["isPrivate"])
        self.assertTrue(lobby["metadata"]["passwordProtected"])

    def test_locked_without_password(self):
        _, lobby = build_bzcc_lobby(self.game(k="0", l="1"))
        self.assertTrue(lobby["isLocked"])
        self.assertFalse(lobby["isPrivate"])
        self.assertTrue(lobby["metadata"]["locked"])

    def test_open_game(self):
        _, lobby = build_bzcc_lobby(self.game())
        self.assertFalse(lobby["isLocked"])
        self.assertFalse(lobby["isPrivate"])

    def test_user_keys_are_strings(self):
        _, lobby = build_bzcc_lobby(self.game())
        self.assertIn("7", lobby["users"])
        self.assertEqual(lobby["owner"], 7)

    def test_ignores_malformed_players(self):
        _, lobby = build_bzcc_lobby(self.game(pl=["junk", None, {"i": "P1", "n": ""}]))
        self.assertEqual(list(lobby["users"]), ["P1"])


class RakNetHeaderTests(unittest.TestCase):
    def test_header_sizes_per_reliability(self):
        self.assertEqual(raknet_frame_header_extra(0, False), 0)  # unreliable
        self.assertEqual(raknet_frame_header_extra(1, False), 7)  # unreliable sequenced
        self.assertEqual(raknet_frame_header_extra(2, False), 3)  # reliable
        self.assertEqual(raknet_frame_header_extra(3, False), 7)  # reliable ordered
        self.assertEqual(raknet_frame_header_extra(4, False), 10)  # reliable sequenced
        self.assertEqual(raknet_frame_header_extra(3, True), 17)

    def test_parse_reliable_sequenced_frame(self):
        payload = b"\x61\x01"
        packet = (
            b"\x84\x00\x00\x00"
            + bytes([4 << 5])
            + (len(payload) * 8).to_bytes(2, "big")
            + b"\x00" * 10
            + payload
        )
        self.assertEqual(parse_raknet_frames(packet), [payload])

    def test_parse_two_frames(self):
        packet = (
            b"\x84\x00\x00\x00"
            + b"\x00\x00\x08\x10"
            + b"\x40\x00\x08\x00\x00\x00\x61"
        )
        self.assertEqual(parse_raknet_frames(packet), [b"\x10", b"\x61"])


if __name__ == "__main__":
    unittest.main()

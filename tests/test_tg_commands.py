"""Tests for Telegram owner-command encoding and parsing."""

from __future__ import annotations

import pytest

from gateway_policy.tg_commands import decode_chat_id, encode_chat_id, parse_owner_command


class TestEncodeDecode:
    def test_whatsapp_lid_round_trip(self):
        raw = "122299244130458@lid"
        enc = encode_chat_id(raw)
        assert enc == "122299244130458_AT_lid"
        assert decode_chat_id(enc) == raw

    def test_whatsapp_s_whatsapp_net_round_trip(self):
        raw = "60173380115@s.whatsapp.net"
        enc = encode_chat_id(raw)
        assert enc == "60173380115_AT_s_DOT_whatsapp_DOT_net"
        assert decode_chat_id(enc) == raw

    def test_telegram_numeric_unchanged(self):
        raw = "640466638"
        assert encode_chat_id(raw) == raw
        assert decode_chat_id(raw) == raw

    def test_encode_rejects_unsupported_chars(self):
        with pytest.raises(ValueError):
            encode_chat_id("bad+id@s.whatsapp.net")


class TestParseOwnerCommand:
    def test_takeback_encoded_lid(self):
        assert parse_owner_command("/takeback_122299244130458_AT_lid") == (
            "takeback",
            "122299244130458@lid",
        )

    def test_handover_numeric(self):
        assert parse_owner_command("/handover_60173380115") == (
            "handover",
            "60173380115",
        )

    def test_extra_arg_no_match(self):
        assert (
            parse_owner_command("/takeback_foo@bot 122299244130458_AT_lid")
            is None
        )

    def test_strip_bot_prefix_in_verb(self):
        assert parse_owner_command(
            "/takeback@hermesbot_122299244130458_AT_lid"
        ) == ("takeback", "122299244130458@lid")

    def test_random_no_match(self):
        assert parse_owner_command("/random_text") is None

    def test_trailing_bot_suffix_stripped(self):
        assert parse_owner_command(
            "/takeback_122299244130458_AT_lid@hermesbot"
        ) == ("takeback", "122299244130458@lid")

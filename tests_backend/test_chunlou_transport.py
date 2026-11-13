"""Public player version 1, checked against a synthetic browser-generated vector."""

import pytest

from backend.chunlou_transport import decode_chunlou_key, playback_key_request

KEY_URL = "https://hls.chunlou.tv/key/025dcf55-68b0-45a6-8392-268bfc3eaa07/12?n=nonce%2B%2Ftest&s=case%2b"
ENVELOPE = bytes.fromhex("01120baced7b95aaeafb433b6f4a20d31f")


def test_browser_wire_format_and_literal_transport_signature():
    assert playback_key_request(KEY_URL) == KEY_URL + "&w=1"
    assert playback_key_request(KEY_URL + "&w=1") == KEY_URL + "&w=1"
    assert decode_chunlou_key(KEY_URL, ENVELOPE) == bytes(range(16))
    assert decode_chunlou_key(KEY_URL, bytes(range(16))) == bytes(range(16))


@pytest.mark.parametrize("body", [b"", b"too short", bytes(17), bytes(18)])
def test_bad_response_never_becomes_a_key(body):
    with pytest.raises(ValueError):
        decode_chunlou_key(KEY_URL, body)

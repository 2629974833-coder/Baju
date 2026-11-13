"""Version 1 key response format used by the official Chunlou HLS web player."""

import hashlib
import re
from urllib.parse import parse_qs, urlsplit

# Public wire-format label from the Feed player, shared by all clients.
_V1_LABEL = "clw1.BEM3C65o3RoBXTOf2i-d_IR4"
_KEY_PATH = re.compile(r"/key/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/([0-9]+)")


def playback_key_request(https_key_url):
    """Ask for the web player's wrapped response without re-encoding the signed query."""
    parsed = urlsplit(https_key_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "hls.chunlou.tv"
        or parsed.fragment
        or not _KEY_PATH.fullmatch(parsed.path)
    ):
        raise ValueError("春楼播放密钥地址与播放器协议不符")
    if re.search(r"(?:^|&)w=1(?:&|$)", parsed.query):
        return https_key_url
    separator = "&" if "?" in https_key_url else "?"
    return https_key_url + separator + "w=1"


def decode_chunlou_key(https_key_url, payload):
    """Decode the response for the signed episode and rotation that requested it."""
    if len(payload) == 16:
        return payload
    if len(payload) != 17 or payload[0] != 1:
        raise ValueError("春楼播放器返回了无法识别的密钥响应")
    parsed = urlsplit(https_key_url)
    match = _KEY_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("春楼播放密钥缺少集号或轮换编号")
    episode, rotation = match.groups()
    nonce = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=100).get("n", [""])[0]
    mask = hashlib.sha256(f"{_V1_LABEL}:{episode}:{rotation}:{nonce}".encode()).digest()
    return bytes(a ^ b for a, b in zip(payload[1:], mask[:16], strict=True))

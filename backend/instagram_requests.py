"""The pinned upstream logged-out read query, bound to one Instagram post."""

import json
from urllib.parse import parse_qs, urlsplit

from yt_dlp.extractor.instagram import _id_to_pk

READ_DOCUMENT_ID = "27130156389949648"
READ_QUERY_NAME = "PolarisLoggedOutDesktopWWWPostRootContentQuery"
READ_ENDPOINT = "https://www.instagram.com/api/graphql"
READ_HEADERS = {"x-fb-lsd", "x-csrftoken", "x-ig-app-id", "x-fb-friendly-name",
                "x-requested-with", "x-asbd-id", "x-ig-www-claim"}


def instagram_read_request(target, url, method, data):
    if (not target or target.platform != "instagram" or not target.post_id or method != "POST"
            or not isinstance(data, bytes) or not 0 < len(data) <= 16384):
        return False
    try:
        parsed = urlsplit(url)
        if url != READ_ENDPOINT or parsed.query or parsed.fragment:
            return False
        fields = parse_qs(data.decode("ascii"), strict_parsing=True, max_num_fields=6, keep_blank_values=True)
        required = {"lsd", "fb_api_caller_class", "fb_api_req_friendly_name",
                    "server_timestamps", "variables", "doc_id"}
        if set(fields) != required or any(len(values) != 1 for values in fields.values()):
            return False
        values = {key: value[0] for key, value in fields.items()}
        return (0 < len(values["lsd"]) <= 1024
                and not any(ord(c) < 32 or ord(c) == 127 for c in values["lsd"])
                and values["fb_api_caller_class"] == "RelayModern"
                and values["fb_api_req_friendly_name"] == READ_QUERY_NAME
                and values["server_timestamps"] == "true"
                and values["doc_id"] == READ_DOCUMENT_ID
                and json.loads(values["variables"]) == {"media_id": str(_id_to_pk(target.post_id))})
    except (ValueError, TypeError, UnicodeError):
        return False

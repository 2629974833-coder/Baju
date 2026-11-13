"""Small, strict download preferences shared by API, manifests and queue jobs."""

import re

from yt_dlp.utils import ISO639Utils

_LANGUAGE = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8}){0,4}", re.ASCII)


def normalize_language(value):
    if not isinstance(value, str):
        raise ValueError("音轨语言必须是语言代码")
    tag = value.strip().lower()
    if len(tag) > 35 or not _LANGUAGE.fullmatch(tag):
        raise ValueError("音轨语言代码无效，请使用 en、zh 或 en-US 等语言标记")
    parts = tag.split("-")
    if len(parts[0]) == 3:
        parts[0] = ISO639Utils.long2short(parts[0]) or parts[0]
    return "-".join(parts)


def normalize_selection(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"maxHeight", "audioLanguage"}:
        raise ValueError("下载偏好必须只包含画质上限与音轨语言")
    height = value.get("maxHeight")
    if height is not None and (type(height) is not int or not 144 <= height <= 4320):
        raise ValueError("画质上限必须是 144–4320 之间的整数")
    language = value.get("audioLanguage", "auto")
    if not isinstance(language, str):
        raise ValueError("音轨语言必须是语言代码")
    language = language.strip().lower()
    if language != "auto":
        language = normalize_language(language)
    return {"maxHeight": height, "audioLanguage": language}


def observed_language(value):
    try:
        return normalize_language(value)
    except ValueError:
        return "und"


def choose_video(candidates, max_height, *, height, rank):
    if max_height is not None:
        candidates = [
            item
            for item in candidates
            if type(height(item)) in {int, float} and 0 < height(item) <= max_height
        ]
    if not candidates:
        raise ValueError("没有可确认满足画质上限的视频分辨率")
    return max(candidates, key=rank)


def choose_audio(candidates, requested, *, language, rank):
    if requested == "auto":
        return max(candidates, key=rank) if candidates else None
    exact = [item for item in candidates if observed_language(language(item)) == requested]
    if exact:
        return max(exact, key=rank)
    primary = requested.split("-")[0]
    compatible = []
    for item in candidates:
        available = observed_language(language(item))
        # A specific dialect never silently changes to another explicit dialect.
        # A base-language request may use a regional track; a regional request may
        # use a track labelled with only the base language, with a visible notice.
        if available.split("-")[0] == primary and ("-" not in requested or "-" not in available):
            compatible.append(item)
    if compatible:
        return max(compatible, key=rank)
    raise ValueError("当前画质没有符合指定语言的音轨")


def audio_metadata(language):
    if language in {"none", "und", "auto"}:
        return []
    primary = language.split("-")[0]
    iso = ISO639Utils.short2long(primary) if len(primary) == 2 else primary
    return ["-metadata:s:a:0", "language=" + iso] if iso else []

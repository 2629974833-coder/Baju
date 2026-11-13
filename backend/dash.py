"""Bounded, offline DASH interpretation using the installed yt-dlp parser.

The preflight only validates the supported subset and estimates expansion size;
yt-dlp owns inheritance, URL templates, timeline expansion and format metadata.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import parse_duration

from .selection import choose_audio, choose_video, normalize_selection

MAX_MANIFEST_BYTES = 2 * 1024**2
MAX_FRAGMENTS = 10000
MAX_REPRESENTATIONS = 64
MAX_DURATION = 7 * 24 * 3600


class _OfflineMPDParser(InfoExtractor):
    def __init__(self):
        # YoutubeDL construction loads global plugins even with auto_init=False.
        # This parser needs only parameters and diagnostic text formatting.
        super().__init__(
            SimpleNamespace(
                params={},
                Styles=SimpleNamespace(EMPHASIS=None),
                _format_err=lambda text, _style: text,
            )
        )

    def _request_webpage(self, *_args, **_kwargs):
        raise ValueError("DASH 解析器禁止直接访问网络")

    def report_warning(self, *_args, **_kwargs):
        raise ValueError("DASH 清单包含不支持的媒体描述")

    def _merge_mpd_periods(self, periods):
        # The SDK's general merger also merges distinct representations with
        # equal codec/bitrate metadata inside one Period, concatenating variants.
        # This supported subset has exactly one Period, so retain its formats.
        entries = list(periods)
        if len(entries) != 1:
            raise ValueError("仅支持单 Period DASH")
        return entries[0]["formats"], entries[0]["subtitles"]


def _integer(value, *, minimum=0):
    if not re.fullmatch(r"[0-9]{1,16}", value or ""):
        raise ValueError("DASH 数值无效或超出范围限制")
    result = int(value)
    if not minimum <= result <= 2**53 - 1:
        raise ValueError("DASH 数值无效或超出范围限制")
    return result


def _template(value):
    # The SDK uses Python %-formatting. Bound field widths before it allocates
    # even one expanded URL, and accept only the DASH-defined integer fields.
    for match in re.finditer(r"\$([^$]*)\$", value):
        token = match[1]
        if not token:
            continue
        parsed = re.fullmatch(
            r"(RepresentationID|Number|Bandwidth|Time)(?:%0?([0-9]{1,2})d)?", token
        )
        if not parsed or (parsed[2] and int(parsed[2]) > 32):
            raise ValueError("DASH URL 模板格式不支持或超过限制")
    if value.count("$") % 2:
        raise ValueError("DASH URL 模板无效")


def _template_length(value, representation_id):
    length = len(value)
    for match in re.finditer(r"\$([^$]*)\$", value):
        token = match[1].split("%", 1)[0]
        if token == "RepresentationID":
            width = len(representation_id)
        elif token:
            width = 32
        else:
            width = 1
        length += width - len(match[0])
    if length > 8192:
        raise ValueError("DASH URL 模板展开长度超过限制")


def _preflight(data):
    if not data or len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("DASH 清单大小超过限制")
    try:
        text = data.decode("utf-8-sig")
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise ValueError()
        root = ET.fromstring(text)
    except (UnicodeError, ET.ParseError, ValueError):
        raise ValueError("DASH XML 无效或包含不支持的声明") from None
    if root.tag not in {"MPD", "{urn:mpeg:dash:schema:mpd:2011}MPD"}:
        raise ValueError("响应不是支持的 DASH MPD 清单")
    if root.get("type", "static") != "static":
        raise ValueError("暂不支持动态或直播 DASH；仅支持 static 清单")
    namespace = root.tag[:-3]

    def children(element, name):
        return element.findall(namespace + name)

    def child(element, name):
        return element.find(namespace + name)

    count = 0
    pending = [(root, 0)]
    while pending:
        element, depth = pending.pop()
        count += 1
        if count > 20000 or depth > 24:
            raise ValueError("DASH XML 结构超过限制")
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name == "ContentProtection":
            raise ValueError("不支持 DRM DASH，不会绕过内容保护")
        if local_name == "SegmentBase" or any(
            name.rsplit("}", 1)[-1].lower().endswith("range") for name in element.attrib
        ):
            raise ValueError("不支持 DASH SegmentBase 或字节范围")
        if any("xlink" in name for name in element.attrib):
            raise ValueError("不支持 DASH 外部 XML 引用")
        if any(len(value) > 4096 for value in element.attrib.values()):
            raise ValueError("DASH 属性长度超过限制")
        if element.text and len(element.text) > 4096:
            raise ValueError("DASH 文本长度超过限制")
        if local_name == "SegmentTemplate":
            for name in ("media", "initialization"):
                if name in element.attrib:
                    _template(element.attrib[name])
        if local_name in {"Representation", "AdaptationSet"} and "$" in element.get("id", ""):
            raise ValueError("DASH 表示 ID 不能包含 URL 模板指令")
        if "bandwidth" in element.attrib:
            _integer(element.get("bandwidth"))
        pending.extend((node, depth + 1) for node in element)

    periods = children(root, "Period")
    if len(periods) != 1:
        raise ValueError("仅支持单 Period DASH")
    period = periods[0]
    duration = parse_duration(period.get("duration")) or parse_duration(
        root.get("mediaPresentationDuration")
    )
    if duration is not None and (not math.isfinite(duration) or not 0 < duration <= MAX_DURATION):
        raise ValueError("DASH 时长无效或超过限制")

    def segment_budget(element, parent):
        state = dict(parent)
        segment = child(element, "SegmentList")
        if segment is None:
            segment = child(element, "SegmentTemplate")
        if segment is None:
            return state
        for name in ("timescale", "duration", "startNumber"):
            if name in segment.attrib:
                state[name] = _integer(segment.get(name), minimum=0 if name == "startNumber" else 1)
        timeline = child(segment, "SegmentTimeline")
        if timeline is not None:
            entries = children(timeline, "S")
            if not entries:
                raise ValueError("DASH 时间轴为空")
            total = 0
            for entry in entries:
                _integer(entry.get("d"), minimum=1)
                if entry.get("t") is not None:
                    _integer(entry.get("t"))
                total += _integer(entry.get("r", "0")) + 1
                if total > MAX_FRAGMENTS:
                    raise ValueError("DASH 片段数量超过限制")
            state["timeline"] = total
        urls = children(segment, "SegmentURL")
        if urls:
            if any(not url.get("media") for url in urls):
                raise ValueError("DASH 片段地址缺失")
            state["urls"] = len(urls)
        if segment.get("media"):
            state["template"] = segment.get("media")
        if segment.get("initialization"):
            state["initialization"] = segment.get("initialization")
        return state

    period_state = segment_budget(period, {"timescale": 1})
    total, representations = 0, 0
    for adaptation in children(period, "AdaptationSet"):
        adaptation_state = segment_budget(adaptation, period_state)
        for representation in children(adaptation, "Representation"):
            representations += 1
            if representations > MAX_REPRESENTATIONS:
                raise ValueError("DASH 表示数量超过限制")
            state = segment_budget(representation, adaptation_state)
            representation_id = representation.get("id", adaptation.get("id", ""))
            for name in ("template", "initialization"):
                if name in state:
                    _template_length(state[name], representation_id)
            if "urls" in state:
                segments = state["urls"]
                if "timeline" in state and state["timeline"] != segments:
                    raise ValueError("DASH 时间轴与片段数量不一致")
            elif "template" in state and "timeline" in state:
                segments = state["timeline"]
            elif "template" in state and "duration" in state and duration:
                segments = math.ceil(duration * state["timescale"] / state["duration"])
            else:
                raise ValueError("仅支持有界 SegmentList 或 SegmentTemplate DASH")
            total += segments + 1  # Include a possible initialization object.
            if not segments or total > MAX_FRAGMENTS:
                raise ValueError("DASH 片段展开数量超过限制")
    if not representations:
        raise ValueError("DASH 清单缺少媒体表示")
    return root


def list_mpd_formats(data: bytes, manifest_url: str) -> list[dict]:
    """Return bounded video/audio representations with absolute fragments; no I/O."""
    root = _preflight(data)
    try:
        formats, _subtitles = _OfflineMPDParser()._parse_mpd_formats_and_subtitles(
            root, mpd_url=manifest_url, mpd_base_url=urljoin(manifest_url, ".")
        )
    except Exception:
        raise ValueError("DASH 清单格式无法解析或不受支持") from None
    if any(format_.get("has_drm") for format_ in formats):
        raise ValueError("不支持 DRM DASH")
    tracks = []
    for format_ in formats:
        kind = next(
            (
                kind
                for kind in ("video", "audio")
                if format_.get("format_note", "").startswith(f"DASH {kind}")
            ),
            None,
        )
        if kind is None:
            continue
        selected = dict(format_)
        if selected.get("protocol") != "http_dash_segments" or not selected.get("fragments"):
            raise ValueError("DASH 未提供支持的完整片段列表")
        fragments = []
        for fragment in selected["fragments"]:
            if any("range" in name.lower() for name in fragment):
                raise ValueError("不支持 DASH 字节范围")
            address = fragment.get("url") or fragment.get("path")
            if not isinstance(address, str) or not address:
                raise ValueError("DASH 片段地址缺失")
            url = urljoin(selected.get("fragment_base_url") or manifest_url, address)
            parsed = urlsplit(url)
            if (
                len(url) > 8192
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or any(ord(char) < 32 for char in url)
            ):
                raise ValueError("DASH 片段 URL 无效或不受支持")
            fragments.append({**fragment, "url": url})
        selected.update(fragments=fragments, _dash_kind=kind)
        tracks.append(selected)
    return tracks


def parse_mpd(data: bytes, manifest_url: str, selection=None) -> list[dict]:
    """Select one video and optional audio using a validated, frozen preference."""
    options = normalize_selection(selection)
    formats = list_mpd_formats(data, manifest_url)
    video = [item for item in formats if item["_dash_kind"] == "video"]
    if not video:
        raise ValueError("DASH 清单缺少视频轨道")
    selected = choose_video(
        video,
        options["maxHeight"],
        height=lambda item: item.get("height"),
        rank=lambda item: (item.get("height") or 0, item.get("tbr") or 0),
    )
    audio = choose_audio(
        [item for item in formats if item["_dash_kind"] == "audio"],
        options["audioLanguage"],
        language=lambda item: item.get("language"),
        rank=lambda item: item.get("tbr") or 0,
    )
    return [selected, audio] if audio else [selected]

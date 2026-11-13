"""CPU processing of local originals into independently verified, immutable versions.

Text is rasterized by Pillow; no user string enters a shell or FFmpeg filter expression.
Watermark removal interpolates surrounding pixels within a fixed or moving rectangle.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import warnings
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from backend.downloader import DownloadCancelled, Downloader, DownloadPaused

_POSITIONS = {"top-left", "top-right", "bottom-left", "bottom-right"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_FORMATS = "mov,matroska,webm,avi,mpegts,mpeg,ogg,flv,asf"
_DEFAULTS = {
    "mode": "add",
    "text": "FRAMEKEEP",
    "position": "bottom-right",
    "opacity": 65,
    "fontSize": 4,
    "region": None,
}
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


def _number(value, minimum, maximum, name):
    if (
        type(value) not in (int, float)
        or not minimum <= value <= maximum
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name}超出允许范围")
    return value


def _normalize_motion(points, region):
    if not isinstance(points, list) or not 1 <= len(points) <= 32:
        raise ValueError("移动选区需要 1–32 个位置关键帧")
    normalized = []
    for point in points:
        if not isinstance(point, dict) or set(point) != {"time", "x", "y", "transition"}:
            raise ValueError("关键帧需要提供时间、位置和移动方式")
        seconds = round(_number(point["time"], 0, sys.float_info.max, "关键帧时间"), 6)
        if normalized and seconds <= normalized[-1]["time"]:
            raise ValueError("关键帧时间必须递增，不能重复")
        x = _number(point["x"], 0, 100, "关键帧水平位置")
        y = _number(point["y"], 0, 100, "关键帧垂直位置")
        if x + region["width"] > 100 or y + region["height"] > 100:
            raise ValueError("关键帧的修复区域超出画面")
        if point["transition"] not in ("linear", "jump"):
            raise ValueError("关键帧的移动方式必须为平移或跳转")
        normalized.append({"time": seconds, "x": x, "y": y, "transition": point["transition"]})
    return normalized


def normalize_processing_options(value=None):
    if value is None:
        value = {}
    if isinstance(value, dict) and "method" in value:
        glyph = {"mode": "remove", "method": "chunlou-glyph"}
        if value == glyph:
            return dict(value)
        if type(value.get("revision")) is int and value == {**glyph, "revision": 2}:
            return dict(value)
        raise ValueError("春楼飘字修复是独立的视频处理方式，不接受手动选区等参数")
    if not isinstance(value, dict) or set(value) - _DEFAULTS.keys() - {"motion"}:
        raise ValueError("处理参数包含未知字段")
    result = {**_DEFAULTS, **value}
    if not isinstance(result["mode"], str) or result["mode"] not in {"add", "remove", "both"}:
        raise ValueError("处理方式必须为加水印、固定区域去水印或二者组合")
    if not isinstance(result["position"], str) or result["position"] not in _POSITIONS:
        raise ValueError("水印位置无效")
    text = result["text"]
    if (
        not isinstance(text, str)
        or len(text) > 80
        or (result["mode"] != "remove" and not text.strip())
    ):
        raise ValueError("水印文字须为 1–80 个字符")
    if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in text):
        raise ValueError("水印文字包含不支持的控制字符")
    result["opacity"] = _number(result["opacity"], 0, 100, "不透明度")
    result["fontSize"] = _number(result["fontSize"], 1, 20, "文字高度百分比")
    region = result["region"]
    if region is not None:
        if not isinstance(region, dict) or set(region) != {"x", "y", "width", "height"}:
            raise ValueError("修复区域必须提供 x、y、width、height 百分比")
        region = {key: _number(value, 0, 100, "修复区域") for key, value in region.items()}
        if region["width"] <= 0 or region["height"] <= 0:
            raise ValueError("修复区域宽高必须大于零")
        if region["x"] + region["width"] > 100 or region["y"] + region["height"] > 100:
            raise ValueError("修复区域超出画面")
        result["region"] = region
    if result["mode"] in {"remove", "both"} and region is None:
        raise ValueError("固定区域去水印需要指定修复区域")
    if "motion" in value:
        if result["mode"] == "add":
            raise ValueError("移动选区仅适用于去水印")
        result["motion"] = _normalize_motion(value["motion"], region)
    return result


def _check_control(control):
    if control.cancelled:
        raise DownloadCancelled()
    if control.paused:
        raise DownloadPaused()


def _sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _safe_child(root, path):
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError("媒体路径无效") from None
    current = root
    for part in relative.parts:
        current = current / part
        if part in {".", ".."} or current.is_symlink():
            raise ValueError("媒体路径不能包含链接或上级目录")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("媒体路径无效")
    return path


def _rectangle(options, width, height):
    region = options["region"]
    x, y = round(width * region["x"] / 100), round(height * region["y"] / 100)
    right = round(width * (region["x"] + region["width"]) / 100)
    bottom = round(height * (region["y"] + region["height"]) / 100)
    if x < 1 or y < 1 or right >= width or bottom >= height or right - x < 2 or bottom - y < 2:
        raise ValueError("修复区域须在画面内至少保留 1 像素边界，且宽高至少 2 像素")
    return x, y, right, bottom


def _repair_image(image, rectangle):
    x, y, right, bottom = rectangle
    size = (right - x, bottom - y)
    left = image.crop((x - 1, y, x, bottom)).resize(size)
    right_edge = image.crop((right, y, right + 1, bottom)).resize(size)
    top = image.crop((x, y - 1, right, y)).resize(size)
    bottom_edge = image.crop((x, bottom, right, bottom + 1)).resize(size)
    vertical = Image.linear_gradient("L").resize(size)
    horizontal = Image.linear_gradient("L").rotate(270).resize(size)
    across = Image.composite(right_edge, left, horizontal)
    down = Image.composite(bottom_edge, top, vertical)
    image.paste(Image.blend(across, down, 0.5), (x, y))


def _coordinate_expression(points, axis, dimension, box_size):
    # delogo samples the pixels just outside its box. Preserve that one-pixel border.
    coords = [max(1, min(dimension - box_size - 1, round(p[axis] * dimension / 100)))
              for p in points]
    terms = [str(coords[0])]
    for i in range(1, len(points)):
        delta = coords[i] - coords[i - 1]
        if not delta:
            continue
        start, end = points[i - 1]["time"], points[i]["time"]
        if points[i]["transition"] == "jump":
            amount = f"gte(t,{end})"
        else:
            # Sum bounded increments to avoid building nested if() trees or extrapolating.
            amount = f"clip((t-{start})/({end}-{start}),0,1)"
        terms.append(f"{delta}*{amount}")
    return "round(" + "+".join(terms) + ")"


def _removal_filter(options, width, height, duration):
    points = options.get("motion")
    if not points:
        x, y, right, bottom = _rectangle(options, width, height)
        return f"delogo=x={x}:y={y}:w={right - x}:h={bottom - y}:show=0"
    if points[-1]["time"] >= duration:
        raise ValueError("最后一个位置关键帧必须在视频片尾之前")
    region = options["region"]
    w = min(width - 2, round(width * region["width"] / 100))
    h = min(height - 2, round(height * region["height"] / 100))
    if w < 2 or h < 2:
        raise ValueError("移动选区在视频上的宽高均须至少 2 像素")
    x = _coordinate_expression(points, "x", width, w)
    y = _coordinate_expression(points, "y", height, h)
    return f"delogo=x='{x}':y='{y}':w={w}:h={h}:show=0:enable='gte(t,{points[0]['time']})'"


class Processor:
    def __init__(
        self,
        data_dir,
        *,
        storage_guard=None,
        font_path=None,
        max_pixels=40_000_000,
        max_seconds=21600,
        max_output_bytes=2 * 1024**3,
    ):
        self.data_dir = Path(data_dir).resolve()
        self.storage_guard = storage_guard
        self.font_path = font_path or os.getenv("FRAMEKEEP_FONT_PATH")
        self.max_pixels, self.max_seconds = max_pixels, max_seconds
        self.max_output_bytes = max_output_bytes

    def _space(self, required_bytes=0):
        if self.storage_guard:
            self.storage_guard.check(required_bytes=required_bytes)

    def _font(self, text, size):
        candidates = [str(self.font_path)] if self.font_path else list(_FONT_CANDIDATES)
        if not self.font_path and text.isascii():
            candidates.extend(("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"))
        for candidate in candidates:
            try:
                font = ImageFont.truetype(candidate, size=size)
                missing = font.getmask("\U0010ffff")
                missing_key = (missing.size, bytes(missing))
                for char in set(text) - {" "}:
                    mask = font.getmask(char)
                    if (mask.size, bytes(mask)) == missing_key:
                        raise ValueError("字体未包含所需文字")
                return font
            except (OSError, ValueError):
                continue
        raise ValueError("缺少支持当前文字的字体，请配置 FRAMEKEEP_FONT_PATH 或安装 Noto CJK 字体")

    def _watermark(self, width, height, options):
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        margin = max(1, round(min(width, height) * 0.025))
        size = max(1, round(height * options["fontSize"] / 100))
        font = self._font(options["text"], size)
        stroke = max(1, size // 18)
        while True:
            bounds = draw.textbbox((0, 0), options["text"], font=font, stroke_width=stroke)
            text_width, text_height = bounds[2] - bounds[0], bounds[3] - bounds[1]
            if text_width <= width - 2 * margin and text_height <= height - 2 * margin:
                break
            size -= 1
            if size < 1:
                raise ValueError("水印文字过长，无法放入画面")
            font = font.font_variant(size=size)
            stroke = max(1, size // 18)
        x = margin if options["position"].endswith("left") else width - margin - text_width
        y = margin if options["position"].startswith("top") else height - margin - text_height
        opacity = round(options["opacity"] * 255 / 100)
        draw.text(
            (x - bounds[0], y - bounds[1]),
            options["text"],
            font=font,
            fill=(255, 255, 255, opacity),
            stroke_width=stroke,
            stroke_fill=(0, 0, 0, opacity),
        )
        return canvas

    def _inspect_dimensions(self, width, height):
        if width <= 0 or height <= 0 or width * height > self.max_pixels:
            raise ValueError("媒体像素数超出处理上限")

    def _image(self, source, destination, options, control):
        _check_control(control)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(source) as opened:
                    self._inspect_dimensions(*opened.size)
                    if getattr(opened, "n_frames", 1) != 1:
                        raise ValueError("暂不处理动画或多帧图片，以避免丢失画面")
                    image = ImageOps.exif_transpose(opened).convert("RGBA")
            if options["mode"] in {"remove", "both"}:
                _repair_image(image, _rectangle(options, *image.size))
            _check_control(control)
            if options["mode"] in {"add", "both"}:
                image = Image.alpha_composite(image, self._watermark(*image.size, options))
            _check_control(control)
            self._space()
            image.save(destination, format="PNG")
            return {"width": image.width, "height": image.height, "mime": "image/png"}
        except (
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            raise ValueError("图片内容无效或像素数超出处理上限") from None

    async def _image_process(
        self, source, destination, options, control, work, *, overlay_size=None
    ):
        job = work / "image-job.json"
        job.write_text(
            json.dumps(
                {
                    "source": str(source),
                    "destination": str(destination),
                    "options": options,
                    "dataDir": str(self.data_dir),
                    "fontPath": str(self.font_path) if self.font_path else None,
                    "maxPixels": self.max_pixels,
                    "overlaySize": overlay_size,
                },
                ensure_ascii=True,
            )
        )
        job.chmod(0o600)
        output = await self._run(
            [sys.executable, "-m", "backend.processing", "--image-worker", str(job)],
            control,
            timeout=300,
            output_path=destination,
        )
        try:
            result = json.loads(output)
        except (ValueError, TypeError):
            raise ValueError("图片处理未返回有效结果") from None
        if result.get("error"):
            raise ValueError(result["error"])
        return result

    async def _run(
        self, command, control, *, timeout, on_progress=None, duration=None, output_path=None
    ):
        _check_control(control)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            cwd=Path(__file__).resolve().parents[1],
        )
        output = bytearray()
        progress = {}

        async def read_output():
            while line := await process.stdout.readline():
                if duration and line.startswith(b"out_time_us="):
                    with contextlib.suppress(ValueError):
                        progress["seconds"] = max(0, int(line.split(b"=", 1)[1]) / 1_000_000)
                    continue
                if len(output) < 1024 * 1024:
                    output.extend(line[: 1024 * 1024 - len(output)])

        async def drain_errors():
            while await process.stderr.read(65536):
                pass

        readers = [asyncio.create_task(read_output()), asyncio.create_task(drain_errors())]
        waiter = asyncio.create_task(process.wait())
        started, last_report = time.monotonic(), 0
        try:
            while process.returncode is None:
                _check_control(control)
                self._space()
                if (
                    output_path
                    and output_path.exists()
                    and output_path.stat().st_size > self.max_output_bytes
                ):
                    raise ValueError("处理输出超过文件大小上限")
                elapsed = time.monotonic() - started
                if elapsed > timeout:
                    raise ValueError("媒体处理超时，原件仍保留")
                if (
                    on_progress
                    and duration
                    and "seconds" in progress
                    and elapsed - last_report >= 0.2
                ):
                    await on_progress(
                        {
                            "phase": "processing",
                            "progress": min(94, 5 + progress["seconds"] * 89 / duration),
                            "processedSeconds": progress["seconds"],
                        }
                    )
                    last_report = elapsed
                await asyncio.wait({waiter}, timeout=0.1)
            await asyncio.gather(*readers)
            _check_control(control)
            if process.returncode:
                raise ValueError("媒体处理或校验失败，源格式可能不受支持或文件损坏")
            return bytes(output)
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            for task in readers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            await waiter

    async def _probe(self, path, control):
        result = await self._run(
            [
                Downloader._binary("ffprobe"),
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                _FORMATS,
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            control,
            timeout=30,
        )
        try:
            info = json.loads(result)
            videos = [
                stream
                for stream in info["streams"]
                if stream.get("codec_type") == "video"
                and not stream.get("disposition", {}).get("attached_pic")
            ]
            if len(videos) != 1:
                raise ValueError()
            video = videos[0]
            width, height = int(video["width"]), int(video["height"])
            duration = float(info["format"].get("duration") or video.get("duration") or 0)
            if duration <= 0 or not math.isfinite(duration) or duration > self.max_seconds:
                raise ValueError()
            self._inspect_dimensions(width, height)
            rotation = 0
            for item in video.get("side_data_list", []):
                if "rotation" in item:
                    rotation = round(float(item["rotation"])) % 360
            if rotation not in {0, 90, 180, 270}:
                raise ValueError()
            if rotation in {90, 270}:
                width, height = height, width
            audio = [stream for stream in info["streams"] if stream.get("codec_type") == "audio"]
            if len(audio) > 16 or any(not stream.get("codec_name") for stream in [video, *audio]):
                raise ValueError()
            return {
                "width": width,
                "height": height,
                "duration": duration,
                "audioTracks": len(audio),
                "audio": [self._audio_info(stream) for stream in audio],
                "rotation": rotation,
                "frameRate": video.get("avg_frame_rate") or "0/0",
                "videoOffset": float(video.get("start_time") or 0) - float(info["format"].get("start_time") or 0),
            }
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise ValueError("视频无有效单一视频轨道、时长或超出处理资源范围") from None

    @staticmethod
    def _audio_info(stream):
        value = stream.get("duration")
        if value is None and stream.get("tags", {}).get("DURATION"):
            parts = stream["tags"]["DURATION"].split(":")
            if len(parts) == 3:
                value = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        duration = float(value) if value is not None else None
        if duration is not None and (not math.isfinite(duration) or duration <= 0):
            raise ValueError("音轨时长无效")
        return {
            "duration": duration,
            "channels": int(stream["channels"]),
            "language": str(stream.get("tags", {}).get("language") or "und")[:35],
        }

    @staticmethod
    def _verify_audio(before, after):
        for source, output in zip(before["audio"], after["audio"], strict=True):
            if source["channels"] != output["channels"] or source["language"] != output["language"]:
                raise ValueError("处理后音轨的声道或语言不一致")
            duration = source["duration"]
            if duration is not None and (
                output["duration"] is None
                or abs(duration - output["duration"]) > max(0.25, min(2, duration * 0.001))
            ):
                raise ValueError("处理后音轨时长不一致")

    async def _glyph_video(self, source, destination, before, control, on_progress, revision):
        try:
            rate = Fraction(before["frameRate"])
        except (ValueError, ZeroDivisionError):
            rate = 0
        offset = before["videoOffset"]
        if not 1 <= rate <= 120 or not math.isfinite(offset) or not 0 <= offset < before["duration"]:
            raise ValueError("该视频的时间轴暂不支持自动飘字修复，可使用手动选区")
        if before["width"] * before["height"] > 3840 * 2160:
            raise ValueError("自动飘字修复支持最高 4K 的视频")
        output = await self._run(
            [sys.executable, "-m", "backend.glyph_worker", str(source), str(destination),
             str(before["width"]), str(before["height"]), str(rate), str(offset), str(revision)],
            control, timeout=min(24 * 3600, max(120, before["duration"] * 15)),
            on_progress=on_progress, duration=before["duration"], output_path=destination,
        )
        report = json.loads(output.splitlines()[-1])
        if report.get("error"):
            raise ValueError(report["error"])
        if not report["glyphFrames"]:
            raise ValueError("未识别到匹配的春楼飘字，没有生成成品；可在工作室改用手动选区")
        return {**before, **report}

    async def _video(self, source, destination, options, control, on_progress, work):
        before = await self._probe(source, control)
        width, height = before["width"], before["height"]
        if options.get("method") == "chunlou-glyph":
            return await self._glyph_video(
                source, destination, before, control, on_progress, options.get("revision", 1)
            )
        filters = []
        if options["mode"] in {"remove", "both"}:
            filters.append(_removal_filter(options, width, height, before["duration"]))
        command = [
            Downloader._binary("ffmpeg"),
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-xerror",
            "-err_detect",
            "explode",
            "-threads",
            "2",
            "-filter_complex_threads",
            "1",
            "-progress",
            "pipe:1",
            "-nostats",
            "-stats_period",
            "0.5",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            _FORMATS,
            "-i",
            str(source),
        ]
        base = ",".join(filters or ["null"])
        if options["mode"] in {"add", "both"}:
            overlay = work / "watermark.png"
            await self._image_process(
                source, overlay, options, control, work, overlay_size=(width, height)
            )
            command.extend(["-protocol_whitelist", "file,pipe", "-i", str(overlay)])
            graph = f"[0:v:0]{base}[base];[base][1:v:0]overlay=0:0:format=auto:repeatlast=1[out]"
            graph += ";[out]pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p[video]"
        else:
            graph = f"[0:v:0]{base},pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p[video]"
        command.extend(
            [
                "-filter_complex",
                graph,
                "-map",
                "[video]",
                "-map",
                "0:a?",
                "-map_metadata",
                "0",
                "-metadata:s:v:0",
                "rotate=0",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-threads",
                "2",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(destination),
            ]
        )
        timeout = min(24 * 3600, max(120, before["duration"] * 8))
        await self._run(
            command,
            control,
            timeout=timeout,
            on_progress=on_progress,
            duration=before["duration"],
            output_path=destination,
        )
        return before

    async def process(self, asset, version_id, options, control, on_progress):
        options = normalize_processing_options(options)
        identifier = asset.get("id", "")
        if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
            raise ValueError("素材 ID 无效")
        if not isinstance(version_id, str) or not _IDENTIFIER.fullmatch(version_id):
            raise ValueError("版本 ID 无效")
        if asset.get("kind") not in {"image", "video"}:
            raise ValueError("仅支持图片和视频处理")
        if options.get("method") == "chunlou-glyph" and (
            asset["kind"] != "video" or asset.get("siteId") != "chunlou"
        ):
            raise ValueError("自动飘字修复仅适用于春楼视频")
        if "motion" in options and asset["kind"] != "video":
            raise ValueError("移动选区关键帧只能用于视频")
        relative = asset.get("_path", "")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ValueError("找不到已下载原件")
        folder = _safe_child(self.data_dir, self.data_dir / "assets" / identifier)
        source = _safe_child(folder, self.data_dir / relative)
        if not source.is_file():
            raise ValueError("找不到已下载原件")
        _check_control(control)
        self._space(max(1024 * 1024, source.stat().st_size * 3))
        source_hash = await asyncio.to_thread(_sha256, source)
        await on_progress({"phase": "preparing", "progress": 0})
        destination_folder = _safe_child(folder, folder / "versions" / version_id)
        extension = ".png" if asset["kind"] == "image" else ".mp4"
        destination = destination_folder / ("processed" + extension)
        if destination.exists():
            raise ValueError("该处理版本已存在，不能覆盖")
        destination_folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination_folder.chmod(0o700)
        work = Path(tempfile.mkdtemp(prefix=".work-", dir=destination_folder))
        temporary = work / ("processed" + extension)
        published = False
        try:
            _check_control(control)
            await on_progress({"phase": "processing", "progress": 5})
            if asset["kind"] == "image":
                result = await self._image_process(source, temporary, options, control, work)
            else:
                result = await self._video(source, temporary, options, control, on_progress, work)
            await on_progress({"phase": "verifying", "progress": 95})
            _check_control(control)
            self._space()
            if asset["kind"] == "image":
                verified, _extension = await asyncio.to_thread(Downloader._inspect_image, temporary)
                if (verified["width"], verified["height"]) != (result["width"], result["height"]):
                    raise ValueError("处理后的图片尺寸不一致")
            else:
                verified = await self._probe(temporary, control)
                dimensions = (
                    result["width"] + result["width"] % 2,
                    result["height"] + result["height"] % 2,
                )
                if (
                    (verified["width"], verified["height"]) != dimensions
                    or verified["audioTracks"] != result["audioTracks"]
                    or abs(verified["duration"] - result["duration"])
                    > max(0.25, result["duration"] * 0.005)
                ):
                    raise ValueError("处理后的视频尺寸、音轨或时长不一致")
                self._verify_audio(result, verified)
                verified["mime"] = "video/mp4"
            size = temporary.stat().st_size
            if size > self.max_output_bytes:
                raise ValueError("处理输出超过文件大小上限")
            digest = await asyncio.to_thread(_sha256, temporary)
            if await asyncio.to_thread(_sha256, source) != source_hash:
                raise ValueError("处理期间原件发生变化，未保存该版本")
            _check_control(control)
            self._space()
            if destination.exists():
                raise ValueError("该处理版本已存在，不能覆盖")
            os.replace(temporary, destination)
            destination.chmod(0o600)
            published = True
            result = {
                **verified,
                **{key: result[key] for key in ("glyphFrames", "totalFrames") if key in result},
                "path": destination.relative_to(self.data_dir).as_posix(),
                "size": size,
                "sha256": digest,
                "sourceSha256": source_hash,
                "options": options,
                "processor": "pillow" if asset["kind"] == "image" else "ffmpeg",
            }
            if options["mode"] in {"remove", "both"}:
                result["removalMethod"] = (
                    "edge-interpolation" if asset["kind"] == "image" else "delogo"
                )
                if options.get("method") == "chunlou-glyph":
                    result["removalMethod"] = "chunlou-glyph"
                elif options.get("motion"):
                    result["removalMethod"] = "delogo-keyframes"
            await on_progress({"phase": "completed", "progress": 100})
            return result
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if not published:
                with contextlib.suppress(OSError):
                    destination_folder.rmdir()


def _image_worker(job_path):
    """Private subprocess entry: the parent owns validation, cancellation and cleanup."""
    try:
        job = json.loads(Path(job_path).read_text())
        processor = Processor(
            job["dataDir"], font_path=job["fontPath"], max_pixels=job["maxPixels"]
        )
        options = normalize_processing_options(job["options"])
        if job.get("overlaySize"):
            width, height = job["overlaySize"]
            processor._inspect_dimensions(width, height)
            processor._watermark(width, height, options).save(job["destination"], format="PNG")
            result = {"width": width, "height": height, "mime": "image/png"}
        else:
            result = processor._image(
                Path(job["source"]),
                Path(job["destination"]),
                options,
                SimpleNamespace(paused=False, cancelled=False),
            )
    except ValueError as error:
        result = {"error": str(error)[:400]}
    except OSError:
        result = {"error": "图片无法读取或写入，原件仍保留"}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--image-worker":
    _image_worker(sys.argv[2])

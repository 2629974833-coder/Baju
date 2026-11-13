"""Download original Instagram video/audio files through the guarded transport."""

import json
import math
import shutil
from urllib.parse import urlsplit

from backend.selection import audio_metadata, choose_audio, normalize_selection, observed_language


def _track_url(url):
    if not isinstance(url, str) or len(url) > 16384 or any(ord(c) < 32 or ord(c) == 127 for c in url):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and bool(parsed.hostname) and parsed.port in {None, 443}
                and parsed.username is None and parsed.password is None and not parsed.fragment)
    except ValueError:
        return False


async def verify_instagram_media(downloader, path, expected):
    output = await downloader._process([
        downloader._binary("ffprobe"), "-v", "error", "-protocol_whitelist", "file,pipe",
        "-format_whitelist", "mov", "-show_format", "-show_streams", "-of", "json", str(path),
    ], timeout=30)
    try:
        info = json.loads(output)
        video = next(s for s in info["streams"] if s.get("codec_type") == "video"
                     and not s.get("disposition", {}).get("attached_pic"))
        audio = next(s for s in info["streams"] if s.get("codec_type") == "audio")
        duration = float(info["format"]["duration"])
        if (video.get("codec_name") != "h264" or audio.get("codec_name") != "aac"
                or not math.isfinite(duration) or duration <= 0):
            raise ValueError()
        if expected and abs(duration - expected) > 1.25:
            raise ValueError()
        for stream in (video, audio):
            length = float(stream.get("duration") or duration)
            if not math.isfinite(length) or length <= 0 or abs(length - duration) > 1.25:
                raise ValueError()
    except (ValueError, KeyError, TypeError, StopIteration):
        raise ValueError("Instagram 成片缺少完整原始音视频或时长不符，原件未替换") from None


async def download_instagram(downloader, transfer, partial, cache):
    """Resume original tracks and mux locally; never generate replacement audio."""
    transfer.check()
    asset = transfer.asset
    audio = asset.get("_instagramAudio")
    if not isinstance(audio, dict) or not _track_url(audio.get("url")) or not _track_url(asset.get("sourceUrl")):
        raise ValueError("Instagram 原始音视频来源不完整，请重新扫描")
    duration = asset.get("_instagramDuration")
    expected = duration if type(duration) in {int, float} and math.isfinite(duration) and duration > 0 else None
    options = normalize_selection(asset.get("_downloadOptions"))
    language = observed_language(audio.get("language"))
    choose_audio([audio], options["audioLanguage"], language=lambda _: language, rank=lambda _: 0)
    height = asset.get("height")
    if options["maxHeight"] and (type(height) not in {int, float} or not 0 < height <= options["maxHeight"]):
        raise ValueError("当前 Instagram 原始视频不符合指定画质上限")
    tracks = [asset["sourceUrl"], audio["url"]]
    for url in tracks:
        transfer.check()
        await downloader.policy.validate(url)
    await transfer.progress(selection=downloader._selection_info("instagram", options, height, language))
    partial.unlink(missing_ok=True)
    if cache.is_symlink():
        cache.unlink()
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    objects = cache / "instagram"
    if objects.is_symlink():
        objects.unlink()
    objects.mkdir(mode=0o700, exist_ok=True)
    selection = downloader._identity(asset.get("siteId"), asset.get("pageUrl"), tracks, options)
    keys = [downloader._identity(selection, index, url) for index, url in enumerate(tracks)]
    for path in objects.iterdir():
        if path.stem not in keys:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    files = []
    try:
        for url, key in zip(tracks, keys, strict=True):
            transfer.check()
            path = objects / (key + ".mp4")
            transfer.asset = {**asset, "sourceUrl": url}
            await downloader._fetch(url, path, transfer, checkpoint=objects / (key + ".json"),
                                    identity=key, reuse_complete=True, bindings={"selection": selection})
            files.append(path)
        await transfer.progress(phase="muxing", retryInSeconds=0)
        command = [downloader._binary("ffmpeg"), "-nostdin", "-v", "error"]
        for path in files:
            command += ["-protocol_whitelist", "file,pipe", "-format_whitelist", "mov", "-i", str(path)]
        command += ["-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart",
                    *audio_metadata(language), "-f", "mp4", str(partial)]
        await downloader._process(command)
        transfer.check()
        await verify_instagram_media(downloader, partial, expected)
        return expected
    finally:
        transfer.asset = asset

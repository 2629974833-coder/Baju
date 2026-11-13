"""Real CPU processing on neutral, locally generated media only."""

import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops, ImageDraw

from backend.downloader import DownloadCancelled, DownloadPaused
from backend.processing import Processor, normalize_processing_options


def controls():
    return SimpleNamespace(paused=False, cancelled=False)


def asset_image(root, *, size=(240, 160), orientation=None):
    folder = root / "assets" / "neutral_image"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "original.png"
    image = Image.new("RGB", size, (36, 96, 144))
    if orientation:
        exif = Image.Exif()
        exif[274] = orientation
        image.save(path, exif=exif)
    else:
        image.save(path)
    return {"id": folder.name, "kind": "image", "_path": str(path.relative_to(root))}


def ffmpeg(*args):
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
        check=True,
        capture_output=True,
        timeout=30,
    )


def asset_video(root, *, rotation=0):
    folder = root / "assets" / "neutral_video"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "original.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=0x246090:size=160x90:rate=10:duration=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=400:duration=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=700:duration=2",
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-map",
        "2:a",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-metadata:s:a:0",
        "language=eng",
        "-metadata:s:a:1",
        "language=zho",
        "-shortest",
        path,
    )
    if rotation:
        rotated = folder / "rotated.mp4"
        ffmpeg("-display_rotation", str(rotation), "-i", path, "-map", "0", "-c", "copy", rotated)
        rotated.replace(path)
    return {"id": folder.name, "kind": "video", "_path": str(path.relative_to(root))}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def process(
    root, asset, options, *, version="version_one", control=None, callback=None, **kw
):
    events = []

    async def progress(value):
        events.append(value)
        if callback:
            await callback(value)

    result = await Processor(root, **kw).process(
        asset, version, options, control or controls(), progress
    )
    return result, events


def test_processing_options_are_bounded_and_compatible_with_ui():
    assert normalize_processing_options({}) == {
        "mode": "add",
        "text": "FRAMEKEEP",
        "position": "bottom-right",
        "opacity": 65,
        "fontSize": 4,
        "region": None,
    }
    value = normalize_processing_options(
        {"mode": "both", "region": {"x": 70, "y": 80, "width": 25, "height": 15}}
    )
    assert value["region"]["width"] == 25


@pytest.mark.parametrize(
    "options",
    [
        {"mode": "upscale"},
        {"text": ""},
        {"text": "A\x00B"},
        {"text": "bad\ud800"},
        {"text": "A" * 81},
        {"opacity": 101},
        {"opacity": True},
        {"opacity": float("nan")},
        {"fontSize": 0},
        {"position": "shell"},
        {"mode": "remove"},
        {"mode": "remove", "region": {"x": 90, "y": 10, "width": 20, "height": 10}},
        {"mode": "remove", "region": {"x": 10, "y": 10, "width": 0, "height": 10}},
        {"mode": "remove", "region": {"x": 10, "y": 10, "width": 10, "height": float("inf")}},
        {"unexpected": 1},
    ],
)
def test_processing_options_reject_unsafe_or_ambiguous_values(options):
    with pytest.raises(ValueError):
        normalize_processing_options(options)


@pytest.mark.asyncio
async def test_image_watermark_is_real_original_preserved_and_version_is_atomic(tmp_path):
    asset = asset_image(tmp_path)
    source = tmp_path / asset["_path"]
    original = digest(source)
    result, events = await process(
        tmp_path, asset, {"text": "Test 课程", "fontSize": 10, "opacity": 100}
    )
    destination = tmp_path / result["path"]
    assert destination.is_relative_to(
        tmp_path / "assets" / asset["id"] / "versions" / "version_one"
    )
    assert digest(source) == original == result["sourceSha256"]
    assert digest(destination) == result["sha256"] != original
    assert (result["width"], result["height"], result["mime"]) == (240, 160, "image/png")
    with Image.open(source) as before, Image.open(destination) as after:
        bounds = ImageChops.difference(before.convert("RGB"), after.convert("RGB")).getbbox()
        assert bounds and bounds[1] > 80
    assert events[-1]["progress"] == 100
    assert not list(destination.parent.glob(".work-*"))
    with pytest.raises(ValueError):
        await process(tmp_path, asset, {"text": "Other"})
    assert digest(destination) == result["sha256"]


@pytest.mark.asyncio
async def test_image_region_removal_changes_only_selected_rectangle(tmp_path):
    asset = asset_image(tmp_path)
    source = tmp_path / asset["_path"]
    with Image.open(source) as opened:
        before = opened.convert("RGBA")
    ImageDraw.Draw(before).rectangle((80, 50, 119, 79), fill=(255, 255, 255, 255))
    before.save(source)
    region = {"x": 30, "y": 25, "width": 25, "height": 30}
    result, _events = await process(tmp_path, asset, {"mode": "remove", "region": region})
    with Image.open(tmp_path / result["path"]) as after:
        bounds = ImageChops.difference(before.convert("RGB"), after.convert("RGB")).getbbox()
        assert bounds and bounds[0] >= 72 and bounds[2] <= 132
        assert after.getpixel((100, 65))[:3] == (36, 96, 144)
    assert result["removalMethod"] == "edge-interpolation"


@pytest.mark.asyncio
async def test_image_orientation_and_alpha_are_preserved(tmp_path):
    asset = asset_image(tmp_path, orientation=6)
    result, _events = await process(tmp_path, asset, {"opacity": 0})
    assert (result["width"], result["height"]) == (160, 240)
    with Image.open(tmp_path / result["path"]) as image:
        assert image.getexif().get(274, 1) == 1
    other = tmp_path / "alpha"
    asset = asset_image(other)
    path = other / asset["_path"]
    Image.new("RGBA", (240, 160), (10, 20, 30, 77)).save(path)
    result, _events = await process(other, asset, {"opacity": 0})
    with Image.open(other / result["path"]) as image:
        assert image.getpixel((10, 10))[3] == 77


@pytest.mark.asyncio
async def test_animated_images_and_resource_limits_fail_without_output(tmp_path):
    asset = asset_image(tmp_path)
    with pytest.raises(ValueError, match="像素"):
        await process(tmp_path, asset, {}, max_pixels=10)
    source = tmp_path / asset["_path"]
    Image.new("RGB", (80, 60), "red").save(
        source, format="GIF", save_all=True, append_images=[Image.new("RGB", (80, 60), "blue")]
    )
    with pytest.raises(ValueError, match="动画|多帧"):
        await process(tmp_path, asset, {})
    assert not list((tmp_path / "assets").rglob("processed.*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["add", "remove", "both"])
async def test_video_processing_preserves_all_audio_tracks_and_decodes(tmp_path, mode):
    asset = asset_video(tmp_path)
    source = tmp_path / asset["_path"]
    original = digest(source)
    options = {
        "mode": mode,
        "text": "FIELD NOTES",
        "fontSize": 9,
        "region": {"x": 20, "y": 20, "width": 20, "height": 20},
    }
    result, events = await process(tmp_path, asset, options)
    path = tmp_path / result["path"]
    info = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    tracks = info["streams"]
    assert len([track for track in tracks if track["codec_type"] == "audio"]) == 2
    assert {
        track.get("tags", {}).get("language") for track in tracks if track["codec_type"] == "audio"
    } == {"eng", "zho"}
    assert abs(float(info["format"]["duration"]) - 2) < 0.15
    ffmpeg(
        "-xerror",
        "-err_detect",
        "explode",
        "-i",
        path,
        "-map",
        "0:v",
        "-map",
        "0:a",
        "-f",
        "null",
        "-",
    )
    assert digest(source) == result["sourceSha256"] == original
    assert result["mime"] == "video/mp4" and result["sha256"] == digest(path)
    assert events[-1]["progress"] == 100


@pytest.mark.asyncio
async def test_video_rotation_is_applied_before_watermark_coordinates(tmp_path):
    asset = asset_video(tmp_path, rotation=90)
    result, _events = await process(tmp_path, asset, {"text": "UP", "position": "top-left"})
    assert (result["width"], result["height"]) == (90, 160)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flag,exception", [("paused", DownloadPaused), ("cancelled", DownloadCancelled)]
)
async def test_interrupted_processing_does_not_publish_a_version(tmp_path, flag, exception):
    asset = asset_video(tmp_path)
    control = controls()
    original = digest(tmp_path / asset["_path"])

    async def interrupt(value):
        if value["phase"] == "verifying":
            setattr(control, flag, True)

    with pytest.raises(exception):
        await process(tmp_path, asset, {}, control=control, callback=interrupt)
    assert not list((tmp_path / "assets").rglob("processed.*"))
    assert not list((tmp_path / "assets").rglob(".work-*"))
    assert digest(tmp_path / asset["_path"]) == original


@pytest.mark.asyncio
async def test_source_changed_during_processing_is_not_published(tmp_path):
    asset = asset_image(tmp_path)

    async def replace_source(value):
        if value["phase"] == "verifying":
            Image.new("RGB", (240, 160), "red").save(tmp_path / asset["_path"])

    with pytest.raises(ValueError, match="原件.*变化|原始.*变化"):
        await process(tmp_path, asset, {}, callback=replace_source)
    assert not list((tmp_path / "assets").rglob("processed.*"))


@pytest.mark.asyncio
async def test_storage_guard_failure_is_preserved_and_output_not_published(tmp_path):
    asset = asset_image(tmp_path)

    class Pressure(RuntimeError):
        pass

    class Guard:
        def check(self, required_bytes=0):
            assert required_bytes >= 0
            raise Pressure("low capacity")

    with pytest.raises(Pressure):
        await process(tmp_path, asset, {}, storage_guard=Guard())
    assert not list((tmp_path / "assets").rglob("processed.*"))


@pytest.mark.asyncio
async def test_processing_rejects_path_escape_and_unknown_font(tmp_path):
    asset = asset_image(tmp_path)
    with pytest.raises(ValueError):
        await process(tmp_path, {**asset, "_path": "../secret.png"}, {})
    with pytest.raises(ValueError):
        await process(tmp_path, asset, {}, version="../../escape")
    with pytest.raises(ValueError, match="字体"):
        await process(tmp_path, asset, {"text": "中文"}, font_path=tmp_path / "missing.ttf")
    assert not list((tmp_path / "assets").rglob("processed.*"))


@pytest.mark.asyncio
async def test_subprocess_waiters_are_bounded_and_cancel_stops_child(tmp_path, monkeypatch):
    import asyncio
    import sys

    children = []
    original = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    control = controls()
    baseline = len(asyncio.all_tasks())
    task = asyncio.create_task(
        Processor(tmp_path)._run(
            [sys.executable, "-c", "import time;time.sleep(30)"],
            control,
            timeout=60,
        )
    )
    await asyncio.sleep(0.85)
    alive = len(asyncio.all_tasks()) - baseline
    control.cancelled = True
    with pytest.raises(DownloadCancelled):
        await task
    assert children[0].returncode is not None
    assert alive <= 5


@pytest.mark.asyncio
async def test_subprocess_timeout_terminates_child(tmp_path, monkeypatch):
    import asyncio
    import sys

    children = []
    original = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    with pytest.raises(ValueError, match="超时"):
        await Processor(tmp_path)._run(
            [sys.executable, "-c", "import time;time.sleep(30)"],
            controls(),
            timeout=0.1,
        )
    assert children[0].returncode is not None


@pytest.mark.parametrize("value", [{"mode": []}, {"position": {}}, {"text": None}])
def test_malformed_option_types_report_validation_error(value):
    with pytest.raises(ValueError):
        normalize_processing_options(value)


@pytest.mark.asyncio
async def test_all_audio_tracks_have_preserved_duration_and_metadata(tmp_path):
    asset = asset_video(tmp_path)
    result, _events = await process(tmp_path, asset, {"text": "Audio"})
    assert result["audioTracks"] == 2
    assert {track["language"] for track in result["audio"]} == {"eng", "zho"}
    assert all(abs(track["duration"] - 2) < 0.15 for track in result["audio"])


@pytest.mark.asyncio
async def test_version_publication_is_no_clobber_when_destination_is_symlink(tmp_path):
    asset = asset_image(tmp_path)
    victim = tmp_path / "unrelated.png"
    victim.write_bytes(b"keep")
    folder = tmp_path / "assets" / asset["id"] / "versions" / "version_one"
    folder.mkdir(parents=True)
    (folder / "processed.png").symlink_to(victim)
    with pytest.raises(ValueError):
        await process(tmp_path, asset, {})
    assert victim.read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_shortened_audio_cannot_pass_using_only_container_duration(tmp_path):
    asset = asset_video(tmp_path)

    async def shorten_audio(value):
        if value["phase"] == "verifying":
            path = next((tmp_path / "assets").glob("*/versions/*/.work-*/processed.mp4"))
            shortened = path.with_name("shortened.mp4")
            ffmpeg(
                "-i",
                path,
                "-map",
                "0",
                "-c:v",
                "copy",
                "-af",
                "atrim=duration=0.5",
                "-c:a",
                "aac",
                shortened,
            )
            shortened.replace(path)

    with pytest.raises(ValueError, match="音轨"):
        await process(tmp_path, asset, {"text": "Audio"}, callback=shorten_audio)
    assert not list((tmp_path / "assets").rglob("processed.*"))


@pytest.mark.asyncio
async def test_image_cancellation_terminates_its_worker_before_cleanup(tmp_path, monkeypatch):
    import asyncio

    asset = asset_image(tmp_path)
    control = controls()
    children = []
    original = asyncio.create_subprocess_exec

    async def stop_after_start(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        control.cancelled = True
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", stop_after_start)
    with pytest.raises(DownloadCancelled):
        await process(tmp_path, asset, {}, control=control)
    assert children and all(child.returncode is not None for child in children)
    assert not list((tmp_path / "assets").rglob("processed.*"))
    assert not list((tmp_path / "assets").rglob(".work-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "video"])
async def test_output_size_limit_does_not_publish_partial_version(tmp_path, kind):
    asset = asset_image(tmp_path) if kind == "image" else asset_video(tmp_path)
    with pytest.raises(ValueError, match="大小上限"):
        await process(tmp_path, asset, {}, max_output_bytes=1)
    assert not list((tmp_path / "assets").rglob("processed.*"))


@pytest.mark.asyncio
async def test_video_overlay_rasterization_is_cancellable_without_background_writer(
    tmp_path, monkeypatch
):
    import asyncio
    import sys

    asset = asset_video(tmp_path)
    control = controls()
    workers = []
    original = asyncio.create_subprocess_exec

    async def cancel_raster_worker(*args, **kwargs):
        child = await original(*args, **kwargs)
        if args[0] == sys.executable:
            workers.append(child)
            control.cancelled = True
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cancel_raster_worker)
    with pytest.raises(DownloadCancelled):
        await process(tmp_path, asset, {}, control=control)
    assert workers and all(child.returncode is not None for child in workers)
    assert not list((tmp_path / "assets").rglob(".work-*"))
    assert not list((tmp_path / "assets").rglob("processed.*"))


def test_fixed_region_removal_does_not_require_watermark_text():
    region = {"x": 70, "y": 75, "width": 25, "height": 20}
    result = normalize_processing_options({"mode": "remove", "text": "", "region": region})
    assert result["mode"] == "remove" and result["region"] == region
    for mode in ("add", "both"):
        with pytest.raises(ValueError, match="文字"):
            normalize_processing_options({"mode": mode, "text": "", "region": region})


def motion_options(*points):
    return {
        "mode": "remove",
        "region": {"x": 12.5, "y": 20, "width": 12.5, "height": 14},
        "motion": [
            {"time": t, "x": x, "y": y, "transition": transition}
            for t, x, y, transition in points
        ],
    }


async def test_keyframes_follow_then_hold_and_jump_without_repairing_early_frames(tmp_path):
    # A bright patch drifts diagonally, stays put through the cut, then jumps across the frame.
    width, height, rate = 160, 100, 4
    frames = []
    targets = []
    for n in range(14):
        dx = min(max(n - 2, 0), 4)
        x, y = (20 + 10 * dx, 20 + 5 * dx) if n < 10 else (110, 50)
        frame = Image.new("L", (width, height), 60)
        draw = ImageDraw.Draw(frame)
        draw.rectangle((x + 3, y + 4, x + 10, y + 8), fill=230)
        draw.rectangle((143, 7, 149, 16), fill=185)  # stationary scene detail
        frames.append(frame.tobytes())
        targets.append((x + 6, y + 6))
    folder = tmp_path / "assets" / "neutral_video"
    folder.mkdir(parents=True)
    source = folder / "original.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray", "-s", "160x100",
        "-r", str(rate), "-i", "pipe:0", "-f", "lavfi", "-i", "sine=frequency=440:duration=3.5",
        "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)
    ], input=b"".join(frames), check=True, timeout=20)
    asset = {"id": folder.name, "kind": "video", "_path": str(source.relative_to(tmp_path))}
    options = motion_options((0.5, 12.5, 20, "linear"), (1.5, 37.5, 40, "linear"),
                             (2.5, 68.75, 50, "jump"))
    result, _ = await process(tmp_path, asset, options)
    out = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(tmp_path / result["path"]),
                                   "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    assert len(out) == len(frames) * width * height
    assert result["removalMethod"] == "delogo-keyframes"
    assert result["audioTracks"] == 1 and abs(result["duration"] - 3.5) < 0.1
    for n, (x, y) in enumerate(targets):
        frame = out[n * width * height:(n + 1) * width * height]
        value = frame[y * width + x]
        assert value > 180 if n < 2 else abs(value - 60) < 15, (n, value)
        assert frame[11 * width + 146] > 155


@pytest.mark.parametrize("options", [
    motion_options(),
    motion_options((0, 1, 1, "linear"), (0, 2, 2, "jump")),
    motion_options((1, 90, 20, "linear")),
    motion_options((float("nan"), 12.5, 20, "linear")),
    motion_options((1, 12.5, 20, "gte(t,0)")),
    {**motion_options((1, 12.5, 20, "linear")), "mode": "add"},
    motion_options(*[(i, 10, 10, "jump") for i in range(33)]),
])
def test_invalid_motion_field_is_rejected(options):
    with pytest.raises(ValueError, match="关键帧|移动"):
        normalize_processing_options(options)


async def test_motion_rejects_image_and_keyframe_past_end(tmp_path):
    options = motion_options((1, 12.5, 20, "linear"), (3, 12.5, 20, "jump"))
    with pytest.raises(ValueError, match="视频"):
        await process(tmp_path, asset_image(tmp_path), options)
    with pytest.raises(ValueError, match="时长|片尾"):
        await process(tmp_path, asset_video(tmp_path), options)
    assert not list(tmp_path.rglob("processed.*"))


def test_one_click_glyph_is_distinct_from_manual_boxes_and_idempotent():
    shorthand = {"mode": "remove", "method": "chunlou-glyph"}
    assert normalize_processing_options(shorthand) == shorthand
    assert normalize_processing_options(normalize_processing_options(shorthand)) == shorthand
    refined = {**shorthand, "revision": 2}
    assert normalize_processing_options(refined) == refined
    for invalid in ({"revision": "2"}, {"revision": 3}, {"method": "unknown"}, {"mode": "both"}, {"motion": []},
                    {"region": {"x": 2, "y": 2, "width": 30, "height": 30}}):
        with pytest.raises(ValueError):
            normalize_processing_options({**shorthand, **invalid})


async def test_glyph_clean_background_does_not_publish_a_fake_finished_version(tmp_path):
    folder = tmp_path / "assets" / "unmarked"
    folder.mkdir(parents=True)
    path = folder / "original.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10:duration=1",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", path)
    original = digest(path)
    asset = {"id": folder.name, "kind": "video", "siteId": "chunlou", "_path": str(path.relative_to(tmp_path))}
    with pytest.raises(ValueError, match="未识别到"):
        await process(tmp_path, asset, {"mode": "remove", "method": "chunlou-glyph"})
    assert digest(path) == original and not list(folder.glob("versions/*"))


async def test_glyph_processor_refuses_another_source_even_if_called_directly(tmp_path):
    with pytest.raises(ValueError, match="春楼视频"):
        await process(tmp_path, {"id": "foreign", "kind": "video", "siteId": "other"},
                      {"mode": "remove", "method": "chunlou-glyph"})


@pytest.mark.parametrize("revision", [{}, {"revision": 2}], ids=["original", "refined"])
async def test_glyph_worker_encodes_only_a_confirmed_version_with_matching_audio_duration(tmp_path, revision):
    from pathlib import Path

    import numpy as np

    folder = tmp_path / "assets" / "marked"
    folder.mkdir(parents=True)
    path = folder / "original.mp4"
    mask = np.asarray(Image.open(Path(__file__).parents[1] / "backend/templates/chunlou-glyph-v1.png"),
                      dtype=np.float32) / 65535
    background = np.full((360, 640, 3), (40, 75, 92), dtype=np.uint8)
    frames = []
    for i in range(20):
        frame = background.copy()
        if i < 10:
            h, w = mask.shape
            x, y = 30 + i * 3, 36 + i
            area = frame[y:y+h, x:x+w]
            area[:] = np.rint(area * (1 - mask[..., None]) + 255 * mask[..., None])
        frames.append(frame)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                    "-s", "640x360", "-r", "10", "-i", "pipe:0", "-f", "lavfi", "-i",
                    "sine=frequency=420:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-crf", "14", "-c:a", "aac", "-metadata:s:a:0", "language=zho", str(path)],
                   input=np.stack(frames).tobytes(), check=True, timeout=20)
    asset = {"id": folder.name, "kind": "video", "siteId": "chunlou", "_path": str(path.relative_to(tmp_path))}
    report, _ = await process(tmp_path, asset, {"mode": "remove", "method": "chunlou-glyph", **revision})
    assert report["glyphFrames"] == 10 and report["totalFrames"] == 20
    assert report["removalMethod"] == "chunlou-glyph" and report["sourceSha256"] == digest(path)
    assert report["audio"][0]["language"] == "zho" and abs(report["duration"] - 2) < 0.08
    ffmpeg("-xerror", "-i", tmp_path / report["path"], "-map", "0:v", "-map", "0:a", "-f", "null", "-")


async def test_long_processing_progress_keeps_worker_result(tmp_path):
    import sys

    # Simulate enough progress reports to exceed the output buffer in a long encode.
    command = [sys.executable, "-c", "print('out_time_us=100000000000\\n' * 52000, end=''); print('finished')"]
    report = await Processor(tmp_path)._run(command, controls(), timeout=10, duration=2000)
    assert report.endswith(b'finished\n')

"""CPU detection is local to the frame, and unrelated lettering survives it."""

from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from backend.chunlou_glyph import ChunlouGlyphCleaner, ChunlouGlyphCleanerV2


def marked_frame(width, height, position):
    # Compose the measured glyph onto a known clean image, at an arbitrary position.
    png = Path(__file__).parents[1] / "backend/templates/chunlou-glyph-v1.png"
    with Image.open(png) as image:
        mask = np.asarray(image, np.float32) / 65535
    scale = min(width, height) / 360
    mask = cv2.resize(mask, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
    ys, xs = np.mgrid[:height, :width]
    scene = np.stack(
        [62 + 16 * np.sin(xs / 16), 32 + 24 * np.cos(ys / 18), 95 + 12 * np.sin((xs + ys) / 45)],
        axis=2,
    )
    scene = np.uint8(scene)
    x, y = position
    h, w = mask.shape
    result = scene.copy()
    tile = result[y : y + h, x : x + w]
    tile[:] = np.rint(tile * (1 - mask[..., None]) + 255 * mask[..., None])
    return scene, result, (slice(y, y + h), slice(x, x + w))


@pytest.mark.parametrize("size", [(640, 360), (960, 540)])
def test_acquires_jumps_and_restores_known_clean_scene(size):
    width, height = size
    processor = ChunlouGlyphCleaner(width, height)
    for x, y in [(11, 13), (width // 2, height // 2), (width // 2 + 3, height // 2 + 2)]:
        background, source, area = marked_frame(width, height, (x, y))
        original = source.copy()
        fixed, found = processor.clean(source)
        assert found
        assert np.array_equal(source, original)
        changed = np.any(fixed != original, axis=2)
        changed[area] = False
        assert not changed.any()
        signal = np.abs(original[area].astype(float) - background[area]).sum()
        residual = np.abs(fixed[area].astype(float) - background[area]).sum()
        assert residual < signal * 0.4


def test_blank_scene_unrelated_sign_and_texture_are_not_repaired():
    processor = ChunlouGlyphCleaner(640, 360)
    rng = np.random.default_rng(319)
    canvas = np.full((360, 640, 3), 65, np.uint8)
    cv2.putText(canvas, "CHUNLOU.TV", (80, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.7, (241, 184, 55), 3)
    for source in [canvas, rng.integers(0, 255, canvas.shape, dtype=np.uint8), canvas * 0]:
        output, found = processor.clean(source)
        assert not found
        assert np.array_equal(output, source)


def test_follows_visible_weak_strokes_after_clear_frames_then_skips_when_they_disappear():
    processor = ChunlouGlyphCleaner(640, 360)
    for i in range(5):
        _, marked, _ = marked_frame(640, 360, (200 + i, 90 + i))
        assert processor.clean(marked)[1]
    background, marked, _ = marked_frame(640, 360, (205, 95))
    noise = np.random.default_rng(733).normal(0, 30, (360, 640, 1))
    textured = np.rint(np.clip(marked + noise, 0, 255)).astype(np.uint8)
    assert processor.clean(textured)[1]
    absent = np.rint(np.clip(background + noise, 0, 255)).astype(np.uint8)
    result, matched = processor.clean(absent)
    assert not matched
    assert np.array_equal(absent, result)


def test_temporal_restoration_recovers_fine_background_behind_shifted_lettering():
    width, height = 640, 360
    rng = np.random.default_rng(41)
    noise = cv2.GaussianBlur(rng.normal(0, 22, (height, width)).astype(np.float32), (0, 0), 0.7)
    yy, xx = np.mgrid[:height, :width]
    scene = np.clip(90 + noise + 16 * np.sin((xx + 0.5 * yy) / 5), 0, 255).astype(np.uint8)
    scene = np.stack([scene, scene + 12, scene - 10], axis=2)
    with Image.open(Path(__file__).parents[1] / "backend/templates/chunlou-glyph-v1.png") as image:
        mask = np.asarray(image, np.float32) / 65535
    # A slight dark outline and soft fringe reproduce a compressed wordmark that
    # cannot be fitted exactly with a single white alpha layer.
    ink = mask[..., None]
    border = cv2.dilate(mask, np.ones((3, 3), np.uint8)) - mask
    h, w = mask.shape
    sources, areas = [], []
    for n in range(18):
        x, y = 170 + 2 * n, 95 + n
        tile = scene[y : y + h, x : x + w]
        source = scene.copy()
        source[y : y + h, x : x + w] = np.clip(
            np.rint(tile * (1 - ink) + 255 * ink - 65 * border[..., None]),
            0,
            255,
        )
        sources.append(source)
        areas.append((slice(y, y + h), slice(x, x + w)))
    v1 = ChunlouGlyphCleaner(width, height)
    legacy = [v1.clean(frame)[0] for frame in sources]
    fixed = list(ChunlouGlyphCleanerV2(width, height).clean_frames(iter(sources)))
    assert len(fixed) == len(sources)
    assert all(ok for _, ok in fixed)
    footprint = cv2.dilate((mask > 0.01).astype("uint8"), np.ones((3, 3), "uint8")).astype(bool)

    def loss(outputs):
        return np.mean(
            [
                np.abs(frame[area].astype(float) - scene[area])[footprint].mean()
                for frame, area in zip(outputs[4:-4], areas[4:-4])
            ]
        )

    assert loss([frame for frame, _ in fixed]) < loss(legacy) * 0.70
    assert all(np.array_equal(frame[:50], scene[:50]) for frame, _ in fixed)


def test_reversing_glyph_across_bright_wall_and_dark_object_recovers_both_halves():
    width, height = 640, 360
    png = Path(__file__).parents[1] / "backend/templates/chunlou-glyph-v1.png"
    with Image.open(png) as image:
        alpha = np.asarray(image, np.float32) / 65535
    hh, ww = alpha.shape
    background = np.full((height, width, 3), 200, np.uint8)
    background[:, :304] = [42, 29, 24]
    inputs, truth = [], []
    # Same typography reverses polarity within a single word and is absent on the last frames.
    for i in range(21):
        frame = background.copy()
        x, y = 229 + i, 121 + i // 2
        if i < 17:
            crop = frame[y : y + hh, x : x + ww]
            crop[:] = np.rint(crop + 2.9 * alpha[..., None] * (104 - crop.astype(float)))
        inputs.append(frame)
        truth.append((x, y))
    outputs = list(ChunlouGlyphCleanerV2(width, height).clean_frames(iter(inputs)))
    assert len(outputs) == 21
    for i, (frame, matched) in enumerate(outputs):
        if i >= 17:
            assert not matched
            assert np.array_equal(frame, inputs[i])
            continue
        assert matched
        x, y = truth[i]
        region = np.s_[y : y + hh, x : x + ww]
        for side in [slice(5, 50), slice(125, 158)]:
            confidence = alpha[:, side] > 0.03

            def error(arr):
                return np.mean(
                    np.abs(arr[region][:, side].astype(float) - background[region][:, side])[
                        confidence
                    ]
                )

            assert error(frame) < 0.45 * error(inputs[i])


def test_temporal_cleaner_keeps_an_unmarked_shot_between_confirmed_detections():
    width, height = 640, 360
    clean, marked, _ = marked_frame(width, height, (211, 95))
    no_logo = np.flip(clean, axis=1).copy()
    cv2.putText(no_logo, "CHUNLOU.TV", (180, 115), cv2.FONT_HERSHEY_COMPLEX, 0.7, (230, 220, 0), 1)
    frames = [marked] * 6 + [no_logo] * 3 + [marked] * 6
    outputs = list(ChunlouGlyphCleanerV2(width, height).clean_frames(iter(frames)))
    assert len(outputs) == len(frames) and outputs[0][1] and outputs[-1][1]
    assert all(not matched and np.array_equal(output, no_logo) for output, matched in outputs[6:9])
    for size in ((3840, 2160), (960, 540)):
        tracker = ChunlouGlyphCleanerV2(*size)
        assert (2 * tracker.radius + 1) * 3 * size[0] * size[1] <= 128 * 1024**2

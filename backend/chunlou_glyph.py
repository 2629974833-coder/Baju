"""Track the translucent Chunlou lettering by its strokes, independently per clip."""

from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

_TEMPLATE = Path(__file__).with_name("templates") / "chunlou-glyph-v1.png"


def _highpass(image, sigma):
    return image - cv2.GaussianBlur(image, (0, 0), sigma)


def _features(gray):
    edges = _highpass(gray.astype(np.float32), 1.7)
    power = cv2.blur(edges * edges, (15, 15))
    return edges / np.sqrt(np.maximum(power, 4))


def _similarity(a, b):
    return float(np.sum(a * b) / max(1e-6, np.linalg.norm(a) * np.linalg.norm(b)))


class ChunlouGlyphCleaner:
    """One tracker per clip. clean() accepts RGB24 frames and returns (RGB24, matched)."""

    def __init__(self, width, height):
        self.width, self.height = width, height
        self.scale = min(width, height) / 360
        # Keep even extremely wide/tall input bounded during full-frame searches.
        self.search_scale = max(self.scale, max(width, height) / 1024, 0.75)
        self.search_size = tuple(max(1, round(v / self.search_scale)) for v in (width, height))
        with Image.open(_TEMPLATE) as image:
            alpha = np.asarray(image, np.float32) / 65535
        h, w = alpha.shape
        self.tile_size = (max(1, round(w * self.scale)), max(1, round(h * self.scale)))
        self.search_tile = tuple(max(1, round(v / self.search_scale)) for v in self.tile_size)
        self.template = _features(cv2.resize(alpha * 255, self.search_tile))
        # Half-pixel phases model antialiasing as the overlay moves between pixels.
        self.phases = [
            cv2.warpAffine(alpha, np.float32([[1, 0, dx], [0, 1, dy]]), (w, h))
            for dx in (0, -0.5, 0.5)
            for dy in (0, -0.5, 0.5)
        ]
        self.history = deque(maxlen=5)
        self.frame_number = 0

    def _field(self, gray):
        return _features(gray)

    def _locate(self, gray, bounds, threshold):
        x0, y0, x1, y1 = bounds
        w, h = self.search_tile
        field = self._field(gray[y0:y1, x0:x1])
        scores = cv2.matchTemplate(field, self.template, cv2.TM_CCORR_NORMED)
        _, score, _, (x, y) = cv2.minMaxLoc(scores)
        if score < threshold:
            return None
        # Similar lettering in one corner or a high-contrast scene edge can look
        # like a partial match. Require the left and right glyphs to agree.
        patch = field[y : y + h, x : x + w]
        if not self._agrees(patch, threshold):
            return None
        return x + x0, y + y0

    def _agrees(self, patch, threshold):
        w = self.search_tile[0]
        return all(
            _similarity(patch[:, region], self.template[:, region]) >= threshold * 0.53
            for region in (slice(0, w // 2), slice(w // 2, w))
        )

    def _near(self, gray, x, y, margin, threshold):
        width, height = self.search_size
        w, h = self.search_tile
        x = min(max(0, x), width - w)
        y = min(max(0, y), height - h)
        bounds = (
            max(0, x - margin),
            max(0, y - margin),
            min(width, x + w + margin),
            min(height, y + h + margin),
        )
        return self._locate(gray, bounds, threshold)

    def clean(self, frame):
        if frame.shape != (self.height, self.width, 3) or frame.dtype != np.uint8:
            raise ValueError("春楼飘字修复需要与视频尺寸一致的 RGB24 画面")
        width, height = self.search_size
        w, h = self.search_tile
        if w < 54 or h < 8 or w > width or h > height:
            return frame, False
        small = (
            frame
            if self.search_size == (self.width, self.height)
            else cv2.resize(frame, self.search_size, interpolation=cv2.INTER_AREA)
        )
        gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
        self.frame_number += 1
        pos = None
        if self.history:
            last_frame, x, y = self.history[-1]
            if len(self.history) >= 3:
                # Nearby weak ink can be accepted only when it follows an observed
                # motion. The search still requires both halves of the glyph.
                first_frame, first_x, first_y = self.history[0]
                delay = (self.frame_number - last_frame) / (last_frame - first_frame)
                predicted_x = round(x + (x - first_x) * delay)
                predicted_y = round(y + (y - first_y) * delay)
                pos = self._near(gray, predicted_x, predicted_y, margin=5, threshold=0.12)
            if pos is None:
                pos = self._near(gray, x, y, margin=16, threshold=0.16)
        if pos is None:
            pos = self._locate(gray, (0, 0, width, height), 0.23)
        if pos is not None:
            x = min(round(pos[0] * self.search_scale), self.width - self.tile_size[0])
            y = min(round(pos[1] * self.search_scale), self.height - self.tile_size[1])
            w, h = self.tile_size
            original = frame[y : y + h, x : x + w]
            result = self._restore(original)
            if result is not None:
                output = frame.copy()
                output[y : y + h, x : x + w] = result
                if self.history:
                    last_frame, last_x, last_y = self.history[-1]
                    if max(abs(pos[0] - last_x), abs(pos[1] - last_y)) > 12 * (
                        self.frame_number - last_frame
                    ):
                        self.history.clear()
                self.history.append((self.frame_number, *pos))
                return output, True
        if self.history and self.frame_number - self.history[-1][0] >= 12:
            self.history.clear()
        return frame, False

    def _restore(self, tile):
        # Estimate opacity against local texture at reference resolution. Only pixels
        # inside the matte are written in the original-resolution result.
        h, w = self.phases[0].shape
        small = cv2.resize(tile, (w, h), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY).astype(np.float32)
        edges = _highpass(gray, 1.9)
        contrast = 255 - cv2.GaussianBlur(gray, (0, 0), 2.6)
        weights = 1 / (14 + cv2.GaussianBlur(edges * edges, (0, 0), 5))
        best = None
        for alpha in self.phases:
            predict = _highpass(alpha * contrast, 1.9)
            power = predict * predict * weights
            cross = predict * edges * weights
            energy = float(power.sum())
            covariance = float(cross.sum())
            opacity = covariance / max(0.001, energy)
            gain = covariance * max(0, opacity)
            if best is None or gain > best[0]:
                best = (gain, alpha, cross, power, opacity, energy)
        _, alpha, cross, power, opacity, energy = best
        # Refuse ambiguous bright scene marks or near-invisible accidental matches.
        if opacity < 0.21 or opacity > 2.4:
            return None
        opacity = min(1.5, opacity)
        xcov = cv2.GaussianBlur(cross.sum(axis=0)[None, :], (0, 1), 11)
        xpow = cv2.GaussianBlur(power.sum(axis=0)[None, :], (0, 1), 11)
        regularizer = 0.015 * energy / w
        local = np.clip((xcov + regularizer * opacity) / (xpow + regularizer + 1e-6), 0, 1.75)
        effective = alpha * (0.8 * local + 0.2 * opacity)
        native_alpha = cv2.resize(alpha, self.tile_size)
        effective = cv2.resize(effective, self.tile_size)[..., None]
        restored = np.rint(np.clip((tile - 255 * effective) / (1 - effective), 0, 255)).astype(
            np.uint8
        )

        ink = (native_alpha > 0.007).astype(np.uint8)
        radius = max(1, round(self.scale))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
        nearby = cv2.inpaint(restored, cv2.dilate(ink, kernel), 2 * radius, cv2.INPAINT_NS)
        repaired = cv2.cvtColor(nearby, cv2.COLOR_RGB2GRAY).astype(np.float32)
        texture = np.sqrt(
            cv2.GaussianBlur(_highpass(repaired, 1.9 * self.scale) ** 2, (0, 0), 2 * self.scale)
        )
        blend = (np.clip((native_alpha - 0.004) / 0.020, 0, 1) * (0.31 + 0.47 / (1 + texture / 7)))[
            ..., None
        ]
        return np.rint(restored * (1 - blend) + nearby * blend).astype(np.uint8)


class _AdaptiveInkTracker(ChunlouGlyphCleaner):
    def __init__(self, width, height):
        super().__init__(width, height)
        # Alpha and premultiplied luma measured at four native subpixel phases.
        # Integer translations recover the negative half-phases without blurring.
        self.phases, self.ink_levels = [], []
        with np.load(_TEMPLATE.with_name("chunlou-glyph-phases-v2.npz")) as package:
            for dy in (0, -0.5, 0.5):
                for dx in (0, -0.5, 0.5):
                    index = int(dx != 0) + 2 * int(dy != 0)
                    alpha = np.float32(package[f"alpha{index}"]) / 65535
                    premultiplied = np.float32(package[f"premultiplied{index}"]) / 256
                    transform = np.float32([[1, 0, -int(dx < 0)], [0, 1, -int(dy < 0)]])
                    h, w = alpha.shape
                    self.phases.append(cv2.warpAffine(alpha, transform, (w, h)))
                    self.ink_levels.append(cv2.warpAffine(premultiplied, transform, (w, h)))
        self.template = _features(cv2.resize(self.phases[0] * 255, self.search_tile))

    def _agrees(self, patch, threshold):
        # A stroke over a surface near the ink's own brightness is invisible.
        # Require more than one matching word fragment while following a track.
        if threshold > 0.16:
            return super()._agrees(patch, threshold)
        segments = np.array_split(np.arange(self.search_tile[0]), 4)
        agreement = [_similarity(patch[:, part], self.template[:, part]) for part in segments]
        return sum(score >= threshold * 0.55 for score in agreement) >= 2

    def _field(self, gray):
        value = gray.astype(np.float32)
        background = cv2.GaussianBlur(value, (0, 0), 3)
        # The same letters are bright over dark surfaces and dark over bright
        # surfaces. Reverse the sign per pixel, including when a word crosses an edge.
        return _features(gray) * np.clip((104 - background) / 18, -1, 1)

    def _restore(self, tile):
        h, w = self.phases[0].shape
        small = cv2.resize(tile, (w, h), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY).astype(np.float32)
        edges = _highpass(gray, 1.9)
        # This encoder blends toward middle gray in the luma plane. A white RGB
        # matte cannot invert its dark strokes and introduces hue shifts on skin.
        background = cv2.GaussianBlur(gray, (0, 0), 2.6)
        weight = 1 / (14 + cv2.GaussianBlur(edges * edges, (0, 0), 5))
        best = None
        for alpha, ink_level in zip(self.phases, self.ink_levels, strict=True):
            signature = _highpass(ink_level - alpha * background, 1.9)
            power = signature * signature * weight
            cross = signature * edges * weight
            strength = float(cross.sum()) / max(0.001, float(power.sum()))
            gain = float(cross.sum()) * max(0, strength)
            if best is None or gain > best[0]:
                best = (gain, alpha, ink_level, cross, power, strength)
        _, alpha, ink_level, cross, power, strength = best
        if not 0.12 <= strength <= 4:
            return None
        prior = 0.10 * power.sum() / w
        strength = min(strength, 3)
        local = cv2.GaussianBlur(cross.sum(axis=0)[None, :], (0, 1), 9) + prior * strength
        local /= cv2.GaussianBlur(power.sum(axis=0)[None, :], (0, 1), 9) + prior + 0.00001
        # Clip outliers from sharp subject edges toward the calibrated strength.
        fitted = 0.55 + 0.45 * (0.35 * strength + 0.65 * np.clip(local, 0.3, 1.8))
        opacity = np.clip(cv2.resize(alpha * fitted, self.tile_size), 0, 0.70)
        ink = cv2.resize(ink_level * fitted, self.tile_size)
        luminance = cv2.cvtColor(tile, cv2.COLOR_RGB2GRAY).astype(np.float32)
        correction = (luminance * opacity - ink) / (1 - opacity)
        return np.rint(np.clip(tile + correction[..., None], 0, 255)).astype(np.uint8)


@dataclass
class _ObservedFrame:
    frame: np.ndarray
    index: int
    pos: tuple[int, int] | None = None
    base: np.ndarray | None = None
    anchor: bool = False


class ChunlouGlyphCleanerV2:
    """Restore glyph strokes from exposed background in adjacent frames, on CPU."""

    def __init__(self, width, height):
        self.tracker = _AdaptiveInkTracker(width, height)
        self.width, self.height = width, height
        self.w, self.h = self.tracker.tile_size
        self.scale = self.tracker.scale
        # A symmetric lookahead closes short detection gaps; cap retained RGB bytes at high resolutions.
        self.radius = max(1, min(8, (128 * 1024**2 // (width * height * 3) - 1) // 2))
        alpha = cv2.resize(np.maximum.reduce(self.tracker.phases), (self.w, self.h))
        ink = np.uint8(alpha > 0.016)
        radius = max(1, round(1.5 * self.scale))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
        self.ink = cv2.dilate(ink, kernel)
        self.feather = np.maximum(
            ink, cv2.GaussianBlur(self.ink.astype(np.float32), (0, 0), max(0.65, 0.7 * self.scale))
        )
        self.feather[self.feather < 0.01] = 0

    def _observe(self, frame, index):
        cleaned, found = self.tracker.clean(frame)
        item = _ObservedFrame(frame, index, anchor=found)
        if found:
            _, x, y = self.tracker.history[-1]
            factor = self.tracker.search_scale
            item.pos = (
                min(round(x * factor), self.width - self.w),
                min(round(y * factor), self.height - self.h),
            )
            x, y = item.pos
            item.base = cleaned[y : y + self.h, x : x + self.w].copy()
        return item

    def _bridge(self, target, window):
        left = next((r for r in reversed(window) if r.index < target.index and r.anchor), None)
        right = next((r for r in window if r.index > target.index and r.anchor), None)
        if left is None or right is None:
            return
        interval = right.index - left.index
        displacement = np.subtract(right.pos, left.pos)
        if np.max(np.abs(displacement)) > 5 * self.scale * interval:
            return
        guess = np.asarray(left.pos) + displacement * ((target.index - left.index) / interval)
        small = cv2.resize(target.frame, self.tracker.search_size, interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
        factor = self.tracker.search_scale
        pos = self.tracker._near(gray, round(guess[0] / factor), round(guess[1] / factor), 2, 0.082)
        if pos is None:
            return
        x = min(round(pos[0] * factor), self.width - self.w)
        y = min(round(pos[1] * factor), self.height - self.h)
        base = self.tracker._restore(target.frame[y : y + self.h, x : x + self.w])
        if base is not None:
            target.pos, target.base = (x, y), base

    def _crop(self, row, bounds):
        x0, y0, x1, y1 = bounds
        patch = row.frame[y0:y1, x0:x1]
        clean = patch.copy()
        mask = np.zeros(patch.shape[:2], np.uint8)
        if row.pos is not None:
            x, y = row.pos
            xa, ya = max(x, x0), max(y, y0)
            xb, yb = min(x + self.w, x1), min(y + self.h, y1)
            if xa < xb and ya < yb:
                region = np.s_[ya - y0 : yb - y0, xa - x0 : xb - x0]
                logo = np.s_[ya - y : yb - y, xa - x : xb - x]
                clean[region] = row.base[logo]
                mask[region] = self.ink[logo]
        return clean, mask

    def _motion_candidate(self, reference, ref_gray, ref_mask, row, bounds, grid):
        image, avoid = self._crop(row, bounds)
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        h, w = gray.shape
        factor = max(1, self.scale)
        small_size = (max(1, round(w / factor)), max(1, round(h / factor)))
        ref = cv2.resize(ref_gray, small_size, interpolation=cv2.INTER_AREA)
        alt = cv2.resize(gray, small_size, interpolation=cv2.INTER_AREA)
        # Estimate local background motion using frames with visible lettering already attenuated.
        static_mask = (ref_mask == 0) & (avoid == 0)
        static_diff = cv2.absdiff(ref_gray, gray)[static_mask]
        stationary = static_diff.size and np.mean(static_diff) < 1.2
        if stationary:
            # A static background requires no resampling; flow can follow residual
            # lettering and introduce motion where the fine texture has none.
            warped, exposed = image, np.float32(1 - avoid)
        else:
            flow = cv2.calcOpticalFlowFarneback(ref, alt, None, 0.5, 2, 15, 3, 5, 1.1, 0)
            if factor != 1:
                flow = cv2.resize(flow, (w, h)) * np.float32([w / small_size[0], h / small_size[1]])
            mapping = grid + flow
            warped = cv2.remap(
                image, mapping, None, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
            )
            exposed = cv2.remap(
                np.float32(1 - avoid),
                mapping,
                None,
                cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
            )
        difference = np.mean(np.abs(reference.astype(np.float32) - warped), axis=2)
        # Agreement on the surrounding backdrop rejects different shots, moving occluders
        # and poor warps, while letting the donor disagree inside the contaminated letters.
        context = np.float32(exposed * (1 - ref_mask))
        if np.sum(difference * context) / max(1, context.sum()) > 14:
            return None
        sigma = max(1.0, 2.5 * self.scale)
        normal = cv2.GaussianBlur(context, (0, 0), sigma)
        error = cv2.GaussianBlur(difference * context, (0, 0), sigma) / np.maximum(normal, 0.03)
        support = cv2.GaussianBlur(normal, (0, 0), max(2, 4 * self.scale))
        reliability = np.maximum(0, 1 - error / 12) ** 2 * exposed * np.minimum(1, support / 0.09)
        # Illumination changes or alignment errors right on the stroke can still
        # disagree with its corrected center pixel even if the neighborhood matches.
        if not stationary:
            reliability *= np.clip((32 - difference) / 16, 0, 1)
        return warped, reliability

    def _repair(self, target, window):
        if target.pos is None:
            self._bridge(target, window)
        if target.pos is None:
            return target.frame, False
        x, y = target.pos
        pad = max(5, round(18 * self.scale))
        bounds = (
            max(0, x - pad),
            max(0, y - pad),
            min(self.width, x + self.w + pad),
            min(self.height, y + self.h + pad),
        )
        reference, ref_mask = self._crop(target, bounds)
        gray = cv2.cvtColor(reference, cv2.COLOR_RGB2GRAY)
        h, w = gray.shape
        xx, yy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
        grid = np.stack((xx, yy), axis=2)
        crop = np.s_[y - bounds[1] : y - bounds[1] + self.h, x - bounds[0] : x - bounds[0] + self.w]
        colors = np.zeros((self.h, self.w, 3), np.float32)
        weights = np.zeros((self.h, self.w), np.float32)
        # Nearest frames preserve time continuity; distant frames expose pixels as the glyph travels.
        offsets = {
            1,
            -1,
            self.radius,
            -self.radius,
            max(2, self.radius // 2),
            -max(2, self.radius // 2),
        }
        for row in window:
            if row.index - target.index not in offsets or row.pos is None:
                continue
            match = self._motion_candidate(reference, gray, ref_mask, row, bounds, grid)
            if match is not None:
                sample, confidence = match
                colors += sample[crop] * confidence[crop][..., None]
                weights += confidence[crop]
        donor = colors / np.maximum(weights[..., None], 0.0001)
        # If no adjacent frame reveals a stroke, use local interpolation preferentially
        # on smooth surfaces, and retain the analytical correction on textured edges.
        base = target.base.astype(np.float32)
        spatial = cv2.inpaint(target.base, self.ink, max(2, round(2 * self.scale)), cv2.INPAINT_NS)
        luma = cv2.cvtColor(spatial, cv2.COLOR_RGB2GRAY).astype(np.float32)
        texture = np.sqrt(
            cv2.GaussianBlur(
                _highpass(luma, max(0.7, 1.6 * self.scale)) ** 2, (0, 0), max(0.7, 2 * self.scale)
            )
        )
        spatial_weight = self.feather * (0.15 + 0.82 / (1 + (texture / 5) ** 2))
        fixed = base + (spatial - base) * spatial_weight[..., None]
        temporal_weight = self.feather * np.clip(weights * 1.4, 0, 1)
        output = target.frame.copy()
        output[y : y + self.h, x : x + self.w] = np.uint8(
            np.clip(np.rint(fixed + (donor - fixed) * temporal_weight[..., None]), 0, 255)
        )
        return output, True

    def clean_frames(self, frames):
        """Yield one ordered RGB24 frame and detection flag per input, including both ends."""
        window = deque()
        next_output = 0
        for index, frame in enumerate(frames):
            window.append(self._observe(frame, index))
            if index >= next_output + self.radius:
                target = window[next_output - window[0].index]
                yield self._repair(target, window)
                next_output += 1
                if next_output - window[0].index > self.radius:
                    window.popleft()
        while window and next_output <= window[-1].index:
            target = window[next_output - window[0].index]
            yield self._repair(target, window)
            next_output += 1
            if next_output - window[0].index > self.radius:
                window.popleft()

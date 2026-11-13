"""Stream one local video through the CPU glyph detector; parent owns the process group."""

import contextlib
import json
import subprocess
import sys
import time
from fractions import Fraction

from backend.downloader import Downloader
from backend.processing import _FORMATS


@contextlib.contextmanager
def _pipe(command, **streams):
    proc = subprocess.Popen(command, stderr=subprocess.DEVNULL, **streams)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        for stream in (proc.stdin, proc.stdout):
            if stream:
                with contextlib.suppress(BrokenPipeError):
                    stream.close()
        proc.wait()


def _frames(stream, width, height):
    import numpy as np

    size = width * height * 3
    while chunk := stream.read(size):
        if len(chunk) != size:
            raise ValueError("视频解码不完整，未生成成品")
        yield np.frombuffer(chunk, dtype=np.uint8).reshape(height, width, 3)


def render(source, destination, width, height, rate, offset, revision=1):
    import cv2

    from backend.chunlou_glyph import ChunlouGlyphCleaner

    cv2.setNumThreads(2)
    common = [Downloader._binary("ffmpeg"), "-nostdin", "-v", "error", "-xerror", "-threads", "2"]
    local_input = ["-protocol_whitelist", "file,pipe", "-format_whitelist", _FORMATS, "-i", source]
    # Sample VFR on the original timeline; avoid compressing pauses by numbering input frames.
    decode = [*common, *local_input, "-map", "0:V:0", "-an", "-sn", "-dn", "-filter_threads", "1",
              "-vf", f"setpts=PTS-STARTPTS,fps={rate}", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    encode = [*common, "-n", "-itsoffset", str(offset), "-protocol_whitelist", "pipe",
              "-f", "rawvideo", "-pix_fmt", "rgb24", "-video_size", f"{width}x{height}",
              "-framerate", str(rate), "-i", "pipe:0", *local_input,
              "-map", "0:v:0", "-map", "1:a?", "-map_metadata", "1", "-metadata:s:v:0", "rotate=0",
              "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p",
              "-fps_mode", "passthrough", "-c:v", "libx264", "-threads", "2", "-preset", "veryfast",
              "-crf", "17", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
              "-f", "mp4", destination]
    count = matched = 0
    last_report = 0
    with _pipe(decode, stdout=subprocess.PIPE) as reader, _pipe(encode, stdin=subprocess.PIPE) as writer:
        frames = _frames(reader.stdout, width, height)
        if revision == 2:
            from backend.chunlou_glyph import ChunlouGlyphCleanerV2

            results = ChunlouGlyphCleanerV2(width, height).clean_frames(frames)
        else:
            results = map(ChunlouGlyphCleaner(width, height).clean, frames)
        for cleaned, found in results:
            writer.stdin.write(memoryview(cleaned).cast("B"))
            count += 1
            matched += int(found)
            now = time.monotonic()
            if now - last_report >= 1:
                print(f"out_time_us={int(count * 1_000_000 / rate)}", flush=True)
                last_report = now
        if reader.wait():
            raise ValueError("原视频解码失败，未生成成品")
        writer.stdin.close()
        if writer.wait():
            raise ValueError("修复视频编码失败，未生成成品")
    return {"totalFrames": count, "glyphFrames": matched}


if __name__ == "__main__":
    source, destination, width, height, rate, offset, *version = sys.argv[1:]
    try:
        report = render(source, destination, int(width), int(height), Fraction(rate), float(offset),
                        int(version[0]) if version else 1)
    except ImportError:
        report = {"error": "当前服务缺少春楼飘字处理组件，请同步项目依赖后重试"}
    except (BrokenPipeError, OSError):
        report = {"error": "读取原件或写入修复视频失败，请检查文件和可用磁盘空间"}
    except ValueError as exc:
        report = {"error": str(exc)[:250]}
    print(json.dumps(report), flush=True)

"""Encode a testbot `record` file into MP4 at a fixed rate and report the real frame pacing.

    python scripts/dev/record_video.py <name.bfrec> [--fps 60] [--scale 1]

Each output frame shows the latest game frame presented by then, so stutter and
low frame rates stay visible instead of being smoothed over.
"""

import argparse
import json
from pathlib import Path
import statistics
import struct
import subprocess
import sys
import zlib

sys.path[:0] = [
    str(Path(__file__).resolve().parent),
    str(Path(__file__).resolve().parents[1]),
]
from media import ffmpeg  # noqa: E402


def frames(path):
    with open(path, "rb") as source:
        if source.read(6) != b"BFREC1":
            raise ValueError("not a testbot recording")
        width, height = struct.unpack(">ii", source.read(8))
        while header := source.read(12):
            nanos, length = struct.unpack(">qi", header)
            yield width, height, nanos, zlib.decompress(source.read(length))


def pacing(times):
    gaps = [(b - a) / 1e6 for a, b in zip(times, times[1:])]
    if not gaps:
        return {"frames": len(times)}
    ordered = sorted(gaps)
    return {
        "frames": len(times),
        "seconds": round((times[-1] - times[0]) / 1e9, 3),
        "mean_fps": round(len(gaps) / ((times[-1] - times[0]) / 1e9), 1),
        "median_frame_ms": round(statistics.median(gaps), 2),
        "p95_frame_ms": round(ordered[int(len(ordered) * 0.95) - 1], 2),
        "max_frame_ms": round(ordered[-1], 2),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("recording", type=Path)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args()
    output = args.recording.with_suffix(".mp4")
    times, encoder, start, written, previous = [], None, None, 0, None
    for width, height, nanos, pixels in frames(args.recording):
        if encoder is None:
            start = nanos
            scale = f"scale=trunc(iw*{args.scale}/2)*2:trunc(ih*{args.scale}/2)*2:flags=lanczos,"
            encoder = subprocess.Popen(
                [
                    str(ffmpeg()),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "-s",
                    f"{width}x{height}",
                    "-r",
                    str(args.fps),
                    "-i",
                    "-",
                    "-vf",
                    scale + "vflip",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(output),
                ],
                stdin=subprocess.PIPE,
            )
        times.append(nanos)
        # The previous game frame stays on screen until this one is presented.
        slot = round((nanos - start) * args.fps / 1e9)
        while previous is not None and written < slot:
            encoder.stdin.write(previous)
            written += 1
        previous = pixels
    if encoder is None:
        raise SystemExit("recording has no frames")
    encoder.stdin.write(previous)
    encoder.stdin.close()
    if encoder.wait(timeout=300):
        raise SystemExit("ffmpeg failed")
    report = {"video": str(output), "output_fps": args.fps, **pacing(times)}
    output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

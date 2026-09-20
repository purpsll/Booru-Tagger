"""FFmpeg-only frame extraction and perceptual verification for Booru Video Importer."""
from __future__ import annotations

import json
import os
import statistics
import subprocess
from typing import Dict, Iterable, List, Tuple

DEFAULT_RATIOS = (0.10, 0.24, 0.38, 0.52, 0.66, 0.80, 0.90)


def ffprobe_from_ffmpeg(ffmpeg_path: str) -> str:
    directory, name = os.path.split(ffmpeg_path or "ffmpeg")
    lower = name.casefold()
    if "ffmpeg" in lower:
        probe = name[: lower.index("ffmpeg")] + "ffprobe" + name[lower.index("ffmpeg") + len("ffmpeg"):]
    else:
        probe = "ffprobe.exe" if name.lower().endswith(".exe") else "ffprobe"
    return os.path.join(directory, probe) if directory else probe


def probe_duration(source: str, ffmpeg_path: str = "ffmpeg", timeout: int = 30) -> float:
    cmd = [
        ffprobe_from_ffmpeg(ffmpeg_path),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        source,
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[:300] or "ffprobe failed")
    payload = json.loads(proc.stdout.decode("utf-8", errors="replace") or "{}")
    value = ((payload.get("format") or {}).get("duration"))
    duration = float(value or 0)
    if duration <= 0:
        raise RuntimeError("video duration is unavailable")
    return duration


def _timestamp(duration: float, ratio: float) -> float:
    return max(0.05, min(max(0.05, duration - 0.05), duration * float(ratio)))


def extract_jpeg_frame(
    source: str,
    duration: float,
    ratio: float,
    ffmpeg_path: str = "ffmpeg",
    timeout: int = 30,
) -> bytes:
    ts = _timestamp(duration, ratio)
    cmd = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-ss", f"{ts:.3f}", "-i", source,
        "-frames:v", "1",
        "-vf", "scale=640:640:force_original_aspect_ratio=decrease",
        "-q:v", "5",
        "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[:300] or "frame extraction failed")
    return bytes(proc.stdout)


def frame_ahash(
    source: str,
    duration: float,
    ratio: float,
    ffmpeg_path: str = "ffmpeg",
    timeout: int = 30,
) -> int:
    """Return a 64-bit average hash from one normalized video frame."""
    ts = _timestamp(duration, ratio)
    vf = "scale=8:8:force_original_aspect_ratio=decrease,pad=8:8:(ow-iw)/2:(oh-ih)/2,format=gray"
    cmd = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-ss", f"{ts:.3f}", "-i", source,
        "-frames:v", "1", "-vf", vf,
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    raw = bytes(proc.stdout or b"")
    if proc.returncode != 0 or len(raw) < 64:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[:300] or "perceptual frame extraction failed")
    pixels = raw[:64]
    mean = sum(pixels) / 64.0
    value = 0
    for pixel in pixels:
        value = (value << 1) | (1 if pixel >= mean else 0)
    return value


def hamming_distance(a: int, b: int) -> int:
    return (int(a) ^ int(b)).bit_count()


def frame_hashes(
    source: str,
    duration: float,
    ffmpeg_path: str = "ffmpeg",
    ratios: Iterable[float] = DEFAULT_RATIOS,
    timeout: int = 30,
) -> List[int]:
    return [frame_ahash(source, duration, ratio, ffmpeg_path, timeout) for ratio in ratios]


def verify_video_candidate(
    local_source: str,
    remote_source: str,
    ffmpeg_path: str = "ffmpeg",
    ratios: Iterable[float] = DEFAULT_RATIOS,
    frame_distance: int = 8,
    timeout: int = 45,
    local_duration: float | None = None,
    local_hashes: List[int] | None = None,
) -> Dict[str, object]:
    """Compare several perceptual frames while tolerating modest trim/timing shifts.

    Each local frame is compared against every candidate frame. This is more tolerant
    of intros/outros and small timing offsets than strict same-timestamp matching.
    """
    ratios = tuple(ratios)
    local_duration = local_duration or probe_duration(local_source, ffmpeg_path, timeout)
    remote_duration = probe_duration(remote_source, ffmpeg_path, timeout)
    local_hashes = local_hashes or frame_hashes(local_source, local_duration, ffmpeg_path, ratios, timeout)
    remote_hashes = frame_hashes(remote_source, remote_duration, ffmpeg_path, ratios, timeout)

    nearest: List[int] = []
    for local_hash in local_hashes:
        nearest.append(min(hamming_distance(local_hash, candidate) for candidate in remote_hashes))

    matched = sum(1 for distance in nearest if distance <= frame_distance)
    median = float(statistics.median(nearest)) if nearest else 64.0
    mean = float(sum(nearest) / len(nearest)) if nearest else 64.0

    # High confidence requires agreement across most of the sampled video.
    high = matched >= max(4, len(nearest) - 2) and median <= frame_distance
    # Borderline candidates are surfaced for Review, never auto-imported.
    review = not high and matched >= 3 and median <= frame_distance + 4

    return {
        "high": high,
        "review": review,
        "matched_frames": matched,
        "total_frames": len(nearest),
        "median_distance": round(median, 2),
        "mean_distance": round(mean, 2),
        "nearest_distances": nearest,
        "local_duration": round(float(local_duration), 3),
        "remote_duration": round(float(remote_duration), 3),
    }

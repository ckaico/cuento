"""Proxy media helpers: probing, waveform peaks, thumbnails. All ffmpeg, all cached."""
import array
import json
import os
import subprocess
import threading

from .project import FPS_DEN, FPS_NUM

PEAKS_PER_SEC = 100
_PEAK_RATE = 8000
_ffmpeg_slots = threading.Semaphore(4)
_locks = {}
_locks_guard = threading.Lock()


def _lock(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def probe_frames(path):
    """Video frame count, or None while the file is missing or still being written."""
    if not path or not os.path.exists(path):
        return None
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=nb_frames,duration", "-of", "json", path], capture_output=True, text=True)
    try:
        s = json.loads(r.stdout)["streams"][0]
    except (ValueError, KeyError, IndexError):
        return None
    if s.get("nb_frames", "N/A").isdigit():
        return int(s["nb_frames"])
    return round(float(s["duration"]) * FPS_NUM / FPS_DEN) if s.get("duration") else None


def peaks(path, cache_file):
    """Peak envelope as bytes, PEAKS_PER_SEC values of 0-255. One pass, then cached."""
    with _lock(cache_file):
        if os.path.exists(cache_file) and os.path.getmtime(cache_file) >= os.path.getmtime(path):
            with open(cache_file, "rb") as f:
                return f.read()
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(_PEAK_RATE),
                            "-f", "s16le", "-"], capture_output=True)
        if r.returncode != 0 or not r.stdout:
            return None
        pcm = array.array("h")
        pcm.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
        n = _PEAK_RATE // PEAKS_PER_SEC
        out = bytearray()
        for i in range(0, len(pcm), n):
            chunk = pcm[i:i + n]
            out.append(min(255, max(max(chunk), -min(chunk)) >> 7))
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, "wb") as f:
            f.write(out)
        return bytes(out)


def thumb(path, frame, cache_file):
    with _lock(cache_file):
        if not os.path.exists(cache_file):
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            t = frame * FPS_DEN / FPS_NUM
            with _ffmpeg_slots:
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.4f}", "-i", path, "-frames:v", "1",
                                "-vf", "scale=192:-2", "-q:v", "5", cache_file], capture_output=True)
        return cache_file if os.path.exists(cache_file) else None


def extract_camera_audio(camera_file, out_file):
    """Camera mic as its own file so A1 can be auditioned per clip. Never mixed with the lav."""
    if os.path.exists(out_file):
        return out_file
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    tmp = out_file + ".part.m4a"
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", camera_file, "-vn", "-ac", "2", "-c:a", "aac",
                        "-b:a", "160k", tmp], capture_output=True)
    if r.returncode != 0:
        return None
    os.replace(tmp, out_file)
    return out_file

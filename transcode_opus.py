#!/usr/bin/env python3
"""
transcode_opus.py - Transcode an audio library to Opus to shrink its size.

- Re-encodes mp3/m4a/opus to Opus VBR (default 128 kbps) using ffmpeg/libopus.
- Copies Opus inputs that are already at/below the target bitrate (no re-encode,
  so already-small files are never bloated and stay lossless-as-is).
- Preserves tags (title/artist/album), drops embedded cover art to save space.
- Parallel, resumable (skips files already present in the destination).

Originals are NEVER modified; output goes to a separate folder.

Usage:
  python transcode_opus.py <src> <dst> [--bitrate 128] [--workers 20] [--limit N]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import subprocess
import sys
import threading

AUDIO_EXTS = {".mp3", ".opus", ".m4a", ".aac", ".wav", ".flac", ".ogg"}
_print_lock = threading.Lock()


def probe_bitrate(path: str):
    """Return (codec_name, bit_rate_int_or_None)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,bit_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60).stdout.split()
        codec = out[0] if out else None
        br = None
        for tok in out[1:]:
            if tok.isdigit():
                br = int(tok)
        return codec, br
    except Exception:
        return None, None


def transcode_one(src: str, dst: str, bitrate_k: int) -> tuple[str, str]:
    if os.path.exists(dst):
        return ("skip", src)
    codec, br = probe_bitrate(src)
    target = bitrate_k * 1000
    # Already-Opus and not larger than target -> copy verbatim.
    if codec == "opus" and br is not None and br <= target * 1.1:
        try:
            import shutil
            shutil.copy2(src, dst)
            return ("copy", src)
        except Exception as e:
            return ("error", f"{src} :: copy {e}")
    tmp = dst + ".part"
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", src, "-map", "a:0", "-c:a", "libopus",
             "-b:a", f"{bitrate_k}k", "-vbr", "on",
             "-map_metadata", "0", "-f", "opus", tmp],
            capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not os.path.exists(tmp):
            return ("error", f"{src} :: {r.stderr.strip()[:200]}")
        os.replace(tmp, dst)
        return ("encode", src)
    except Exception as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return ("error", f"{src} :: {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--bitrate", type=int, default=128, help="kbps (default 128)")
    ap.add_argument("--workers", type=int, default=min(20, os.cpu_count() or 8))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    src_root = os.path.abspath(args.src)
    dst_root = os.path.abspath(args.dst)
    os.makedirs(dst_root, exist_ok=True)

    files = [os.path.join(src_root, f) for f in os.listdir(src_root)
             if os.path.splitext(f)[1].lower() in AUDIO_EXTS
             and os.path.isfile(os.path.join(src_root, f))]
    files.sort()
    if args.limit:
        files = files[: args.limit]

    # Map each source to a unique .opus destination name.
    used = set()
    jobs = []
    for src in files:
        stem = os.path.splitext(os.path.basename(src))[0]
        name = stem + ".opus"
        i = 2
        while name.lower() in used:
            name = f"{stem}_{i}.opus"
            i += 1
        used.add(name.lower())
        jobs.append((src, os.path.join(dst_root, name)))

    total = len(jobs)
    print(f"Transcoding {total} files -> {dst_root} at {args.bitrate}k "
          f"({args.workers} workers)\n")
    counts = {"encode": 0, "copy": 0, "skip": 0, "error": 0}
    done = 0
    errors = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(transcode_one, s, d, args.bitrate): s for s, d in jobs}
        for fut in cf.as_completed(futs):
            status, info = fut.result()
            counts[status] += 1
            if status == "error":
                errors.append(info)
            done += 1
            if done % 100 == 0 or done == total:
                with _print_lock:
                    print(f"  {done}/{total}  "
                          f"(enc {counts['encode']}, copy {counts['copy']}, "
                          f"skip {counts['skip']}, err {counts['error']})")

    out_sz = sum(os.path.getsize(os.path.join(dst_root, f))
                 for f in os.listdir(dst_root)
                 if f.lower().endswith(".opus"))
    print(f"\nDone. Output size: {out_sz/1024/1024/1024:.2f} GB")
    print(f"Encoded {counts['encode']}, copied {counts['copy']}, "
          f"skipped {counts['skip']}, errors {counts['error']}")
    if errors:
        print("First errors:")
        for e in errors[:8]:
            print("  -", e)


if __name__ == "__main__":
    raise SystemExit(main())

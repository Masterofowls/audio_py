#!/usr/bin/env python3
"""
enrich_titles.py - Find titles for audio files that have NO title tag, using
their other metadata (artist / album / track / lyrics / embedded cover) and,
as a last resort, a web search. The resolved title is written into the file's
metadata and (optionally) the file is renamed.

Strategy order (first confident hit wins):
  1. AcoustID acoustic fingerprint  -> identifies the song from the AUDIO itself.
       Works even with zero tags. Needs a FREE api key in env ACOUSTID_API_KEY
       (get one in ~1 min at https://acoustid.org/new-application). Fingerprint
       is produced with ffmpeg's built-in chromaprint muxer (no fpcalc needed).
  2. MusicBrainz lookup from artist + album (+ track number). No key required.
  3. Web search (DuckDuckGo) built from lyrics snippet / artist / album.
       Best-effort, low confidence -> written to a review file, not auto-applied
       unless you pass --allow-web-apply.

DEFAULT IS DRY-RUN. Pass --apply to write tags / rename.

Examples
--------
  # Preview what could be resolved (no changes):
  python enrich_titles.py "C:\\Users\\mrdan\\Downloads\\All\\All"

  # Use AcoustID (set the key first), write titles into the files:
  $env:ACOUSTID_API_KEY="xxxx"; python enrich_titles.py "C:\\path" --apply

  # Also rename the files to the resolved title afterwards:
  python enrich_titles.py "C:\\path" --apply --rename
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3, USLT
except ImportError:
    sys.exit("mutagen is required. Install with:  pip install mutagen")

# Reuse the sanitizer / collision helper from the renamer for optional --rename.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from rename_media import sanitize, unique_target
except Exception:  # pragma: no cover - renamer is co-located
    sanitize = None
    unique_target = None

AUDIO_EXTS = {".mp3", ".opus"}
USER_AGENT = "MediaTitleEnricher/1.0 (local library tool)"
MB_BASE = "https://musicbrainz.org/ws/2/"
ACOUSTID_BASE = "https://api.acoustid.org/v2/lookup"
_last_call: dict[str, float] = {}


def safe_print(s: str):
    try:
        print(s)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((s + "\n").encode("utf-8", "replace"))


def polite(host: str, min_interval: float = 1.05):
    """Rate-limit per host (MusicBrainz/AcoustID ask for <= 1 req/sec)."""
    now = time.monotonic()
    wait = min_interval - (now - _last_call.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.monotonic()


def http_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# --------------------------------------------------------------------------- #
# Metadata extraction
# --------------------------------------------------------------------------- #
def read_meta(path: str) -> dict:
    """Collect whatever identifying metadata the file has."""
    meta = {"artist": None, "album": None, "track": None,
            "lyrics": None, "has_cover": False, "duration": None,
            "filename": os.path.splitext(os.path.basename(path))[0]}
    easy = MutagenFile(path, easy=True)
    if easy is not None:
        def g(k):
            v = easy.get(k)
            return str(v[0]).strip() if v else None
        meta["artist"] = g("artist") or g("albumartist")
        meta["album"] = g("album")
        meta["track"] = g("tracknumber")
        try:
            meta["duration"] = float(easy.info.length)
        except Exception:
            pass

    raw = MutagenFile(path)
    if raw is not None and getattr(raw, "tags", None) is not None:
        # cover art
        try:
            if raw.tags.getall("APIC"):
                meta["has_cover"] = True
        except Exception:
            pass
        # lyrics (ID3 USLT)
        try:
            uslt = raw.tags.getall("USLT")
            if uslt and uslt[0].text:
                meta["lyrics"] = str(uslt[0].text).strip()
        except Exception:
            pass
    if hasattr(raw, "pictures") and raw.pictures:
        meta["has_cover"] = True
    # Opus/Vorbis lyrics comment
    if not meta["lyrics"] and raw is not None and getattr(raw, "tags", None):
        for key in ("lyrics", "LYRICS", "unsyncedlyrics"):
            try:
                v = raw.tags.get(key)
                if v:
                    meta["lyrics"] = str(v[0]).strip()
                    break
            except Exception:
                pass
    return meta


# --------------------------------------------------------------------------- #
# Strategy 1: AcoustID acoustic fingerprint
# --------------------------------------------------------------------------- #
def ffmpeg_fingerprint(path: str, max_seconds: int = 120):
    """Return (duration_int, fingerprint_str) using ffmpeg's chromaprint muxer."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
             "-t", str(max_seconds), "-map", "a:0",
             "-f", "chromaprint", "-fp_format", "base64", "-"],
            capture_output=True, text=True, timeout=120,
        )
        fp = (out.stdout or "").strip()
        if not fp:
            return None
    except Exception:
        return None
    dur = None
    try:
        d = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
        dur = int(float(d.stdout.strip()))
    except Exception:
        return None
    return dur, fp


def acoustid_lookup(path: str, api_key: str):
    fp = ffmpeg_fingerprint(path)
    if not fp:
        return None
    duration, fingerprint = fp
    params = urllib.parse.urlencode({
        "client": api_key, "meta": "recordings", "format": "json",
        "duration": duration, "fingerprint": fingerprint,
    })
    polite("acoustid")
    try:
        data = http_json(f"{ACOUSTID_BASE}?{params}")
    except Exception as e:
        return {"error": f"acoustid: {e}"}
    best = None
    for res in data.get("results", []):
        score = res.get("score", 0)
        for rec in res.get("recordings", []) or []:
            title = rec.get("title")
            if not title:
                continue
            artist = None
            if rec.get("artists"):
                artist = rec["artists"][0].get("name")
            cand = {"title": title, "artist": artist,
                    "confidence": round(float(score), 3),
                    "source": "acoustid"}
            if best is None or cand["confidence"] > best["confidence"]:
                best = cand
    return best


# --------------------------------------------------------------------------- #
# Strategy 2: MusicBrainz from artist + album (+ track)
# --------------------------------------------------------------------------- #
_LUCENE = re.compile(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)')
_EDITION = re.compile(
    r"(?i)\s*[\(\[]\s*[^()\[\]]*\b(remaster(ed)?|deluxe|edition|version|"
    r"mono|stereo|expanded|anniversary|bonus|live|explicit|remix|"
    r"single|ep|reissue|\d{4})\b[^()\[\]]*[\)\]]")


def _clean(text: str) -> str:
    """Drop edition/remaster parentheticals that aren't in MB's canonical name."""
    return re.sub(r"\s+", " ", _EDITION.sub("", text)).strip()


def _lucene_escape(text: str) -> str:
    return _LUCENE.sub(r"\\\1", text)


def _primary_artist(artist: str) -> str:
    """First credited artist (queries do better with a single artist)."""
    return re.split(r"\s*(?:,|;|/|&| feat\.?| ft\.?| with | x )\s*",
                    artist, flags=re.IGNORECASE)[0].strip()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def musicbrainz_lookup(meta: dict):
    artist, album, track = meta["artist"], meta["album"], meta["track"]
    if not artist or not album:
        return None
    a_clean = _clean(_primary_artist(artist))
    alb_clean = _clean(album)
    target_track = None
    if track:
        m = re.match(r"\d+", str(track))
        if m:
            target_track = int(m.group())

    # Strict query first, then a relaxed one if it yields nothing.
    queries = [
        f'artist:"{_lucene_escape(a_clean)}" AND release:"{_lucene_escape(alb_clean)}"',
        f'artist:{_lucene_escape(a_clean)} AND release:{_lucene_escape(alb_clean)}',
    ]
    recs = []
    for q in queries:
        url = MB_BASE + "recording?" + urllib.parse.urlencode(
            {"query": q, "fmt": "json", "limit": 50})
        polite("musicbrainz")
        try:
            data = http_json(url)
        except Exception as e:
            return {"error": f"musicbrainz: {e}"}
        recs = data.get("recordings", [])
        if recs:
            break
    if not recs:
        return None

    alb_norm = _norm(alb_clean)
    # Collect every recording that sits at the target track number on a release
    # whose title is our album. Multi-disc releases reuse track numbers, so we
    # only trust the result when all such candidates AGREE on the title.
    if target_track:
        candidates = {}
        for rec in recs:
            for rel in rec.get("releases", []) or []:
                rel_title = _norm(_clean(rel.get("title", "")))
                if not (rel_title == alb_norm or alb_norm in rel_title
                        or rel_title in alb_norm):
                    continue
                for media in rel.get("media", []) or []:
                    for tr in media.get("track", []) or []:
                        if str(tr.get("number")) == str(target_track):
                            title = rec.get("title")
                            key = _norm(title or "")
                            if key:
                                candidates.setdefault(
                                    key, {"title": title,
                                          "score": rec.get("score", 0)})
        if len(candidates) == 1:
            only = next(iter(candidates.values()))
            return {"title": only["title"], "artist": artist,
                    "confidence": round(min(only["score"], 100) / 100, 3),
                    "source": "musicbrainz(track#)"}
        if len(candidates) > 1:
            best = max(candidates.values(), key=lambda c: c["score"])
            return {"title": best["title"], "artist": artist,
                    "confidence": 0.45,  # disagreement -> needs review
                    "source": "musicbrainz(ambiguous-disc)"}
    # No usable track signal: best-scoring recording, but kept BELOW the
    # auto-apply threshold (<0.5) so it always goes to the review file.
    top = recs[0]
    return {"title": top.get("title"), "artist": artist,
            "confidence": round(min(top.get("score", 0), 100) / 100 * 0.4, 3),
            "source": "musicbrainz(ambiguous)"}


# --------------------------------------------------------------------------- #
# Strategy 3: Web search fallback
# --------------------------------------------------------------------------- #
_NOISE = re.compile(
    r"(?i)\b(official|lyrics|video|audio|hd|hq|music|youtube|spotify|"
    r"genius|azlyrics|musixmatch|soundcloud)\b")


def web_search_title(meta: dict):
    parts = []
    if meta["lyrics"]:
        line = next((l.strip() for l in meta["lyrics"].splitlines()
                     if len(l.strip()) > 12), "")
        if line:
            parts.append(f'"{line}"')
    if meta["artist"]:
        parts.append(meta["artist"])
    if meta["album"]:
        parts.append(meta["album"])
    if not parts:
        return None
    query = " ".join(parts) + " song title"
    for endpoint in ("https://html.duckduckgo.com/html/",
                     "https://lite.duckduckgo.com/lite/"):
        try:
            url = endpoint + "?" + urllib.parse.urlencode({"q": query})
            req = urllib.request.Request(
                url, headers={"User-Agent":
                              "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            polite("duckduckgo", 1.5)
            html = urllib.request.urlopen(req, timeout=20).read().decode(
                "utf-8", "ignore")
        except Exception:
            continue
        cands = re.findall(r'<a[^>]+class="result__a"[^>]*>(.*?)</a>', html)
        cands += re.findall(r'<a[^>]+class="result-link"[^>]*>(.*?)</a>', html)
        cands += re.findall(r"<a[^>]+rel=\"nofollow\"[^>]*>(.*?)</a>", html)
        for raw_t in cands:
            text = re.sub("<.*?>", "", raw_t)
            text = re.sub(r"&[a-z]+;", " ", text).strip()
            # Heuristic: take segment after "Artist -" / before "lyrics".
            seg = re.split(r"[-–|:]", text)
            seg = [s.strip() for s in seg if s.strip()]
            for s in seg:
                if meta["artist"] and meta["artist"].lower() in s.lower():
                    continue
                if _NOISE.search(s) or len(s) < 3:
                    continue
                return {"title": s, "artist": meta["artist"],
                        "confidence": 0.3, "source": "websearch"}
    return None


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #
def resolve(path: str, meta: dict, api_key: str | None, use_web: bool):
    if api_key:
        r = acoustid_lookup(path, api_key)
        if r and r.get("title"):
            return r
    r = musicbrainz_lookup(meta)
    if r and r.get("title") and r.get("confidence", 0) >= 0.85:
        return r
    if use_web:
        w = web_search_title(meta)
        if w and w.get("title"):
            return w
    # return low-confidence MB result if nothing better
    return r if (r and r.get("title")) else None


def write_title(path: str, title: str) -> bool:
    try:
        audio = MutagenFile(path, easy=True)
        if audio is None:
            return False
        if audio.tags is None:
            try:
                audio.add_tags()
            except Exception:
                pass
        audio["title"] = title
        audio.save()
        return True
    except Exception as e:
        print(f"    [tag-error] {e}")
        return False


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("library", help="Folder containing the audio files.")
    ap.add_argument("--apply", action="store_true",
                    help="Write tags / rename (default is dry-run).")
    ap.add_argument("--recurse", action="store_true", help="Include subfolders.")
    ap.add_argument("--rename", action="store_true",
                    help="After writing the title, rename the file to it.")
    ap.add_argument("--no-web", dest="use_web", action="store_false",
                    help="Disable the web-search fallback.")
    ap.add_argument("--allow-web-apply", action="store_true",
                    help="Permit applying low-confidence web-search results.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N files (0 = no limit).")
    ap.add_argument("--min-confidence", type=float, default=0.5,
                    help="Minimum confidence to auto-apply (default 0.5).")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    root = os.path.abspath(args.library)
    if not os.path.isdir(root):
        sys.exit(f"Not a folder: {root}")

    api_key = os.environ.get("ACOUSTID_API_KEY")
    if api_key:
        print("AcoustID: enabled (fingerprint lookup active).")
    else:
        print("AcoustID: DISABLED (set ACOUSTID_API_KEY to identify files that "
              "have no usable tags). Get a free key at "
              "https://acoustid.org/new-application")
    print("Web search fallback:", "on" if args.use_web else "off")
    print("Mode:", "APPLY" if args.apply else "DRY-RUN", "\n")

    # collect untitled files
    files = []
    walker = (os.walk(root) if args.recurse
              else [(root, [], os.listdir(root))])
    for dirpath, _d, names in walker:
        for n in names:
            full = os.path.join(dirpath, n)
            if (os.path.isfile(full)
                    and os.path.splitext(n)[1].lower() in AUDIO_EXTS):
                easy = MutagenFile(full, easy=True)
                if not (easy and easy.get("title")):
                    files.append(full)

    print(f"Untitled files found: {len(files)}")
    if args.limit:
        files = files[: args.limit]
        print(f"(limited to {len(files)})")

    log_path = os.path.join(
        root, f"enrich_log_{_dt.datetime.now():%Y%m%d_%H%M%S}.csv")
    review_path = os.path.join(
        root, f"enrich_review_{_dt.datetime.now():%Y%m%d_%H%M%S}.csv")

    applied = resolved = low = none = 0
    taken: dict[str, set] = {}
    review_rows = []

    # Crash-safe: open the change log up front and flush after every applied
    # row. Columns start with old_path,new_path so this file can be fed
    # straight into  rename_media.py --undo  to revert all renames.
    log_fh = open(log_path, "w", newline="", encoding="utf-8")
    log_writer = csv.writer(log_fh)
    log_writer.writerow(["old_path", "new_path", "resolved_title",
                         "artist", "source", "confidence"])
    log_fh.flush()

    try:
        for i, path in enumerate(files, 1):
            meta = read_meta(path)
            base = os.path.basename(path)
            has_clues = any([meta["artist"], meta["album"], meta["lyrics"],
                             meta["has_cover"]])
            res = resolve(path, meta, api_key, args.use_web)
            if not res or not res.get("title"):
                none += 1
                review_rows.append([base, meta["artist"], meta["album"],
                                    bool(meta["lyrics"]), meta["has_cover"],
                                    "", "",
                                    "no-match" if has_clues or api_key
                                    else "no-metadata-and-no-acoustid-key"])
                continue
            title = res["title"]
            conf = res.get("confidence", 0)
            src = res.get("source", "?")
            safe_print(f"[{i}/{len(files)}] {base}\n    -> {title}  "
                       f"(src={src}, conf={conf})")

            is_web = src == "websearch"
            auto = (conf >= args.min_confidence
                    and (not is_web or args.allow_web_apply))
            if not auto:
                low += 1
                review_rows.append([base, meta["artist"], meta["album"],
                                    bool(meta["lyrics"]), meta["has_cover"],
                                    title, src, f"low-confidence({conf})"])
                continue

            resolved += 1
            new_path = path
            if args.apply:
                if write_title(path, title):
                    if args.rename and sanitize and unique_target:
                        ext = os.path.splitext(path)[1].lower()
                        stem = sanitize(title, "chars")
                        d = os.path.dirname(path)
                        tk = taken.setdefault(d, set())
                        dst = unique_target(d, stem, ext, tk)
                        tk.add(os.path.basename(dst).lower())
                        try:
                            os.rename(path, dst)
                            new_path = dst
                        except Exception as e:
                            safe_print(f"    [rename-error] {e}")
                    applied += 1
                    log_writer.writerow([path, new_path, title,
                                         res.get("artist"), src, conf])
                    log_fh.flush()
            else:
                log_writer.writerow([path, "(dry-run)", title,
                                     res.get("artist"), src, conf])
                log_fh.flush()
    finally:
        log_fh.close()

    if review_rows:
        with open(review_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["file", "artist", "album", "has_lyrics", "has_cover",
                        "suggested_title", "source", "reason"])
            w.writerows(review_rows)
    if resolved == 0:
        try:
            os.remove(log_path)
        except OSError:
            pass

    print("\nSummary:")
    print(f"  Untitled processed : {len(files)}")
    print(f"  Confident matches  : {resolved}"
          + (f"  (written: {applied})" if args.apply else ""))
    print(f"  Low-confidence     : {low}  -> review file")
    print(f"  No match           : {none}")
    if resolved:
        print(f"  Change log         : {log_path}")
        if args.apply and args.rename:
            print(f"  Undo renames with  : python rename_media.py --undo \"{log_path}\"")
    if review_rows:
        print(f"  Review needed      : {review_path}")
    if not args.apply and resolved:
        print("\nDRY-RUN: re-run with --apply (and --rename) to write changes.")


if __name__ == "__main__":
    raise SystemExit(main())

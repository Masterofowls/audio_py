#!/usr/bin/env python3
"""
rename_media.py - Rename mp3/opus files to their metadata title.

Renames files like 20260401_133117.mp3  ->  Sweet Dreams (Are Made of This).mp3
using the embedded "title" tag (read with mutagen).

DEFAULT IS DRY-RUN: nothing is renamed until you pass --apply.

Examples
--------
  # Preview what would happen (no changes):
  python rename_media.py "C:\\Users\\mrdan\\Downloads\\All\\All"

  # Actually rename:
  python rename_media.py "C:\\Users\\mrdan\\Downloads\\All\\All" --apply

  # Include subfolders, use "Artist - Title":
  python rename_media.py "C:\\path" --apply --recurse --pattern artist_title

  # Undo a previous run using its log:
  python rename_media.py --undo "rename_log_20260614_201500.csv"
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import re
import sys

try:
    from mutagen import File as MutagenFile
except ImportError:
    sys.exit("mutagen is required. Install with:  pip install mutagen")

AUDIO_EXTS = {".mp3", ".opus"}

# Characters illegal in Windows filenames.
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# Bracket pairs and their characters.
_BRACKET_PAIRS = re.compile(r"[\(\[\{][^\(\)\[\]\{\}]*[\)\]\}]")
_BRACKET_CHARS = re.compile(r"[\(\)\[\]\{\}]")
# Windows reserved device names.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MAX_STEM = 180  # keep filenames comfortably within path limits


def sanitize(name: str, brackets: str = "none") -> str:
    """Turn an arbitrary tag value into a safe Windows filename stem.

    brackets: 'none'     -> keep brackets
              'chars'    -> remove only ( ) [ ] { } characters
              'contents' -> remove brackets and the text inside them
    """
    if brackets == "contents":
        # repeat to handle nested/adjacent groups
        prev = None
        while prev != name:
            prev = name
            name = _BRACKET_PAIRS.sub("", name)
        name = _BRACKET_CHARS.sub("", name)  # strip any unmatched leftovers
    elif brackets == "chars":
        name = _BRACKET_CHARS.sub("", name)
    name = _ILLEGAL.sub("", name)
    name = name.replace("\n", " ").replace("\r", " ")
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(". ")  # Windows forbids trailing dot/space
    if name.upper() in _RESERVED:
        name = "_" + name
    if len(name) > _MAX_STEM:
        name = name[:_MAX_STEM].rstrip(". ")
    return name


def get_tag(audio, key: str) -> str | None:
    """Read first value of a tag (EasyMP3 / Opus vorbis comments)."""
    if audio is None:
        return None
    try:
        val = audio.get(key)
    except Exception:
        return None
    if not val:
        return None
    if isinstance(val, (list, tuple)):
        val = val[0]
    val = str(val).strip()
    return val or None


def build_stem(audio, pattern: str, brackets: str = "none") -> str | None:
    title = get_tag(audio, "title")
    if not title:
        return None
    if pattern == "title":
        stem = title
    elif pattern == "artist_title":
        artist = get_tag(audio, "artist")
        stem = f"{artist} - {title}" if artist else title
    elif pattern == "tracknum_title":
        track = get_tag(audio, "tracknumber")
        if track:
            track = track.split("/")[0].strip().zfill(2)
            stem = f"{track} - {title}"
        else:
            stem = title
    else:
        stem = title
    return sanitize(stem, brackets)


def iter_files(root: str, recurse: bool):
    if recurse:
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if os.path.splitext(f)[1].lower() in AUDIO_EXTS:
                    yield os.path.join(dirpath, f)
    else:
        for f in os.listdir(root):
            full = os.path.join(root, f)
            if os.path.isfile(full) and os.path.splitext(f)[1].lower() in AUDIO_EXTS:
                yield full


def unique_target(directory: str, stem: str, ext: str, taken: set[str],
                  suffix_start: int = 2) -> str:
    """Return a path that doesn't collide with disk or already-planned names.

    Duplicates are suffixed as 'stem_N' (e.g. song_2, song_3 ...).
    """
    candidate = stem + ext
    if candidate.lower() not in taken and not os.path.exists(
        os.path.join(directory, candidate)
    ):
        return os.path.join(directory, candidate)
    i = suffix_start
    while True:
        candidate = f"{stem}_{i}{ext}"
        if candidate.lower() not in taken and not os.path.exists(
            os.path.join(directory, candidate)
        ):
            return os.path.join(directory, candidate)
        i += 1


def run_rename(args) -> int:
    root = os.path.abspath(args.library)
    if not os.path.isdir(root):
        sys.exit(f"Not a folder: {root}")

    log_path = os.path.join(
        root,
        f"rename_log_{_dt.datetime.now():%Y%m%d_%H%M%S}.csv",
    )

    planned: list[tuple[str, str]] = []
    stats = {"total": 0, "renamed": 0, "skipped_no_title": 0,
             "skipped_same": 0, "errors": 0}
    # Reserve names already on disk so we never overwrite an existing file.
    taken_by_dir: dict[str, set[str]] = {}

    for src in iter_files(root, args.recurse):
        stats["total"] += 1
        directory = os.path.dirname(src)
        ext = os.path.splitext(src)[1].lower()
        try:
            audio = MutagenFile(src, easy=True)
        except Exception as e:
            stats["errors"] += 1
            print(f"[ERROR] {os.path.basename(src)}: {e}")
            continue

        stem = build_stem(audio, args.pattern, args.brackets)
        if not stem:
            # No title tag: optionally still clean brackets from the existing name.
            if args.clean_untitled and args.brackets != "none":
                cleaned = sanitize(os.path.splitext(os.path.basename(src))[0],
                                   args.brackets)
                if cleaned and cleaned != os.path.splitext(
                    os.path.basename(src))[0]:
                    stem = cleaned
            if not stem:
                stats["skipped_no_title"] += 1
                if args.verbose:
                    print(f"[NO TITLE] {os.path.basename(src)}")
                continue

        taken = taken_by_dir.setdefault(directory, set())
        current = os.path.basename(src)
        current_stem = os.path.splitext(current)[0]
        # Already correctly named (exact match or an accepted stem_N duplicate)?
        already_ok = current_stem.lower() == stem.lower() or re.fullmatch(
            re.escape(stem) + r"_\d+", current_stem, re.IGNORECASE
        )
        if already_ok and ext == os.path.splitext(current)[1].lower():
            stats["skipped_same"] += 1
            taken.add(current.lower())
            continue

        dst = unique_target(directory, stem, ext, taken, args.suffix_start)
        taken.add(os.path.basename(dst).lower())
        planned.append((src, dst))

    # Show plan
    for src, dst in planned[: args.preview]:
        print(f"  {os.path.basename(src)}\n    -> {os.path.basename(dst)}")
    if len(planned) > args.preview:
        print(f"  ... and {len(planned) - args.preview} more")

    if not args.apply:
        print(
            f"\nDRY-RUN. {len(planned)} file(s) would be renamed. "
            f"Re-run with --apply to perform them."
        )
        _print_stats(stats, len(planned))
        return 0

    # Apply
    with open(log_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["old_path", "new_path"])
        for src, dst in planned:
            try:
                os.rename(src, dst)
                writer.writerow([src, dst])
                stats["renamed"] += 1
            except Exception as e:
                stats["errors"] += 1
                print(f"[ERROR] renaming {os.path.basename(src)}: {e}")

    print(f"\nDone. Log written to:\n  {log_path}")
    _print_stats(stats, stats["renamed"])
    return 0


def run_undo(log_file: str) -> int:
    if not os.path.isfile(log_file):
        sys.exit(f"Log file not found: {log_file}")
    restored = errors = 0
    with open(log_file, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))[1:]  # skip header
    # Reverse order to be safe with chained collisions.
    for old_path, new_path in reversed(rows):
        if not os.path.exists(new_path):
            print(f"[MISSING] {new_path}")
            errors += 1
            continue
        if os.path.exists(old_path):
            print(f"[CONFLICT] original already exists: {old_path}")
            errors += 1
            continue
        try:
            os.rename(new_path, old_path)
            restored += 1
        except Exception as e:
            print(f"[ERROR] {new_path}: {e}")
            errors += 1
    print(f"\nUndo complete. Restored {restored}, errors {errors}.")
    return 0


def _print_stats(stats: dict, action_count: int):
    print(
        "\nSummary:"
        f"\n  Scanned          : {stats['total']}"
        f"\n  To rename/renamed: {action_count}"
        f"\n  Already correct  : {stats['skipped_same']}"
        f"\n  No title (skip)  : {stats['skipped_no_title']}"
        f"\n  Errors           : {stats['errors']}"
    )


def main():
    ap = argparse.ArgumentParser(
        description="Rename mp3/opus files to their metadata title.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("library", nargs="?", help="Folder containing the audio files.")
    ap.add_argument("--apply", action="store_true",
                    help="Perform renames (default is a safe dry-run).")
    ap.add_argument("--recurse", action="store_true",
                    help="Include subfolders.")
    ap.add_argument(
        "--pattern",
        choices=["title", "artist_title", "tracknum_title"],
        default="title",
        help="Filename pattern (default: title).",
    )
    ap.add_argument(
        "--brackets",
        choices=["none", "chars", "contents"],
        default="chars",
        help="Bracket handling: 'chars' removes ()[]{} characters (default), "
             "'contents' removes brackets and their inner text, 'none' keeps them.",
    )
    ap.add_argument(
        "--no-clean-untitled", dest="clean_untitled", action="store_false",
        help="Do NOT strip brackets from the existing names of files that "
             "have no title tag (by default such names are still cleaned).",
    )
    ap.add_argument(
        "--suffix-start", dest="suffix_start", type=int, default=2,
        help="First number used for duplicate names: song_N (default 2).",
    )
    ap.add_argument("--preview", type=int, default=20,
                    help="How many planned renames to print (default 20).")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Also list files skipped for missing title.")
    ap.add_argument("--undo", metavar="LOG_CSV",
                    help="Undo a previous run using its rename_log_*.csv.")
    args = ap.parse_args()

    if args.undo:
        return run_undo(args.undo)
    if not args.library:
        ap.error("library folder is required (or use --undo LOG_CSV)")
    return run_rename(args)


if __name__ == "__main__":
    raise SystemExit(main())

"""
Match slide decks to SRT files by lecture number.

Naming conventions:
  Decks : *_L<N>_*.pptx  or  *_L<N>_*.pdf
  SRTs  : *_L<N>_S<M>*.srt   (optional _v2 / _V2 / _v3 … suffix)

Rules:
  - If both .pptx and .pdf exist for the same lecture, use .pptx.
  - For SRTs sharing the same (lecture, segment) number, pick the one
    with the latest modification time.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


_DECK_RE  = re.compile(r"L(\d+)", re.IGNORECASE)
_SRT_RE   = re.compile(r"L(\d+)_S(\d+)", re.IGNORECASE)


@dataclass
class SegmentInfo:
    segment_num: int
    srt_path: Path
    srt_name: str          # filename without extension


@dataclass
class LectureInfo:
    lecture_num: int
    deck_path: Path
    deck_stem: str         # filename without extension (e.g. "APM_L1_AI and Precision Medicine")
    deck_format: str       # "pptx" or "pdf"
    segments: list[SegmentInfo] = field(default_factory=list)


# ── helpers ────────────────────────────────────────────────────────────────────

def _lecture_num(path: Path) -> int | None:
    m = _DECK_RE.search(path.stem)
    return int(m.group(1)) if m else None


def _srt_lecture_segment(path: Path) -> tuple[int, int] | None:
    m = _SRT_RE.search(path.stem)
    return (int(m.group(1)), int(m.group(2))) if m else None


# ── public API ─────────────────────────────────────────────────────────────────

def scan_inputs(
    deck_input: str,   # path to a single deck file OR a folder of decks
    srt_folder: str,   # path to the folder containing SRT files
) -> list[LectureInfo]:
    """
    Return a list of LectureInfo objects, each pairing a deck with its SRT segments.
    Raises ValueError if no matching lectures are found.
    """
    deck_input = Path(deck_input)
    srt_folder = Path(srt_folder)

    # --- collect decks ---
    if deck_input.is_file():
        deck_candidates = [deck_input]
    elif deck_input.is_dir():
        deck_candidates = list(deck_input.glob("*.pptx")) + list(deck_input.glob("*.pdf"))
    else:
        raise ValueError(f"Deck path not found: {deck_input}")

    # Map lecture_num -> best deck path (prefer pptx over pdf)
    lecture_to_deck: dict[int, Path] = {}
    for p in deck_candidates:
        num = _lecture_num(p)
        if num is None:
            continue
        existing = lecture_to_deck.get(num)
        if existing is None:
            lecture_to_deck[num] = p
        elif existing.suffix.lower() == ".pdf" and p.suffix.lower() == ".pptx":
            # pptx wins
            lecture_to_deck[num] = p

    if not lecture_to_deck:
        raise ValueError("No lecture decks found (files must contain 'L<number>' in the name).")

    # --- collect SRTs ---
    if not srt_folder.is_dir():
        raise ValueError(f"SRT folder not found: {srt_folder}")

    all_srts = list(srt_folder.glob("*.srt"))

    # Map (lecture_num, segment_num) -> best SRT path (latest mtime)
    srt_map: dict[tuple[int, int], Path] = {}
    for p in all_srts:
        key = _srt_lecture_segment(p)
        if key is None:
            continue
        existing = srt_map.get(key)
        if existing is None or p.stat().st_mtime > existing.stat().st_mtime:
            srt_map[key] = p

    # --- build LectureInfo list ---
    lectures: list[LectureInfo] = []
    for lnum in sorted(lecture_to_deck):
        deck = lecture_to_deck[lnum]
        # Find all segments for this lecture, sorted by segment number
        segs_for_lecture = sorted(
            [(lnum, snum) for (ln, snum) in srt_map if ln == lnum],
            key=lambda x: x[1],
        )
        if not segs_for_lecture:
            continue   # skip lectures with no SRTs

        segments = [
            SegmentInfo(
                segment_num=snum,
                srt_path=srt_map[(lnum, snum)],
                srt_name=srt_map[(lnum, snum)].stem,
            )
            for _, snum in segs_for_lecture
        ]

        lectures.append(
            LectureInfo(
                lecture_num=lnum,
                deck_path=deck,
                deck_stem=deck.stem,
                deck_format=deck.suffix.lstrip(".").lower(),
                segments=segments,
            )
        )

    if not lectures:
        raise ValueError(
            "No lectures could be matched. Ensure SRT files contain 'L<N>_S<M>' "
            "and deck files contain 'L<N>' in their names, with matching lecture numbers."
        )

    return lectures

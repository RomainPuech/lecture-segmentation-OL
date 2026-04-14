"""Parse SRT subtitle files and return clean text content."""

from __future__ import annotations

import re
from pathlib import Path


_TIMESTAMP_RE = re.compile(
    r"\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}"
)
_INDEX_RE = re.compile(r"^\s*\d+\s*$")


def parse_srt(path: str | Path) -> str:
    """
    Read an SRT file and return its spoken text (no index numbers, no timestamps).
    Lines are joined with spaces; blank lines between cues are preserved as newlines.
    """
    path = Path(path)
    raw = path.read_text(encoding="utf-8", errors="replace")

    lines = raw.splitlines()
    text_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _INDEX_RE.match(stripped):
            continue
        if _TIMESTAMP_RE.match(stripped):
            continue
        text_lines.append(stripped)

    return " ".join(text_lines)


def parse_srt_with_timing(path: str | Path) -> list[dict]:
    """
    Return a list of cue dicts: {"start": str, "end": str, "text": str}
    """
    path = Path(path)
    raw = path.read_text(encoding="utf-8", errors="replace")

    cues: list[dict] = []
    blocks = re.split(r"\n\s*\n", raw.strip())

    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        # Skip numeric index if present
        start_idx = 0
        if _INDEX_RE.match(lines[0]):
            start_idx = 1
        if start_idx >= len(lines):
            continue
        ts_match = _TIMESTAMP_RE.match(lines[start_idx])
        if not ts_match:
            continue
        ts_parts = lines[start_idx].split("-->")
        start = ts_parts[0].strip()
        end = ts_parts[1].strip() if len(ts_parts) > 1 else ""
        text = " ".join(lines[start_idx + 1 :])
        cues.append({"start": start, "end": end, "text": text})

    return cues

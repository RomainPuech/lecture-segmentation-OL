"""
Claude API integration.

Two capabilities:
  1. detect_cover_slide(image_bytes) → bool
  2. segment_slides(slide_images, srt_contents) → list of segment dicts
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

MODEL = "claude-sonnet-4-5"   # Update if a newer model is available


def _client() -> anthropic.Anthropic:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set. Check your .env file.")
    return anthropic.Anthropic(api_key=key)


def _img_block(png_bytes: bytes) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(png_bytes).decode(),
        },
    }


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


# ── cover slide detection ──────────────────────────────────────────────────────

def detect_cover_slide_text(slide_text: str) -> bool:
    """
    Text-based cover slide detection (used when LibreOffice is unavailable).
    Returns True if the slide text is minimal enough to be a cover slide.
    """
    response = _client().messages.create(
        model=MODEL,
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": (
                f"This is the text extracted from the first slide of a lecture deck:\n\n"
                f"{slide_text}\n\n"
                "Is this a 'cover slide' (only a title, course name, or institution name — "
                "no detailed lecture content)?\n"
                'Reply with JSON only: {"is_cover": true/false, "reason": "one sentence"}'
            ),
        }],
    )
    text = response.content[0].text.strip()
    m = re.search(r"\{[^}]+\}", text, re.DOTALL)
    if m:
        try:
            return bool(json.loads(m.group()).get("is_cover", False))
        except json.JSONDecodeError:
            pass
    return False


def detect_cover_slide(slide_image: bytes) -> bool:
    """
    Ask Claude whether the given slide (PNG bytes) is a cover slide.
    A cover slide has only a title / course name / logos / decorative images —
    nothing specific to one segment of a lecture.
    Returns True if it is a cover slide.
    """
    response = _client().messages.create(
        model=MODEL,
        max_tokens=256,
        messages=[
            {
                "role": "user",
                "content": [
                    _img_block(slide_image),
                    _text_block(
                        "Look at this slide. Is it a 'cover slide'?\n"
                        "A cover slide contains only: a title, course/module name, "
                        "institution logos, and/or decorative images. "
                        "It does NOT contain lecture content such as bullet points, "
                        "charts, diagrams, tables, equations, or detailed text.\n\n"
                        "Reply with JSON only — no other text:\n"
                        '{"is_cover": true, "reason": "one sentence"}'
                    ),
                ],
            }
        ],
    )
    text = response.content[0].text.strip()
    m = re.search(r"\{[^}]+\}", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            return bool(data.get("is_cover", False))
        except json.JSONDecodeError:
            pass
    return False


# ── slide segmentation ─────────────────────────────────────────────────────────

def segment_slides_text(
    slide_texts: list[str],
    srt_contents: list[tuple[int, str]],
    progress_cb=None,
) -> list[dict]:
    """
    Text-only fallback for segment_slides (used when LibreOffice is unavailable).
    Sends extracted slide text instead of images.
    """
    n_slides = len(slide_texts)
    n_segs   = len(srt_contents)

    if progress_cb:
        progress_cb(f"Sending {n_slides} slides (text) and {n_segs} segments to Claude…")

    srt_block = "\n\n".join(
        f"=== SEGMENT {num} ===\n{text}" for num, text in srt_contents
    )
    slides_block = "\n\n".join(
        f"--- Slide {i+1} ---\n{txt if txt.strip() else '(no text)'}"
        for i, txt in enumerate(slide_texts)
    )

    prompt = (
        f"You are given a lecture slide deck ({n_slides} slides, text extracted) "
        f"and {n_segs} transcript segments.\n\n"
        f"Assign a slide range to each segment.\n\n"
        f"Constraints:\n"
        f"• Segments are in order; segment 1 first, segment {n_segs} last.\n"
        f"• first_slide of segment 1 must be 1.\n"
        f"• last_slide of segment {n_segs} must be {n_slides}.\n"
        f"• first_slide[i+1] must equal last_slide[i] or last_slide[i]+1 "
        f"(a 1-slide overlap is allowed when the same slide bridges two topics).\n"
        f"• No gaps: every slide must appear in at least one segment.\n\n"
        f"TRANSCRIPT SEGMENTS:\n\n{srt_block}\n\n"
        f"SLIDE CONTENT:\n\n{slides_block}\n\n"
        f"Output ONLY valid JSON — no markdown, no explanation:\n"
        f'[{{"segment": 1, "first_slide": 1, "last_slide": N}}, ...]'
    )

    response = _client().messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    m = re.search(r"\[[\s\S]*\]", raw)
    if not m:
        raise ValueError(f"Could not find JSON array in LLM response:\n{raw[:500]}")
    return fix_segment_boundaries(json.loads(m.group()), n_slides)


def segment_slides(
    slide_images: list[bytes],
    srt_contents: list[tuple[int, str]],   # [(segment_num, full_text), …]
    progress_cb=None,                      # optional callable(msg: str)
) -> list[dict]:
    """
    Ask Claude to assign slide ranges to each SRT segment.

    Returns a list like:
      [{"segment": 1, "first_slide": 1, "last_slide": 4}, …]

    The returned list is guaranteed to be:
      - sorted by segment
      - contiguous (validated by fix_segment_boundaries)
      - first_slide[0] == 1, last_slide[-1] == len(slide_images)
    """
    n_slides = len(slide_images)
    n_segs = len(srt_contents)

    if progress_cb:
        progress_cb(f"Sending {n_slides} slides and {n_segs} segments to Claude…")

    # Build the SRT context block
    srt_block = "\n\n".join(
        f"=== SEGMENT {num} ===\n{text}" for num, text in srt_contents
    )

    intro = _text_block(
        f"You are given a lecture slide deck ({n_slides} slides, shown below as images) "
        f"and {n_segs} transcript segments for a recorded lecture.\n\n"
        f"Your task: assign a slide range to each segment.\n\n"
        f"Constraints:\n"
        f"• Segments are in order: segment 1 first, segment {n_segs} last.\n"
        f"• first_slide of segment 1 must be 1.\n"
        f"• last_slide of segment {n_segs} must be {n_slides}.\n"
        f"• first_slide[i+1] must equal last_slide[i] or last_slide[i]+1 "
        f"(a 1-slide overlap between adjacent segments is allowed when the same "
        f"slide bridges two topics).\n"
        f"• No gaps: every slide must appear in at least one segment.\n\n"
        f"Transcript segments:\n\n{srt_block}\n\n"
        f"Slides follow (numbered 1–{n_slides}). "
        f"Match slide content to what is being discussed in each segment.\n\n"
        f"Output ONLY valid JSON — no markdown, no explanation:\n"
        f'[{{"segment": 1, "first_slide": 1, "last_slide": N}}, ...]'
    )

    content = [intro]
    for i, img in enumerate(slide_images):
        content.append(_text_block(f"Slide {i + 1}:"))
        content.append(_img_block(img))

    response = _client().messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
    )

    raw = response.content[0].text.strip()

    # Extract JSON array
    m = re.search(r"\[[\s\S]*\]", raw)
    if not m:
        raise ValueError(f"Could not find JSON array in LLM response:\n{raw[:500]}")

    segments = json.loads(m.group())
    return fix_segment_boundaries(segments, n_slides)


# ── boundary fixing ────────────────────────────────────────────────────────────

def fix_segment_boundaries(segments: list[dict], total_slides: int) -> list[dict]:
    """
    Validate and repair segment boundaries.

    Allowed transitions between segment i and i+1:
      • first_slide[i+1] == last_slide[i]     (1-slide overlap)
      • first_slide[i+1] == last_slide[i] + 1 (no overlap / contiguous)

    Any gap (first_slide[i+1] > last_slide[i]+1) or excessive overlap
    (first_slide[i+1] < last_slide[i]) is corrected by setting
    first_slide[i+1] = last_slide[i] (prefer overlap on ambiguous boundaries).
    """
    if not segments:
        return segments

    segments = sorted(segments, key=lambda x: x["segment"])

    # Force first segment to start at slide 1
    segments[0]["first_slide"] = 1

    # Repair each boundary: allow overlap of exactly 0 or 1
    for i in range(1, len(segments)):
        prev_last = segments[i - 1]["last_slide"]
        cur_first = segments[i]["first_slide"]

        if cur_first < prev_last:
            # More than 1-slide overlap → clamp to 1-slide overlap
            segments[i]["first_slide"] = prev_last
        elif cur_first > prev_last + 1:
            # Gap between segments → close it (no overlap)
            segments[i]["first_slide"] = prev_last + 1

    # Force last segment to end at total_slides
    segments[-1]["last_slide"] = total_slides

    # Ensure first_slide <= last_slide in every segment
    for seg in segments:
        if seg["first_slide"] > seg["last_slide"]:
            seg["last_slide"] = seg["first_slide"]

    return segments

"""
FastAPI backend for the Slide Deck Segmentation Tool.

Endpoints
---------
GET  /                         → serve index.html
POST /api/scan                 → scan deck + SRT folders, return lecture list
POST /api/process              → start segmentation job, return job_id
GET  /api/progress/{job_id}    → SSE stream of progress events
GET  /api/health               → {"status": "ok"}
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── local imports ──────────────────────────────────────────────────────────────
from file_matcher import LectureInfo, SegmentInfo, scan_inputs
from srt_parser import parse_srt
from slide_renderer import render_slides, libreoffice_available, extract_slide_texts, slide_count
from llm_client import (
    detect_cover_slide, detect_cover_slide_text,
    segment_slides, segment_slides_text, fix_segment_boundaries,
)
from deck_splitter import split_deck

# ── app setup ──────────────────────────────────────────────────────────────────

# Locate the static directory regardless of working directory or build layout.
# Walk upward from this file and also check alongside it.
def _find_static() -> Path:
    candidates = [
        Path(__file__).parent / "static",           # Docker: /app/static
        Path(__file__).parent.parent / "static",    # local dev: project/static
    ]
    for p in candidates:
        if p.is_dir():
            return p
    # Last resort: return the first candidate and let FastAPI report a clear error
    return candidates[0]

STATIC_DIR = _find_static()

app = FastAPI(title="Slide Deck Segmenter")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "libreoffice": libreoffice_available(),
    }


# ── /api/browse ────────────────────────────────────────────────────────────────

@app.get("/api/browse")
async def api_browse(path: str = ""):
    """
    List directory contents for the file browser.
    Returns the current path, parent path, and a sorted list of entries.
    """
    import os

    # Default to home directory
    if not path:
        path = str(Path.home())

    target = Path(path).expanduser().resolve()

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")
    if not target.is_dir():
        # Caller passed a file path — browse its parent
        target = target.parent

    try:
        raw_entries = list(target.iterdir())
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    entries = []
    for p in sorted(raw_entries, key=lambda x: (not x.is_dir(), x.name.lower())):
        if p.name.startswith("."):
            continue   # hide hidden files
        ext = p.suffix.lower()
        if p.is_dir():
            kind = "dir"
        elif ext in {".pptx", ".pdf"}:
            kind = "deck"
        elif ext == ".srt":
            kind = "srt"
        else:
            continue   # skip other files

        entries.append({
            "name": p.name,
            "type": kind,
            "path": str(p),
        })

    parent = str(target.parent) if target != target.parent else None

    return {
        "current": str(target),
        "parent": parent,
        "entries": entries,
    }


# ── in-memory job store ────────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}   # job_id → {"events": [], "done": bool, "error": str|None}


def _make_job() -> tuple[str, dict]:
    jid = str(uuid.uuid4())
    job: dict = {"events": [], "done": False, "error": None, "output_files": []}
    _jobs[jid] = job
    return jid, job


def _push(job: dict, msg: str, level: str = "info"):
    event = {"time": time.strftime("%H:%M:%S"), "level": level, "msg": msg}
    job["events"].append(event)


# ── /api/scan ──────────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    deck_path: str
    srt_folder: str


class SegmentOut(BaseModel):
    segment_num: int
    srt_name: str


class LectureOut(BaseModel):
    lecture_num: int
    deck_stem: str
    deck_format: str
    segments: list[SegmentOut]


@app.post("/api/scan")
async def api_scan(req: ScanRequest) -> list[LectureOut]:
    try:
        lectures = scan_inputs(req.deck_path, req.srt_folder)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return [
        LectureOut(
            lecture_num=lec.lecture_num,
            deck_stem=lec.deck_stem,
            deck_format=lec.deck_format,
            segments=[
                SegmentOut(segment_num=s.segment_num, srt_name=s.srt_name)
                for s in lec.segments
            ],
        )
        for lec in lectures
    ]


# ── /api/process ───────────────────────────────────────────────────────────────

class LectureConfig(BaseModel):
    lecture_num: int
    include: bool = True
    cover_mode: str = "auto"   # "yes" | "no" | "auto"


class ProcessRequest(BaseModel):
    deck_path: str
    srt_folder: str
    output_folder: str
    lectures: list[LectureConfig]


@app.post("/api/process")
async def api_process(req: ProcessRequest):
    jid, job = _make_job()
    thread = threading.Thread(target=_run_job, args=(jid, job, req), daemon=True)
    thread.start()
    return {"job_id": jid}


# ── /api/progress/{job_id} ────────────────────────────────────────────────────

@app.get("/api/progress/{job_id}")
async def api_progress(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_stream() -> AsyncGenerator[str, None]:
        seen = 0
        job = _jobs[job_id]
        while True:
            events = job["events"]
            while seen < len(events):
                evt = events[seen]
                yield f"data: {json.dumps(evt)}\n\n"
                seen += 1
            if job["done"]:
                final = {
                    "time": time.strftime("%H:%M:%S"),
                    "level": "done",
                    "msg": "Processing complete.",
                    "output_files": job["output_files"],
                    "error": job["error"],
                }
                yield f"data: {json.dumps(final)}\n\n"
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── background worker ──────────────────────────────────────────────────────────

def _run_job(jid: str, job: dict, req: ProcessRequest):
    try:
        _process(job, req)
    except Exception:
        err = traceback.format_exc()
        _push(job, f"Fatal error:\n{err}", "error")
        job["error"] = err
    finally:
        job["done"] = True


def _process(job: dict, req: ProcessRequest):
    push = lambda msg, lvl="info": _push(job, msg, lvl)

    # --- scan files ---
    push("Scanning input files…")
    try:
        lectures = scan_inputs(req.deck_path, req.srt_folder)
    except ValueError as exc:
        push(str(exc), "error")
        job["error"] = str(exc)
        return

    # filter to selected lectures
    selected_nums = {lc.lecture_num for lc in req.lectures if lc.include}
    cover_modes   = {lc.lecture_num: lc.cover_mode for lc in req.lectures}
    lectures = [l for l in lectures if l.lecture_num in selected_nums]

    if not lectures:
        push("No lectures selected.", "error")
        return

    # prepare output directory
    out_root = Path(req.output_folder)
    out_root.mkdir(parents=True, exist_ok=True)

    multi_lecture = len(lectures) > 1

    for lec in lectures:
        _process_lecture(job, push, lec, cover_modes.get(lec.lecture_num, "auto"),
                         out_root, multi_lecture)

    push(f"All done! Output saved to: {out_root}", "success")


def _process_lecture(
    job: dict,
    push,
    lec: LectureInfo,
    cover_mode: str,
    out_root: Path,
    multi_lecture: bool,
):
    push(f"── Lecture {lec.lecture_num}: {lec.deck_stem} ──")

    # output directory
    if multi_lecture:
        lec_out = out_root / f"L{lec.lecture_num}"
        lec_out.mkdir(parents=True, exist_ok=True)
    else:
        lec_out = out_root

    # --- render slides (images if LibreOffice available, else text) ---
    use_images = libreoffice_available() or lec.deck_format == "pdf"
    slide_images: list[bytes] = []
    slide_texts: list[str] = []

    if use_images:
        push(f"Rendering slides from {Path(lec.deck_path).name}…")
        try:
            slide_images = render_slides(lec.deck_path)
            n_slides = len(slide_images)
            push(f"  {n_slides} slides rendered as images.")
        except RuntimeError as exc:
            push(str(exc), "error")
            job["error"] = str(exc)
            return
    else:
        push(f"LibreOffice not found — extracting slide text from {Path(lec.deck_path).name}…")
        push("  (Install LibreOffice for image-based analysis, which is more accurate.)", "warn")
        try:
            slide_texts = extract_slide_texts(lec.deck_path)
            n_slides = len(slide_texts)
            push(f"  {n_slides} slides extracted as text.")
        except Exception as exc:
            push(f"Text extraction failed: {exc}", "error")
            job["error"] = str(exc)
            return

    # --- cover slide decision ---
    use_cover: bool = False

    if cover_mode == "yes":
        use_cover = True
        push("Cover slide: enabled (manual).")
    elif cover_mode == "no":
        use_cover = False
        push("Cover slide: disabled (manual).")
    else:  # auto
        push("Auto-detecting cover slide via Claude…")
        try:
            if use_images:
                use_cover = detect_cover_slide(slide_images[0])
            else:
                use_cover = detect_cover_slide_text(slide_texts[0] if slide_texts else "")
            push(f"  Cover slide auto-detected: {'yes' if use_cover else 'no'}.")
        except Exception as exc:
            push(f"  Cover detection failed ({exc}); defaulting to no cover.", "warn")
            use_cover = False

    # --- read SRT files ---
    push("Reading SRT transcripts…")
    srt_contents: list[tuple[int, str]] = []
    for seg in lec.segments:
        text = parse_srt(seg.srt_path)
        srt_contents.append((seg.segment_num, text))
        push(f"  Segment {seg.segment_num}: {seg.srt_name} ({len(text)} chars)")

    # --- call LLM for segmentation ---
    push("Calling Claude to segment slides…")
    try:
        if use_images:
            segments = segment_slides(
                slide_images, srt_contents,
                progress_cb=lambda m: push(f"  {m}"),
            )
        else:
            segments = segment_slides_text(
                slide_texts, srt_contents,
                progress_cb=lambda m: push(f"  {m}"),
            )
    except Exception as exc:
        push(f"Segmentation failed: {exc}", "error")
        job["error"] = str(exc)
        return

    push("Segmentation result:")
    for seg_info in segments:
        push(
            f"  Segment {seg_info['segment']}: "
            f"slides {seg_info['first_slide']}–{seg_info['last_slide']}"
        )

    # --- split deck ---
    push("Splitting deck…")
    ext = f".{lec.deck_format}"

    for seg_info in segments:
        seg_num   = seg_info["segment"]
        first_s   = seg_info["first_slide"]
        last_s    = seg_info["last_slide"]
        out_name  = f"{lec.deck_stem}_S{seg_num}{ext}"
        out_path  = lec_out / out_name

        push(f"  Writing {out_name} (slides {first_s}–{last_s})…")
        try:
            split_deck(
                src_path=lec.deck_path,
                dst_path=out_path,
                first_slide=first_s,
                last_slide=last_s,
                cover_slide=bool(use_cover),
            )
            job["output_files"].append(str(out_path))
        except Exception as exc:
            push(f"  Failed to write {out_name}: {exc}", "error")

    push(f"Lecture {lec.lecture_num} done. {len(segments)} files written.", "success")


# ── run ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)

"""
FastAPI backend for the Slide Deck Segmentation Tool.

Endpoints
---------
GET  /                          → serve index.html
GET  /api/health                → {"status": "ok", "libreoffice": bool}
GET  /api/browse                → file browser (local mode only)
POST /api/scan                  → scan deck + SRT folders by path (local mode)
POST /api/upload-and-scan       → upload files + scan (remote mode)
POST /api/process               → start segmentation job, return job_id
GET  /api/progress/{job_id}     → SSE stream of progress events
GET  /api/download/{job_id}     → download results as ZIP (remote mode)
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
import traceback
import uuid
import zipfile as zipfile_mod
from io import BytesIO
from pathlib import Path
from typing import AsyncGenerator

import aiofiles
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── local imports ──────────────────────────────────────────────────────────────
from file_matcher import LectureInfo, scan_inputs
from srt_parser import parse_srt
from slide_renderer import render_slides, libreoffice_available, extract_slide_texts
from llm_client import (
    detect_cover_slide, detect_cover_slide_text,
    segment_slides, segment_slides_text,
)
from deck_splitter import split_deck

# ── app setup ──────────────────────────────────────────────────────────────────

def _find_static() -> Path:
    candidates = [
        Path(__file__).parent / "static",
        Path(__file__).parent.parent / "static",
    ]
    for p in candidates:
        if p.is_dir():
            return p
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
    return {"status": "ok", "libreoffice": libreoffice_available()}


# ── /api/browse  (local mode) ──────────────────────────────────────────────────

@app.get("/api/browse")
async def api_browse(path: str = ""):
    if not path:
        path = str(Path.home())
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")
    if not target.is_dir():
        target = target.parent
    try:
        raw_entries = list(target.iterdir())
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    entries = []
    for p in sorted(raw_entries, key=lambda x: (not x.is_dir(), x.name.lower())):
        if p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if p.is_dir():
            kind = "dir"
        elif ext in {".pptx", ".pdf"}:
            kind = "deck"
        elif ext == ".srt":
            kind = "srt"
        else:
            continue
        entries.append({"name": p.name, "type": kind, "path": str(p)})

    parent = str(target.parent) if target != target.parent else None
    return {"current": str(target), "parent": parent, "entries": entries}


# ── shared models ──────────────────────────────────────────────────────────────

class SegmentOut(BaseModel):
    segment_num: int
    srt_name: str

class LectureOut(BaseModel):
    lecture_num: int
    deck_stem: str
    deck_format: str
    segments: list[SegmentOut]

class LectureConfig(BaseModel):
    lecture_num: int
    include: bool = True
    cover_mode: str = "auto"   # "yes" | "no" | "auto"

def _lectures_to_out(lectures: list[LectureInfo]) -> list[LectureOut]:
    return [
        LectureOut(
            lecture_num=lec.lecture_num,
            deck_stem=lec.deck_stem,
            deck_format=lec.deck_format,
            segments=[SegmentOut(segment_num=s.segment_num, srt_name=s.srt_name)
                      for s in lec.segments],
        )
        for lec in lectures
    ]


# ── /api/scan  (local mode) ────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    deck_path: str
    srt_folder: str

@app.post("/api/scan")
async def api_scan(req: ScanRequest) -> list[LectureOut]:
    try:
        return _lectures_to_out(scan_inputs(req.deck_path, req.srt_folder))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── /api/upload-and-scan  (remote mode) ───────────────────────────────────────

_sessions: dict[str, dict] = {}   # session_id → {deck_dir, srt_dir, out_dir}

@app.post("/api/upload-and-scan")
async def api_upload_and_scan(
    decks: list[UploadFile] = File(...),
    srts:  list[UploadFile] = File(...),
):
    session_id = str(uuid.uuid4())
    base = Path(tempfile.gettempdir()) / "segmenter" / session_id
    deck_dir = base / "decks"
    srt_dir  = base / "srts"
    out_dir  = base / "output"
    for d in (deck_dir, srt_dir, out_dir):
        d.mkdir(parents=True)

    for f in decks:
        dest = deck_dir / Path(f.filename).name
        async with aiofiles.open(dest, "wb") as fh:
            while chunk := await f.read(1024 * 1024):
                await fh.write(chunk)

    for f in srts:
        dest = srt_dir / Path(f.filename).name
        async with aiofiles.open(dest, "wb") as fh:
            await fh.write(await f.read())

    try:
        lectures = scan_inputs(str(deck_dir), str(srt_dir))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    _sessions[session_id] = {
        "deck_dir": str(deck_dir),
        "srt_dir":  str(srt_dir),
        "out_dir":  str(out_dir),
    }
    return {"session_id": session_id, "lectures": _lectures_to_out(lectures)}


# ── /api/process ───────────────────────────────────────────────────────────────

class ProcessRequest(BaseModel):
    # local mode (provide all three paths)
    deck_path:     str = ""
    srt_folder:    str = ""
    output_folder: str = ""
    # remote mode (provide session_id from /api/upload-and-scan)
    session_id:    str = ""
    # common
    lectures: list[LectureConfig]

@app.post("/api/process")
async def api_process(req: ProcessRequest):
    jid, job = _make_job()
    threading.Thread(target=_run_job, args=(jid, job, req), daemon=True).start()
    return {"job_id": jid}


# ── /api/progress/{job_id} ────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}

def _make_job() -> tuple[str, dict]:
    jid = str(uuid.uuid4())
    job: dict = {"events": [], "done": False, "error": None, "output_files": []}
    _jobs[jid] = job
    return jid, job

def _push(job: dict, msg: str, level: str = "info"):
    job["events"].append({"time": time.strftime("%H:%M:%S"), "level": level, "msg": msg})

@app.get("/api/progress/{job_id}")
async def api_progress(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_stream() -> AsyncGenerator[str, None]:
        seen = 0
        job = _jobs[job_id]
        while True:
            while seen < len(job["events"]):
                yield f"data: {json.dumps(job['events'][seen])}\n\n"
                seen += 1
            if job["done"]:
                yield f"data: {json.dumps({'time': time.strftime('%H:%M:%S'), 'level': 'done', 'msg': 'Processing complete.', 'output_files': job['output_files'], 'error': job['error']})}\n\n"
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── /api/download/{job_id}  (remote mode) ─────────────────────────────────────

@app.get("/api/download/{job_id}")
async def api_download(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job["done"]:
        raise HTTPException(status_code=400, detail="Job not finished yet")

    files = [Path(f) for f in job["output_files"] if Path(f).exists()]
    if not files:
        raise HTTPException(status_code=404, detail="No output files found")

    # Find common output root to build relative paths inside the ZIP
    try:
        common = Path(os.path.commonpath([str(f) for f in files]))
        if common.is_file():
            common = common.parent
    except ValueError:
        common = files[0].parent

    buf = BytesIO()
    with zipfile_mod.ZipFile(buf, "w", zipfile_mod.ZIP_DEFLATED) as zf:
        for f in files:
            try:
                arcname = f.relative_to(common)
            except ValueError:
                arcname = f.name
            zf.write(f, arcname)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="segments.zip"'},
    )


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

    # --- resolve paths (local vs remote) ---
    if req.session_id:
        session = _sessions.get(req.session_id)
        if not session:
            push("Session not found or expired.", "error")
            return
        deck_path  = session["deck_dir"]
        srt_folder = session["srt_dir"]
        out_root   = Path(session["out_dir"])
    else:
        deck_path  = req.deck_path
        srt_folder = req.srt_folder
        out_root   = Path(req.output_folder)

    push("Scanning input files…")
    try:
        lectures = scan_inputs(deck_path, srt_folder)
    except ValueError as exc:
        push(str(exc), "error")
        job["error"] = str(exc)
        return

    selected_nums = {lc.lecture_num for lc in req.lectures if lc.include}
    cover_modes   = {lc.lecture_num: lc.cover_mode for lc in req.lectures}
    lectures = [l for l in lectures if l.lecture_num in selected_nums]

    if not lectures:
        push("No lectures selected.", "error")
        return

    out_root.mkdir(parents=True, exist_ok=True)
    multi_lecture = len(lectures) > 1

    for lec in lectures:
        _process_lecture(job, push, lec, cover_modes.get(lec.lecture_num, "auto"),
                         out_root, multi_lecture)

    push(f"All done! {len(job['output_files'])} file(s) ready.", "success")


def _process_lecture(job, push, lec: LectureInfo, cover_mode: str,
                     out_root: Path, multi_lecture: bool):
    push(f"── Lecture {lec.lecture_num}: {lec.deck_stem} ──")

    lec_out = (out_root / f"L{lec.lecture_num}") if multi_lecture else out_root
    lec_out.mkdir(parents=True, exist_ok=True)

    # --- render ---
    use_images = libreoffice_available() or lec.deck_format == "pdf"
    slide_images: list[bytes] = []
    slide_texts:  list[str]   = []

    if use_images:
        push(f"Rendering slides from {Path(lec.deck_path).name}…")
        try:
            slide_images = render_slides(lec.deck_path)
            push(f"  {len(slide_images)} slides rendered as images.")
        except RuntimeError as exc:
            push(str(exc), "error"); job["error"] = str(exc); return
    else:
        push(f"Extracting slide text from {Path(lec.deck_path).name}…")
        push("  (Install LibreOffice for image-based analysis, which is more accurate.)", "warn")
        try:
            slide_texts = extract_slide_texts(lec.deck_path)
            push(f"  {len(slide_texts)} slides extracted as text.")
        except Exception as exc:
            push(f"Text extraction failed: {exc}", "error"); job["error"] = str(exc); return

    # --- cover slide ---
    use_cover = False
    if cover_mode == "yes":
        use_cover = True; push("Cover slide: enabled (manual).")
    elif cover_mode == "no":
        push("Cover slide: disabled (manual).")
    else:
        push("Auto-detecting cover slide via Claude…")
        try:
            use_cover = (detect_cover_slide(slide_images[0]) if use_images
                         else detect_cover_slide_text(slide_texts[0] if slide_texts else ""))
            push(f"  Cover slide auto-detected: {'yes' if use_cover else 'no'}.")
        except Exception as exc:
            push(f"  Cover detection failed ({exc}); defaulting to no cover.", "warn")

    # --- SRTs ---
    push("Reading SRT transcripts…")
    srt_contents: list[tuple[int, str]] = []
    for seg in lec.segments:
        text = parse_srt(seg.srt_path)
        srt_contents.append((seg.segment_num, text))
        push(f"  Segment {seg.segment_num}: {seg.srt_name} ({len(text)} chars)")

    # --- segment ---
    push("Calling Claude to segment slides…")
    try:
        segments = (
            segment_slides(slide_images, srt_contents, progress_cb=lambda m: push(f"  {m}"))
            if use_images else
            segment_slides_text(slide_texts, srt_contents, progress_cb=lambda m: push(f"  {m}"))
        )
    except Exception as exc:
        push(f"Segmentation failed: {exc}", "error"); job["error"] = str(exc); return

    push("Segmentation result:")
    for s in segments:
        push(f"  Segment {s['segment']}: slides {s['first_slide']}–{s['last_slide']}")

    # --- split ---
    push("Splitting deck…")
    ext = f".{lec.deck_format}"
    for s in segments:
        name     = f"{lec.deck_stem}_S{s['segment']}{ext}"
        out_path = lec_out / name
        push(f"  Writing {name} (slides {s['first_slide']}–{s['last_slide']})…")
        try:
            split_deck(lec.deck_path, out_path, s["first_slide"], s["last_slide"], bool(use_cover))
            job["output_files"].append(str(out_path))
        except Exception as exc:
            push(f"  Failed to write {name}: {exc}", "error")

    push(f"Lecture {lec.lecture_num} done. {len(segments)} files written.", "success")


# ── run ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8080, reload=False)

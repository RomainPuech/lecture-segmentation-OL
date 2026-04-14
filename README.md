# Slide Deck Segmenter

Automatically split a lecture slide deck into per-segment slide decks that
correspond to SRT transcript segments, using Claude AI to determine which
slides belong to which segment.

---

## Quick start — Docker (recommended)

Docker bundles LibreOffice automatically, so PPTX slides are rendered as
images for the best AI analysis quality.

```bash
docker compose up --build
```

Then open **http://localhost:8000** in your browser.

> **First build** downloads LibreOffice (~500 MB) and Python packages; expect
> 3–5 min. Subsequent starts are instant.

### Stopping

```bash
docker compose down
```

---

## Quick start — local Python

```bash
python run.py        # installs deps, then starts the server
```

Then open **http://127.0.0.1:8000**.

### Local requirements

| Dependency | Purpose |
|---|---|
| **Python 3.10+** | Runtime |
| **LibreOffice** | Render PPTX slides as images (for best quality) |

Without LibreOffice, the app falls back to extracting text from PPTX slides
(less accurate for segmentation).

**Installing LibreOffice:**
- macOS — <https://www.libreoffice.org/download/> (drag `.app` to Applications)
- Linux — `sudo apt install libreoffice-impress`
- Windows — <https://www.libreoffice.org/download/>

---

## API key

The Anthropic API key is read from `.env` in the project root:

```
ANTHROPIC_API_KEY=sk-ant-...
```

This file is automatically passed into the Docker container via `docker-compose.yml`.

---

## Input conventions

### Deck files
- Formats: `.pptx`, `.pdf`
- File names must contain `L<N>` (e.g. `APM_L1_AI and Precision Medicine.pptx`)
- If both `.pptx` and `.pdf` exist for the same lecture, `.pptx` is used.

### SRT files
- Must be in a single flat folder.
- Names must contain `L<N>_S<M>` (e.g. `APM_L1_S3.srt`, `APM_L1_S3_v2.srt`)
- When multiple SRTs match the same lecture + segment, the one with the
  **latest modification time** is used.

---

## Output

Each output file is named after the source deck with `_S<N>` appended:

```
APM_L1_AI and Precision Medicine_S1.pptx
APM_L1_AI and Precision Medicine_S2.pptx
...
```

For multi-lecture input, output is grouped into `L<N>/` sub-folders.

---

## Cover slide

| Mode | Behaviour |
|---|---|
| **Auto** | Claude inspects slide 1; if it is a title/logo-only slide it is prepended to every segment. |
| **Always** | Slide 1 is always duplicated and prepended. |
| **Never** | No cover slide is added. |

---

## Segmentation rules

- Segments are strictly ordered; every slide appears in at least one segment.
- Adjacent segments may share **one** slide (1-slide overlap at the boundary).
- Slide ranges are validated and corrected if the model output violates these rules.

---

## File browser

The UI includes a built-in file browser that navigates the host filesystem
(mounted into the container at the same path, so paths are identical).

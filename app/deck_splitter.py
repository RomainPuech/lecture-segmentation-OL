"""
Split PPTX and PDF files into sub-decks containing only the specified slides.

PPTX splitting is done via direct ZIP/XML manipulation so that all slide
content (images, fonts, themes, layouts) is preserved exactly.

slide_indices is always a 1-based list in output order; duplicates are
allowed (used to prepend a cover slide copy).
"""

from __future__ import annotations

import copy
import re
import zipfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter

# ── XML namespaces ─────────────────────────────────────────────────────────────

_P_NS    = "http://schemas.openxmlformats.org/presentationml/2006/main"
_R_NS    = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CT_NS   = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_SLIDE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
_CT_SLIDE  = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"


# ── helpers ────────────────────────────────────────────────────────────────────

def _rels_path(slide_target: str) -> str:
    """'slides/slide3.xml'  →  'slides/_rels/slide3.xml.rels'"""
    if "/" in slide_target:
        head, tail = slide_target.rsplit("/", 1)
        return f"{head}/_rels/{tail}.rels"
    return f"_rels/{slide_target}.rels"


def _parse_xml(data: bytes):
    from lxml import etree
    return etree.fromstring(data)


def _serialize_xml(root) -> bytes:
    from lxml import etree
    return etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )


# ── PPTX splitter ──────────────────────────────────────────────────────────────

def split_pptx(src_path: str | Path, dst_path: str | Path, slide_indices: list[int]) -> None:
    """
    Write a new PPTX to *dst_path* containing exactly the slides listed in
    *slide_indices* (1-based, ordered, duplicates allowed for cover slide).
    """
    src_path = str(src_path)
    dst_path = str(dst_path)

    with zipfile.ZipFile(src_path, "r") as src_zip:
        all_names = set(src_zip.namelist())

        # --- parse core XML ---
        prs_el      = _parse_xml(src_zip.read("ppt/presentation.xml"))
        prs_rels_el = _parse_xml(src_zip.read("ppt/_rels/presentation.xml.rels"))
        ct_el       = _parse_xml(src_zip.read("[Content_Types].xml"))

        # slide ID list in presentation.xml
        sld_id_lst = prs_el.find(f"{{{_P_NS}}}sldIdLst")
        if sld_id_lst is None:
            raise ValueError("presentation.xml has no sldIdLst element.")

        # rId → target (e.g. "slides/slide3.xml")
        rid_to_target: dict[str, str] = {}
        for rel in prs_rels_el:
            if rel.get("Type") == _REL_SLIDE:
                rid_to_target[rel.get("Id", "")] = rel.get("Target", "")

        # ordered list of (rId, target) matching original slide order
        orig_slides: list[tuple[str, str]] = []
        for sld_el in sld_id_lst:
            rid = sld_el.get(f"{{{_R_NS}}}id", "")
            orig_slides.append((rid, rid_to_target.get(rid, "")))

        # files to skip during bulk copy
        skip: set[str] = {
            "ppt/presentation.xml",
            "ppt/_rels/presentation.xml.rels",
            "[Content_Types].xml",
        }
        for _, tgt in orig_slides:
            if tgt:
                skip.add(f"ppt/{tgt}")
                skip.add(f"ppt/{_rels_path(tgt)}")

        # template elements (for namespace-safe element creation)
        tmpl_sld_id   = next(iter(sld_id_lst), None)
        tmpl_rel      = next(iter(prs_rels_el), None)
        tmpl_override = next(
            (e for e in ct_el if e.get("ContentType") == _CT_SLIDE), None
        )

        max_id = max(
            (int(e.get("id", 0)) for e in sld_id_lst if e.get("id", "").isdigit()),
            default=255,
        )

        with zipfile.ZipFile(dst_path, "w", zipfile.ZIP_DEFLATED) as dst_zip:

            # 1. copy every non-slide file unchanged
            for name in all_names:
                if name not in skip:
                    dst_zip.writestr(name, src_zip.read(name))

            # 2. add selected slides (possibly renamed)
            new_entries: list[tuple[str, str, str]] = []   # (rId, target, slide_id)

            for out_idx, orig_1based in enumerate(slide_indices):
                orig_0based = orig_1based - 1
                if not (0 <= orig_0based < len(orig_slides)):
                    continue
                _, orig_target = orig_slides[orig_0based]
                if not orig_target:
                    continue

                out_num      = out_idx + 1
                new_target   = f"slides/slide{out_num}.xml"
                new_rid      = f"rId{out_num + 500}"
                new_slide_id = str(max_id + out_num)

                orig_ppt      = f"ppt/{orig_target}"
                orig_rels_ppt = f"ppt/{_rels_path(orig_target)}"
                new_ppt       = f"ppt/{new_target}"
                new_rels_ppt  = f"ppt/{_rels_path(new_target)}"

                if orig_ppt in all_names:
                    dst_zip.writestr(new_ppt, src_zip.read(orig_ppt))
                if orig_rels_ppt in all_names:
                    dst_zip.writestr(new_rels_ppt, src_zip.read(orig_rels_ppt))

                new_entries.append((new_rid, new_target, new_slide_id))

            # 3. rebuild presentation.xml sldIdLst
            for child in list(sld_id_lst):
                sld_id_lst.remove(child)
            for new_rid, _, new_slide_id in new_entries:
                if tmpl_sld_id is not None:
                    el = copy.deepcopy(tmpl_sld_id)
                else:
                    from lxml import etree
                    el = etree.SubElement(sld_id_lst, f"{{{_P_NS}}}sldId")
                el.set("id", new_slide_id)
                el.set(f"{{{_R_NS}}}id", new_rid)
                sld_id_lst.append(el)
            dst_zip.writestr("ppt/presentation.xml", _serialize_xml(prs_el))

            # 4. rebuild presentation.xml.rels (remove old slide rels, add new)
            for rel in list(prs_rels_el):
                if rel.get("Type") == _REL_SLIDE:
                    prs_rels_el.remove(rel)
            for new_rid, new_target, _ in new_entries:
                if tmpl_rel is not None:
                    el = copy.deepcopy(tmpl_rel)
                else:
                    from lxml import etree
                    _PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
                    el = etree.SubElement(prs_rels_el, f"{{{_PKG_NS}}}Relationship")
                el.set("Id", new_rid)
                el.set("Type", _REL_SLIDE)
                el.set("Target", new_target)
                prs_rels_el.append(el)
            dst_zip.writestr("ppt/_rels/presentation.xml.rels", _serialize_xml(prs_rels_el))

            # 5. rebuild [Content_Types].xml slide overrides
            for ov in list(ct_el):
                if ov.get("ContentType") == _CT_SLIDE:
                    ct_el.remove(ov)
            for _, new_target, _ in new_entries:
                if tmpl_override is not None:
                    el = copy.deepcopy(tmpl_override)
                else:
                    from lxml import etree
                    el = etree.SubElement(ct_el, f"{{{_CT_NS}}}Override")
                el.set("PartName", f"/ppt/{new_target}")
                el.set("ContentType", _CT_SLIDE)
                ct_el.append(el)
            dst_zip.writestr("[Content_Types].xml", _serialize_xml(ct_el))


# ── PDF splitter ───────────────────────────────────────────────────────────────

def split_pdf(src_path: str | Path, dst_path: str | Path, slide_indices: list[int]) -> None:
    """
    Write a new PDF to *dst_path* containing exactly the pages listed in
    *slide_indices* (1-based, ordered, duplicates allowed).
    """
    reader = PdfReader(str(src_path))
    writer = PdfWriter()
    total = len(reader.pages)
    for idx_1based in slide_indices:
        page_idx = idx_1based - 1
        if 0 <= page_idx < total:
            writer.add_page(reader.pages[page_idx])
    with open(str(dst_path), "wb") as f:
        writer.write(f)


# ── unified entry point ────────────────────────────────────────────────────────

def split_deck(
    src_path: str | Path,
    dst_path: str | Path,
    first_slide: int,
    last_slide: int,
    cover_slide: bool = False,
) -> None:
    """
    Extract slides [first_slide … last_slide] (1-based, inclusive) from *src_path*
    and save to *dst_path*.

    If *cover_slide* is True, slide 1 is prepended as a duplicate before the
    segment range — except when *first_slide* is already 1: the range already
    opens with the deck's first slide (the cover), so prepending would show it
    twice.
    """
    indices = list(range(first_slide, last_slide + 1))
    if cover_slide and first_slide > 1:
        indices = [1] + indices

    src_path = Path(src_path)
    ext = src_path.suffix.lower()
    if ext == ".pptx":
        split_pptx(src_path, dst_path, indices)
    elif ext == ".pdf":
        split_pdf(src_path, dst_path, indices)
    else:
        raise ValueError(f"Unsupported format: {ext}")

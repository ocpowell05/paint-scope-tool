"""
Drawings path — reads construction DRAWING sheets by rendering them to images
and using Claude vision. Generalized: finds artifacts (room-finish source,
finish legend, door schedule, RCP) by WHAT THEY ARE via the model, never by
sheet number. Built for easy testing:
  - analyze_drawings(..., only_pages=[...]) processes just those pages (fast loop)
  - every result includes "raw" model output so you can see read-vs-parse errors
"""
import os, re, json, io, base64
import fitz  # pymupdf
import anthropic

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# Universal vocabulary — only a cheap prefilter to shortlist candidate sheets.
# The model makes the real type decision, so a missed word here is recoverable.
ARTIFACT_HINTS = [
    "room finish schedule", "finish schedule", "room finish", "finish plan",
    "finish legend", "finish key", "material legend",
    "door schedule", "reflected ceiling", "ceiling plan", "rcp", "a.f.f", "aff",
]

def _client():
    raw = os.environ["ANTHROPIC_API_KEY"]
    return anthropic.Anthropic(api_key="".join(raw.split()))  # strip stray whitespace

def _render(doc, idx, scale=3.0):
    pix = doc[idx].get_pixmap(matrix=fitz.Matrix(scale, scale))
    return pix.tobytes("png")

# Max pixels per side the API accepts.
MAX_PX = 8000

def _render_fit(doc, idx, target=7600):
    """Render the whole sheet as ONE image sized just under the pixel cap.
    Good for classification (big-picture), not for tiny text."""
    rect = doc[idx].rect
    longest = max(rect.width, rect.height)
    scale = max(1.0, min(target / longest, 6.0))
    pix = doc[idx].get_pixmap(matrix=fitz.Matrix(scale, scale))
    return pix.tobytes("png")

def _render_tiles(doc, idx, scale=4.0, cols=2, rows=2, overlap=0.06):
    """Render the sheet at HIGH resolution, sliced into cols x rows tiles so each
    tile stays under the pixel cap while keeping text sharp. Overlap keeps rows
    that fall on a tile boundary readable in at least one tile.
    Returns a list of PNG bytes, reading order left->right, top->bottom."""
    page = doc[idx]
    rect = page.rect
    tw = rect.width / cols
    th = rect.height / rows
    ov_w = tw * overlap
    ov_h = th * overlap
    tiles = []
    for r in range(rows):
        for c in range(cols):
            x0 = max(rect.x0, rect.x0 + c * tw - ov_w)
            y0 = max(rect.y0, rect.y0 + r * th - ov_h)
            x1 = min(rect.x1, rect.x0 + (c + 1) * tw + ov_w)
            y1 = min(rect.y1, rect.y0 + (r + 1) * th + ov_h)
            clip = fitz.Rect(x0, y0, x1, y1)
            # cap scale so this tile doesn't exceed MAX_PX on either side
            tile_long = max(clip.width, clip.height)
            safe_scale = min(scale, (MAX_PX - 100) / tile_long)
            pix = page.get_pixmap(matrix=fitz.Matrix(safe_scale, safe_scale), clip=clip)
            tiles.append(pix.tobytes("png"))
    return tiles

def _img_block(png):
    return {"type": "image", "source": {"type": "base64",
            "media_type": "image/png", "data": base64.b64encode(png).decode()}}

def _ask(png, prompt, max_tokens=3000):
    msg = _client().messages.create(
        model=MODEL, max_tokens=max_tokens,
        messages=[{"role": "user", "content": [_img_block(png),
                                               {"type": "text", "text": prompt}]}])
    return "".join(b.text for b in msg.content if b.type == "text")

def _ask_tiles(tiles, prompt, max_tokens=4000):
    """Send multiple image tiles in one message so the model sees the whole sheet
    sharply. Tiles are labeled by position to help the model reassemble context."""
    content = []
    labels = ["top-left", "top-right", "bottom-left", "bottom-right",
              "tile-5", "tile-6", "tile-7", "tile-8", "tile-9"]
    for n, png in enumerate(tiles):
        lbl = labels[n] if n < len(labels) else f"tile-{n+1}"
        content.append({"type": "text", "text": f"[{lbl} of the sheet]"})
        content.append(_img_block(png))
    content.append({"type": "text", "text": prompt})
    msg = _client().messages.create(
        model=MODEL, max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}])
    return "".join(b.text for b in msg.content if b.type == "text")

def _parse(raw):
    cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        m = re.search(r"[\[{].*[\]}]", cleaned, re.DOTALL)
        if m:
            try: return json.loads(m.group(0))
            except Exception: return None
        return None

# ---- shortlist candidate drawing sheets by universal vocabulary ----
def shortlist_sheets(doc):
    cands = []
    for i in range(doc.page_count):
        up = doc[i].get_text().upper()
        if any(h.upper() in up for h in ARTIFACT_HINTS):
            cands.append(i)
    return cands

# ---- classify a single sheet by what it IS (model decides) ----
CLASSIFY_PROMPT = """Look at this construction drawing sheet. Reply ONLY with JSON:
{"is_room_finish_source": bool,
 "is_finish_legend": bool,
 "is_door_schedule": bool,
 "is_reflected_ceiling_plan": bool,
 "sheet_id": "the sheet number from the title block, e.g. A7.1, or ''",
 "notes": "one short phrase"}
No markdown, no preamble."""

# ---- read the finish legend ----
LEGEND_PROMPT = """This sheet has a finish legend/key. Extract every finish code and meaning.
Return ONLY a JSON array:
[{"code":"PNT-1","category":"paint|floor|base|wall|ceiling|wallcovering|other",
  "description":"","product":"","sheen":""}]
No markdown."""

# ---- read per-room finishes (table OR plan tags) ----
ROOMS_PROMPT = """This sheet shows room finishes (a schedule table OR per-room finish tags on a plan).
For EVERY room, extract its finishes. Return ONLY a JSON array:
[{"room_number":"101","room_name":"Lobby","floor":"","base":"","walls":"","ceiling":"","source":""}]
Use exact codes shown; blank field -> "". No markdown."""

# ---- read door schedule ----
DOORS_PROMPT = """This sheet has a door schedule. Extract every door row plus any general
door/frame finish notes on the sheet. Return ONLY JSON:
{"doors":[{"door_number":"","door_material":"","frame_material":"","fire_rating":"","comments":""}],
 "finish_notes":["short note", "..."]}
No markdown."""

# ---- read RCP ceiling heights ----
RCP_PROMPT = """This is a reflected ceiling plan. For each room where a ceiling height is shown
(e.g. "9'-0\\" A.F.F." or an elevation tag), extract it. Return ONLY a JSON array:
[{"room_number":"101","room_name":"","ceiling_height":"","other_heights":"","ceiling_type":""}]
Only rooms whose height you can actually read. No markdown."""

def analyze_drawings(pdf_bytes, only_pages=None, tile_scale=4.0):
    """
    only_pages: list of 0-based page indices to process. If None, shortlist by
    keywords (still cheap). Use a short list while testing for fast loops.

    Classification uses one fit-to-cap image (big-picture, cheap).
    Detailed extraction uses HIGH-RES TILES so small schedule text reads accurately.
    Returns classification + extracted artifacts + RAW outputs for debugging.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = doc.page_count

    pages = only_pages if only_pages is not None else shortlist_sheets(doc)
    out = {"total_sheets": total, "processed_pages": pages,
           "classified": {}, "legend": [], "rooms": [], "doors": {},
           "rcp_heights": [], "raw": {}}

    for i in pages:
        # 1) classify on a single fit-to-cap image (text clarity not critical here)
        fit_png = _render_fit(doc, i)
        craw = _ask(fit_png, CLASSIFY_PROMPT, max_tokens=400)
        cls = _parse(craw) or {}
        out["classified"][i] = cls
        out["raw"][f"classify_p{i}"] = craw

        # 2) for detailed reads, render high-res tiles once and reuse
        needs_detail = any(cls.get(k) for k in
            ("is_finish_legend", "is_room_finish_source", "is_door_schedule",
             "is_reflected_ceiling_plan"))
        tiles = _render_tiles(doc, i, scale=tile_scale) if needs_detail else None

        if cls.get("is_finish_legend"):
            r = _ask_tiles(tiles, LEGEND_PROMPT); out["raw"][f"legend_p{i}"] = r
            parsed = _parse(r);  out["legend"] += parsed if isinstance(parsed, list) else []
        if cls.get("is_room_finish_source"):
            r = _ask_tiles(tiles, ROOMS_PROMPT, max_tokens=5000); out["raw"][f"rooms_p{i}"] = r
            parsed = _parse(r);  out["rooms"] += parsed if isinstance(parsed, list) else []
        if cls.get("is_door_schedule"):
            r = _ask_tiles(tiles, DOORS_PROMPT, max_tokens=4000); out["raw"][f"doors_p{i}"] = r
            parsed = _parse(r)
            if isinstance(parsed, dict):
                out["doors"].setdefault("doors", []).extend(parsed.get("doors", []))
                out["doors"].setdefault("finish_notes", []).extend(parsed.get("finish_notes", []))
        if cls.get("is_reflected_ceiling_plan"):
            r = _ask_tiles(tiles, RCP_PROMPT, max_tokens=3000); out["raw"][f"rcp_p{i}"] = r
            parsed = _parse(r);  out["rcp_heights"] += parsed if isinstance(parsed, list) else []

    out["summary"] = {
        "legend_codes": len(out["legend"]),
        "rooms_found": len(out["rooms"]),
        "doors_found": len(out["doors"].get("doors", [])),
        "rcp_heights_found": len(out["rcp_heights"]),
    }
    doc.close()
    return out

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

def _render(doc, idx, scale=2.0):
    pix = doc[idx].get_pixmap(matrix=fitz.Matrix(scale, scale))
    return pix.tobytes("png")

def _img_block(png):
    return {"type": "image", "source": {"type": "base64",
            "media_type": "image/png", "data": base64.b64encode(png).decode()}}

def _ask(png, prompt, max_tokens=3000):
    msg = _client().messages.create(
        model=MODEL, max_tokens=max_tokens,
        messages=[{"role": "user", "content": [_img_block(png),
                                               {"type": "text", "text": prompt}]}])
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

def analyze_drawings(pdf_bytes, only_pages=None, scale=2.0):
    """
    only_pages: list of 0-based page indices to process. If None, shortlist by
    keywords (still cheap). Use a short list while testing for fast loops.
    Returns classification + extracted artifacts + RAW outputs for debugging.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = doc.page_count

    pages = only_pages if only_pages is not None else shortlist_sheets(doc)
    out = {"total_sheets": total, "processed_pages": pages,
           "classified": {}, "legend": [], "rooms": [], "doors": {},
           "rcp_heights": [], "raw": {}}

    for i in pages:
        png = _render(doc, i, scale)
        craw = _ask(png, CLASSIFY_PROMPT, max_tokens=400)
        cls = _parse(craw) or {}
        out["classified"][i] = cls
        out["raw"][f"classify_p{i}"] = craw

        # route to the right reader(s) based on what the sheet IS
        if cls.get("is_finish_legend"):
            r = _ask(png, LEGEND_PROMPT); out["raw"][f"legend_p{i}"] = r
            parsed = _parse(r);  out["legend"] += parsed if isinstance(parsed, list) else []
        if cls.get("is_room_finish_source"):
            r = _ask(png, ROOMS_PROMPT, max_tokens=4000); out["raw"][f"rooms_p{i}"] = r
            parsed = _parse(r);  out["rooms"] += parsed if isinstance(parsed, list) else []
        if cls.get("is_door_schedule"):
            r = _ask(png, DOORS_PROMPT, max_tokens=3000); out["raw"][f"doors_p{i}"] = r
            parsed = _parse(r)
            if isinstance(parsed, dict):
                out["doors"].setdefault("doors", []).extend(parsed.get("doors", []))
                out["doors"].setdefault("finish_notes", []).extend(parsed.get("finish_notes", []))
        if cls.get("is_reflected_ceiling_plan"):
            r = _ask(png, RCP_PROMPT, max_tokens=2500); out["raw"][f"rcp_p{i}"] = r
            parsed = _parse(r);  out["rcp_heights"] += parsed if isinstance(parsed, list) else []

    out["summary"] = {
        "legend_codes": len(out["legend"]),
        "rooms_found": len(out["rooms"]),
        "doors_found": len(out["doors"].get("doors", [])),
        "rcp_heights_found": len(out["rcp_heights"]),
    }
    doc.close()
    return out

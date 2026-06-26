"""
Pipeline logic. Skeleton version: specs path only, prefilter + one extraction pass.
This is where the real tool grows — add vision (drawings), cross-reference engine,
and room-schedule assembly here as separate functions, keeping main.py thin.
"""
import os, re, json, io
from pypdf import PdfReader
import anthropic

# Model string lives in one place so it's easy to update.
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# Universal paint-scope vocabulary (not job-specific). Drives the cheap prefilter.
KEYWORDS = [
    "paint","painted","field painted","shop primed","finish paint","stain","stained",
    "sealer","sealed","concrete sealer","epoxy","dryfall","elastomeric","intumescent",
    "fireproofing","high-performance coating","anti-graffiti","prefinished","factory finish",
    "anodized","powder coated","kynar","pvdf","wallcovering","09 91","09 93","09 96",
]

def _client():
    # Created lazily so the app can boot (and health-check) even if key is missing.
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def extract_pages(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [(i, (p.extract_text() or "")) for i, p in enumerate(reader.pages)]

def prefilter(pages):
    hits = []
    for idx, text in pages:
        low = " " + text.lower() + " "
        if any(k in low for k in KEYWORDS):
            hits.append((idx, text))
    return hits

EXTRACTION_PROMPT = """You are a senior paint estimator reviewing CONSTRUCTION SPECIFICATIONS.
Find items related to painting, staining, sealing, wallcovering, prefinished/factory finishing,
intumescent coating, or exposed-to-view finish responsibility.

Return ONLY a JSON array (no markdown). Each item:
{"spec_section":"","location":"","item":"","finish_required":"",
 "finish_type":"paint|stain|sealer|wallcovering|intumescent|prefinished|other",
 "disposition":"Include|Exclude|Clarify","confidence":"High|Medium|Low",
 "source_page":0,"source_note":"<15 words"}

PAGES:
{pages}
"""

def _parse(raw):
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        return json.loads(m.group(0)) if m else []

def analyze_spec_pdf(pdf_bytes, max_pages=60):
    pages = extract_pages(pdf_bytes)
    total = len(pages)
    hits = prefilter(pages)[:max_pages]  # cap for skeleton speed/cost
    if not hits:
        return {"total_pages": total, "relevant_pages": 0, "items": [],
                "note": "No paint-relevant pages found in prefilter."}

    pages_text = "\n\n".join(f"[PAGE {idx}]\n{text}" for idx, text in hits)[:60000]
    msg = _client().messages.create(
        model=MODEL, max_tokens=4000,
        messages=[{"role": "user",
                   "content": EXTRACTION_PROMPT.replace("{pages}", pages_text)}])
    raw = "".join(b.text for b in msg.content if b.type == "text")
    items = _parse(raw)
    return {
        "total_pages": total,
        "relevant_pages": len(hits),
        "item_count": len(items),
        "items": items,
        "note": f"Skeleton run: specs path, first {len(hits)} relevant pages of {total}.",
    }

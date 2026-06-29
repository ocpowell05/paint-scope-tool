"""
Paint Scope Tool — backend (skeleton)
Proves the full loop: upload PDF -> prefilter pages -> one extraction call -> report.
The heavy pipeline (vision, cross-reference, room schedule) gets added after this
deploys cleanly. Keep this file small; grow app/pipeline.py instead.
"""
import os
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.pipeline import analyze_spec_pdf
from app.drawings import analyze_drawings
from app.assemble import assemble_schedule

app = FastAPI(title="Paint Scope Tool")

# Allow the frontend to call the API (loosened for now; tighten before real users)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# Health check — Render pings this to know the service is up
@app.get("/api/health")
def health():
    key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))
    return {"status": "ok", "api_key_configured": key_present}

@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "Server missing ANTHROPIC_API_KEY. Set it in Render env vars.")
    pdf_bytes = await file.read()
    try:
        # max_pages keeps the skeleton fast + cheap; raise later for full runs
        result = analyze_spec_pdf(pdf_bytes, max_pages=int(os.environ.get("MAX_PAGES", "60")))
        return JSONResponse(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Processing failed: {e}")

@app.post("/api/analyze-drawings")
async def analyze_drawings_endpoint(
    file: UploadFile = File(...),
    only_pages: str = "",   # comma-separated 0-based page indices for test mode, e.g. "23,30,24"
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "Server missing ANTHROPIC_API_KEY.")
    pdf_bytes = await file.read()
    pages = None
    if only_pages.strip():
        try:
            pages = [int(x) for x in only_pages.split(",") if x.strip() != ""]
        except ValueError:
            raise HTTPException(400, "only_pages must be comma-separated numbers, e.g. 23,30")
    try:
        result = analyze_drawings(pdf_bytes, only_pages=pages)
        return JSONResponse(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Drawings processing failed: {e}")

@app.post("/api/room-schedule")
async def room_schedule_endpoint(
    file: UploadFile = File(...),
    only_pages: str = "",   # comma-separated 0-based page indices, e.g. "23,30,24"
    include_raw: bool = False,
):
    """Full flow: read drawings (vision) -> resolve codes + scope rules -> assemble
    the room finish schedule with ceiling heights. Pass the schedule sheets in
    only_pages while testing, e.g. 23,30,24."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "Server missing ANTHROPIC_API_KEY.")
    pdf_bytes = await file.read()
    pages = None
    if only_pages.strip():
        try:
            pages = [int(x) for x in only_pages.split(",") if x.strip() != ""]
        except ValueError:
            raise HTTPException(400, "only_pages must be comma-separated numbers.")
    try:
        drawings = analyze_drawings(pdf_bytes, only_pages=pages)
        assembled = assemble_schedule(drawings)
        if not include_raw:
            drawings.pop("raw", None)
        assembled["extraction_summary"] = drawings.get("summary", {})
        return JSONResponse(assembled)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Schedule assembly failed: {e}")

# Serve the frontend (index.html) at the root
app.mount("/", StaticFiles(directory="static", html=True), name="static")

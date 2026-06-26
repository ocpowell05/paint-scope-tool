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
        raise HTTPException(500, f"Processing failed: {e}")

# Serve the frontend (index.html) at the root
app.mount("/", StaticFiles(directory="static", html=True), name="static")

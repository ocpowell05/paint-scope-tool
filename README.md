# Paint Scope Tool

Reads construction spec/drawing PDFs and extracts paint scope for estimators.
This repo is the **deploy-first skeleton**: upload a spec PDF → prefilter paint-relevant
pages → one AI extraction pass → scope table. Once this runs in the cloud, the full
pipeline (drawings vision, code cross-reference, room finish schedule) gets layered in.

## Architecture
- **Backend:** FastAPI (`app/main.py`) wrapping the pipeline (`app/pipeline.py`)
- **Frontend:** static HTML upload page (`static/index.html`)
- **Host:** Render (`render.yaml`)
- **Key:** lives in an environment variable, never in code or the browser

---

## Run it locally (do this first)

1. Create and activate a virtual environment:
   ```
   python -m venv .venv
   # mac/linux:
   source .venv/bin/activate
   # windows:
   .venv\Scripts\activate
   ```
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Set your key for local testing:
   ```
   cp .env.example .env
   ```
   Open `.env` and paste your real `ANTHROPIC_API_KEY`. (This file is gitignored.)
   Then load it into your shell, OR install python-dotenv and load it — simplest for now:
   ```
   # mac/linux
   export $(grep -v '^#' .env | xargs)
   ```
4. Start the server:
   ```
   uvicorn app.main:app --reload
   ```
5. Open http://localhost:8000 — upload a spec PDF.
   Health check: http://localhost:8000/api/health (shows if the key is detected).

---

## Deploy to Render

1. Push this repo to GitHub.
2. On render.com: **New → Blueprint**, select this repo (it reads `render.yaml`).
3. In the service's **Environment** tab, add `ANTHROPIC_API_KEY` with your real key.
4. Render builds and gives you a public URL. Every `git push` redeploys automatically.

---

## Important notes
- **Never commit `.env`** or your key. If a key ever lands in a commit, rotate it immediately.
- `MAX_PAGES` caps how many relevant pages get sent (skeleton speed/cost control).
  Raise it once long-running-job handling is added.
- Large docs (600+ pages, vision) will eventually exceed normal web request timeouts.
  That's the next milestone after this deploys: a job/polling pattern.

## Roadmap (next, in order)
1. Deploy this skeleton, confirm upload→report works in the cloud.
2. Add drawings path (render pages → Claude vision) for schedules.
3. Add cross-reference engine (resolve finish codes → spec products → scope rules).
4. Add room finish schedule assembly with ceiling heights.
5. Long-running job handling (so full sets don't time out).
6. Cost-per-job measurement for pricing.

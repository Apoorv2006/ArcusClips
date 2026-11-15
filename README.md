# ArcusClips

**A self-hosted, local-AI bulk video clipping platform.** ArcusClips turns long
source videos (Google Drive folders, creator channels, playlists, direct video
URLs and public webpages) into short, ready-to-post clips — transcribed, scored
and rendered **entirely on your own machine**. No transcripts, video or metadata
are ever sent to a cloud AI.

---

## Why ArcusClips

- **Local-only AI.** Transcription runs on local **faster-whisper**; moment
  selection/titles/descriptions run on a local **LM Studio** server
  (OpenAI-compatible). No external AI API is ever contacted.
- **Bulk & autonomous.** Drop a folder or channel, configure a batch, and the
  pipeline runs: import → ffprobe → extract audio → Whisper → segment →
  LM Studio moment ranking → hooks/titles/descriptions → FFmpeg render
  (3 layouts × 6 caption styles) → validation.
- **Resilient.** Batch jobs are persisted in the database. Progress survives
  restarts; one failed video never blocks the rest of the batch.
- **Read-only by default.** Google Drive integration is scoped to
  `drive.readonly`; originals are never modified or deleted.
- **No Docker required.** Runs natively on Windows or Linux with just Python
  and Node.

## Architecture

| Layer    | Tech |
| -------- | ---- |
| Frontend | React 18 + TypeScript + Vite + Tailwind, TanStack Query, React Router |
| Backend  | Python 3.11+ · FastAPI · Pydantic · SQLAlchemy · Alembic · SQLite (Postgres optional) |
| Media    | FFmpeg/FFprobe (bundled via `imageio-ffmpeg`) |
| Transcription | faster-whisper (local) |
| LLM      | LM Studio (local, OpenAI-compatible, `http://127.0.0.1:1234/v1`) |

```
frontend/            React SPA (Vite dev server proxies /api -> backend)
backend/
  app/
    config.py        env-driven settings (ARCUS_ prefix)
    security.py      Fernet encryption, PBKDF2, SSRF guard, path-traversal safe paths
    models.py        SQLAlchemy ORM
    services/
      ffmpeg_engine.py   probe, layout filters, 6 ASS caption styles, render
      whisper_service.py faster-whisper transcription
      lmstudio.py        OpenAI-compatible client (test/list/chat/chat_json)
      drive_google.py    Drive v3 + OAuth2 (paginated, recursive, shortcuts, dedup)
      source_scanner.py  webpage crawler + yt-dlp + direct video URLs
      selection.py       moment ranking + hallucination guard + heuristic fallback
      jobs.py            persistent job queue + worker
      export.py          ZIP packaging with manifest
    api/             FastAPI routers
    main.py          app factory, lifespan, static dist serving
    worker.py        standalone worker entry (`python -m app.worker`)
  alembic/           migrations
  tests/             pytest suite
data/                media, transcripts, renders, exports (git-ignored)
```

## Quick start

### Prerequisites

- **Python 3.11+** and **Node.js 18+**
- **FFmpeg** — auto-detected via the bundled `imageio-ffmpeg` binary. (Optionally
  point `ARCUS_FFMPEG_PATH` / `ARCUS_FFPROBE_PATH` at a system/GPU build.)
- **LM Studio** running locally with an OpenAI-compatible server on
  `http://127.0.0.1:1234` (optional but recommended; without it, selection falls
  back to deterministic heuristics flagged `is_heuristic`).

### 1. Backend

```powershell
# Windows PowerShell
cd backend
python -m venv ..\.venv
..\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Copy and edit config (optional — sensible defaults work out of the box)
Copy-Item .env.example .env

# Run migrations (creates the SQLite DB and schema)
alembic upgrade head

# Start the API
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

```bash
# Linux / macOS
cd backend
python -m venv ../.venv && source ../.venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### 2. Frontend

```powershell
# Windows PowerShell
cd frontend
npm install
npm run dev        # http://localhost:5173 — proxies /api to the backend
```

```bash
# Linux / macOS
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**. The default local account is
**admin / changeme** — change it immediately in deployment.

### 3. Worker

The API server runs an in-process worker by default. For a separate worker
process (e.g. to scale rendering), run:

```powershell
..\.venv\Scripts\python.exe -m app.worker
```

The worker reads the same job queue from the database, so multiple workers are
safe and jobs are recovered automatically after a restart.

## Using ArcusClips

1. **Sources** — connect Google Drive (Drive Studio) or add video URLs, creator
   channels, playlists and public webpages.
2. **Videos** — discovered/imported videos appear here. Transcribe to inspect
   the transcript.
3. **Batch Studio** — pick videos, choose clips-per-video, duration, quality,
   caption style and layout, then generate. Monitor live progress per job.
4. **Clips** — review generated clips, download MP4s, or open the **editor** to
   adjust timing/hook/title/description and re-render (new version each time).
5. **Exports** — export a finished batch as a ZIP with `clips/`, `thumbnails/`
   and `metadata/` (`manifest.csv` + `batch_summary.json`).
6. **Settings** — configure LM Studio, Whisper, render defaults and branding.

## Configuration

All settings are environment variables prefixed with `ARCUS_` (see
[`backend/.env.example`](backend/.env.example) for the full list). Key ones:

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `ARCUS_DATA_DIR` | `<repo>/data` | Where all media and DB live |
| `ARCUS_DATABASE_URL` | SQLite in data dir | Override for Postgres |
| `ARCUS_LMSTUDIO_BASE_URL` | `http://127.0.0.1:1234/v1` | Local LLM server |
| `ARCUS_WHISPER_MODEL` | `base` | faster-whisper model size |
| `ARCUS_GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | empty | Google Drive OAuth credentials |
| `ARCUS_AUTH_ENABLED` | `true` | Local auth (set `false` for a trusted LAN) |

## Google Drive

1. In Google Cloud Console, create an OAuth **Web application** client (the
   server-side authorization code flow requires a Web application client, not
   Desktop app credentials). Add an authorized redirect URI.
2. Set `ARCUS_GOOGLE_OAUTH_CLIENT_ID`, `ARCUS_GOOGLE_OAUTH_CLIENT_SECRET`, and
   make sure `ARCUS_GOOGLE_OAUTH_REDIRECT_URI`
   (`http://127.0.0.1:8000/api/drive/auth/callback`) is an authorized redirect —
   it must match the configured value exactly.
3. In **Drive Studio**, click *Connect Google Drive* and approve. The callback
   resolves the initiating user from a one-time, short-lived OAuth state record
   (no bearer token is required on the redirect). Access/refresh tokens are
   encrypted at rest (Fernet) and never exposed to the frontend.
4. Browse to folders, select them, and *Scan* — ArcusClips discovers videos
   recursively across My Drive and Shared Drives (following shortcuts,
   de-duplicating by file id).

The integration is scoped to `drive.readonly`; originals are never modified.

## Tests

```powershell
# Backend (backend/)
cd backend
..\.venv\Scripts\python.exe -m pytest tests/ -q
```

```bash
# Frontend (frontend/)
cd frontend
npm test
```

The backend suite covers security (encryption, SSRF guard, path traversal),
the FFmpeg engine (all layouts/styles), LM Studio selection logic, Drive
scanning (with fakes), the source crawler, the API, and a full end-to-end batch
pipeline. The frontend suite covers the API client and UI primitives.

## License

Provided as-is for local, personal use.

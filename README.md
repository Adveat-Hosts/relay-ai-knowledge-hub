# Relay — AI-powered team knowledge & handoff

A deliberately small Flask + SQLite application for capturing meetings, extracting structured follow-ups with AI, and keeping open work attached to its history.

## Features
- Create and persist meeting records with date, participants, agenda and raw notes.
- Track action items by owner, due date and status; save an outcome when closing work.
- Carry all non-closed items into a follow-up meeting. The original record remains unchanged and the new record stores a carry-forward history entry.
- Generate an AI recap containing a summary, decisions, action items, owners and a short catch-up brief.
- Generate a standalone catch-up brief for a teammate who missed the meeting.
- Add short team documents to a knowledge base. The AI recap receives these documents as context and returns suggested links between work and relevant references.
- Structured JSON logs with request IDs, log levels, key lifecycle events, AI latency/outcome, and rotating file output in `logs/app.log`.

## Stack and approach
- **Backend:** Python, Flask, Flask-SQLAlchemy.
- **Database:** SQLite for a zero-setup local demo. Set `DATABASE_URL` to a persistent database URL for deployment.
- **Frontend:** plain HTML, CSS and JavaScript. No build step.
- **AI:** OpenAI Chat Completions API using `gpt-4o-mini` by default. The prompt asks for JSON and explicitly instructs the model not to invent owners, dates or decisions. Knowledge-base documents are included as retrieval context (latest 15 records, clipped to 1,800 characters each).

## Run locally
1. Install Python 3.10 or newer.
2. Open a terminal in this project folder.
3. Create and activate a virtual environment:
   - Windows PowerShell: `py -m venv .venv` then `.venv\\Scripts\\Activate.ps1`
   - macOS/Linux: `python3 -m venv .venv` then `source .venv/bin/activate`
4. Install dependencies: `pip install -r requirements.txt`
5. Copy `.env.example` to `.env` and add your own `OPENAI_API_KEY`.
6. Start: `python app.py`
7. Visit `http://127.0.0.1:5000`.

The app creates its SQLite database and one clearly labelled sample meeting/document on first run. AI buttons require a valid API key and internet access. Without a key, normal CRUD and handoff workflows remain available and AI actions show a configuration message.

## AI configuration and data flow
The browser sends meeting notes to `POST /api/meetings/<id>/ai-recap`. The server fetches recent knowledge-base documents, constructs a constrained prompt, calls the configured OpenAI model, validates the returned JSON shape by parsing it, then persists the summary, decisions and extracted action items. `POST /api/meetings/<id>/catchup` uses the meeting and its open items to produce a concise handoff brief. API failures are logged and returned as a friendly `503`; existing records are not overwritten on AI failure.

Set `OPENAI_MODEL` to another compatible chat model if desired. Never commit `.env` or paste API keys into source code.

## Logging
Logs are newline-delimited JSON written to `logs/app.log`, with 1 MB rotation and three backups. Each request receives an `X-Request-ID` response header (or accepts one supplied by a caller). Events include request completion, meeting creation, item reassignment/closure, carry-forward, knowledge creation, and AI call latency/outcome. Request bodies, notes, API keys and other secrets are not logged.

## Special feature: Context Relay
**Carry-forward with provenance.** A follow-up meeting can inherit every open, in-progress or blocked item without closing or moving the original. Each copied item keeps its owner, due date and status and appends a `carried_forward` event referencing the source meeting. This avoids the common “new meeting, lost action item” failure while retaining a simple audit trail.

## API overview
| Method | Route | Purpose |
|---|---|---|
| GET | `/api/overview` | Dashboard stats and meeting list |
| POST | `/api/meetings` | Create meeting |
| GET | `/api/meetings/<id>` | Read meeting and items |
| POST | `/api/meetings/<id>/ai-recap` | AI extraction and knowledge linking |
| POST | `/api/meetings/<id>/catchup` | AI catch-up brief |
| POST | `/api/meetings/<id>/carry-forward` | Create follow-up with open items |
| PATCH | `/api/items/<id>` | Reassign/update/close item and save outcome |
| GET/POST | `/api/knowledge` | List/add reference documents |

## Deployment
For a simple demo deployment, use Render or Railway with a persistent disk/volume for SQLite, or configure a managed PostgreSQL database through `DATABASE_URL`. Build command: `pip install -r requirements.txt`. Start command: `gunicorn app:app`. Add `OPENAI_API_KEY` and `OPENAI_MODEL` as server environment variables. Do not use an ephemeral filesystem for the SQLite database if you need data to survive redeploys.

## Assumptions and known limitations
- Single shared workspace; no authentication, roles or per-user permissions (out of scope for this time-boxed assessment).
- Knowledge retrieval is lightweight context injection, not a vector database. Only the latest 15 documents are supplied to the model.
- AI output should be reviewed by a person before treating extracted owners/dates as final. The model may omit ambiguous details; the UI supports manual correction.
- Email/calendar integrations, file uploads, notifications and background reminders are not implemented.
- SQLite is suitable for a demo; a multi-user deployment should use PostgreSQL and add authentication/rate limiting.

# K1NG5L3Y

A local-first cyberpunk operator console: one screen that watches your mail, your
calendar, your screen, your voice and your repo, and routes each job to the model
that is best at it — while spending as close to zero tokens as possible.

```bash
python -m kingsley          # http://127.0.0.1:3000
```

![console](https://img.shields.io/badge/status-alpha-ff2bd6) ![python](https://img.shields.io/badge/python-3.10+-00f0ff)

---

## What it actually does

| Capability | How it works | Needs |
|---|---|---|
| **Chat with task-aware routing** | Each message is tagged (`code`, `news`, `google`, `ideas`, `homework`, `fast`) and the router tries providers in the order that task deserves, failing over automatically | at least one provider |
| **Model switching** | Per-message provider override in the console, plus a static route table in `kingsley/config.py` | — |
| **Gmail triage** | Reads unread mail (`is:unread newer_than:14d`), extracts actionable items, writes them into the task grid | Google OAuth |
| **Calendar sync** | Pushes open tasks that have a due date into Google Calendar as timed blocks | Google OAuth |
| **Ambient listening** | Browser speech recognition while the tab is open; wake word **“Kingsley”**; “Kingsley add task …” creates a task without spending a token | mic + Chrome/Edge |
| **Screen vision** | `getDisplayMedia` → JPEG frame → vision model → analysis in the panel | a vision-capable provider |
| **Repo agent** | Lists your repo, proposes file contents, and **waits for your approval** before writing anything; read-only git status/diff/log built in | — |
| **Diagnostics** | Real `psutil` telemetry (CPU per core, memory, disk, net, process) + provider health matrix + event stream over SSE | — |
| **Interest scout** | Searches your interests with `ddgs`, has the news router write a briefing, and can turn focus items into tasks | network |

## Zero-token strategy

The router is ordered so the **free** options come first for every task class and
paid frontier models are the escalation, not the default:

```
fast      → freellmapi → gemini → ollama → gpt
code      → claude → freellmapi → gpt → gemini → ollama
news      → grok → gemini → freellmapi → gpt
google    → gemini → freellmapi → gpt
ideas     → gpt → claude → gemini → freellmapi
homework  → gpt → claude → gemini → freellmapi
vision    → gemini → gpt → claude → freellmapi → ollama
```

[FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) is the recommended free
layer: it stacks 30+ provider free tiers behind one OpenAI-compatible endpoint, so
Kingsley just points at it:

```bash
# in the FreeLLMAPI checkout
npm install && npm run dev        # router on http://localhost:3001
# add provider keys in its dashboard, copy the unified `freellmapi-…` key
export FREELLMAPI_BASE_URL=http://127.0.0.1:3001/v1
export FREELLMAPI_API_KEY=freellmapi-your-unified-key
```

Ollama (`ollama serve`, port 11434) is the fully offline floor: no network, no quota.
Its honest limit: free tiers are not frontier models, latency varies, and quality
dips late in the day as daily caps are hit — that is why the paid providers stay in
the chain as fallbacks instead of being removed.

## Configuration

Everything is env-driven; nothing secret is written into the repo. Data (SQLite)
lives in `.kingsley/` by default.

```bash
# free layer
export FREELLMAPI_BASE_URL=http://127.0.0.1:3001/v1   # default
export FREELLMAPI_API_KEY=freellmapi-...              # from the FreeLLMAPI dashboard
export OLLAMA_HOST=http://127.0.0.1:11434             # default

# paid escalation (all optional)
export GEMINI_API_KEY=...        # best for Google-shaped work + vision
export OPENAI_API_KEY=...        # ideation
export ANTHROPIC_API_KEY=...     # code
export XAI_API_KEY=...           # news / live web flavour

# model overrides
export KINGSLEY_GEMINI_MODEL=gemini-2.5-flash
export KINGSLEY_GPT_MODEL=gpt-5
export KINGSLEY_CLAUDE_MODEL=claude-sonnet-4-5
export KINGSLEY_GROK_MODEL=grok-4
export FREELLMAPI_MODEL=auto

# Google: Gmail read + Calendar write
export GOOGLE_CLIENT_ID=...
export GOOGLE_CLIENT_SECRET=...
export KINGSLEY_PUBLIC_URL=http://localhost:3000

# behaviour
export KINGSLEY_INTERESTS="local llm inference, rust, formula 1"
export KINGSLEY_WORKSPACE=/path/to/repo     # repo agent's sandbox root
export KINGSLEY_HOME=~/.kingsley            # sqlite + state
export KINGSLEY_PORT=3000
export KINGSLEY_HOST=127.0.0.1
```

### Google setup, exactly

1. Google Cloud Console → new project → enable **Gmail API** and **Google Calendar API**.
2. OAuth consent screen → External → add yourself as a test user.
3. Credentials → OAuth client ID → **Web application** → authorized redirect URI:
   `http://localhost:3000/api/google/callback` (must match `KINGSLEY_PUBLIC_URL`).
4. Export both values, restart Kingsley, open **LINK → CONNECT GOOGLE**.

Scopes requested: `gmail.readonly`, `calendar`, `openid`, `userinfo.email`.
Kingsley never sends mail and never deletes anything.

## Safety model

* Repo writes are **approval-gated**: the agent proposes full file contents with a
  diff, you click APPLY. Nothing touches disk otherwise.
* Paths are confined to `KINGSLEY_WORKSPACE`; non-text extensions are refused.
* Git access is read-only (`status`, `diff`, `log`, `branch`, `show`, `remote`).
* Google access is read-only for Gmail and event-scoped for Calendar.
* The server binds `127.0.0.1` by default. If you expose it, put it behind a
  reverse proxy with auth — Kingsley has no login of its own.
* CORS is open because the UI may be hosted on GitHub Pages while the API stays
  local; that is safe only while the API is loopback-bound.

## Layout

```
kingsley/
├── config.py        env, provider table, task routes
├── providers.py     router: openai / anthropic / gemini wire formats + health probes
├── agents.py        triage, calendar sync, scout, homework, repo agent
├── google_api.py    OAuth + Gmail + Calendar REST
├── tools.py         sandboxed file access, read-only git, approval queue
├── diagnostics.py   psutil telemetry
├── store.py         SQLite (tasks, transcripts, events, approvals, chat, intel)
├── server.py        FastAPI surface + SSE
└── static/          the console (index.html, style.css, app.js)
```

## GitHub Pages

Pages can only serve static files, so it hosts **the console UI**, not the Python
backend. `.github/workflows/kingsley-pages.yml` publishes `kingsley/static` on every
push to `main`; after enabling *Settings → Pages → Source: GitHub Actions* the
console is reachable at `https://<user>.github.io/<repo>/`.

When the page is on `github.io` it points at `http://127.0.0.1:3000` by default, so
run the backend on the same machine. Set a different endpoint with the **LINK**
panel (stored in `localStorage`, or `?api=http://host:3000`).

Chrome may block a public page from calling a private address (Private Network
Access). If the LINK chip stays red on the Pages URL, open the console directly at
`http://localhost:3000` — same UI, served by Kingsley itself, always same-origin.

## Status / honest limits

* Single user, no authentication, no persistence for chat beyond SQLite.
* Ambient listening only works while the tab is open (browser speech API).
* “Constantly checks Gmail” is polling on demand/interval from the UI, not a
  background daemon; wrap `POST /api/gmail/triage` in cron/systemd timers for
  always-on behaviour.
* Screen understanding is one frame per ANALYSE, not a live video stream.
* The repo agent proposes edits; it cannot open PRs or push.

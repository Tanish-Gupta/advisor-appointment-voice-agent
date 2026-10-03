# Voice Agent: Advisor Appointment Scheduler

A compliant pre-booking assistant. It is built chat-first, and voice is added in Phase 7. See `docs/` for the HLD and LLD.

**Status:** Phases 1–6 are done, so the chat agent is complete. Voice (Phase 7) and hardening (Phase 8) remain. Done so far:
- the domain and mock calendar
- the state machine
- guardrails
- the chat CLI, HTTP API and web UI
- NLU: stub, rules, and hybrid rules + Gemini (Phase 3)
- real Google tools via our own FastMCP server, with Gemini tool calling behind a ToolGate (Phase 3B)
- a SQLite booking store, an idempotent outbox with compensation, a signed secure link and a PII vault (Phase 4)
- the waitlist when no slot matches, and investment-advice refusal with educational links and a "continue where we left off" pivot (Phase 5)
- reschedule, cancel, "what to prepare" (static guides) and check availability (read-only) (Phase 6)

## Setup (Python 3.12)

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install editables
.venv/bin/pip install --no-build-isolation -e ".[dev]"
```

## Run

```bash
.venv/bin/agent-chat            # CLI REPL (add --debug for state/NLU, --record FILE for golden jsonl)
.venv/bin/agent-api             # HTTP API + web chat at http://127.0.0.1:8000
.venv/bin/agent-mcp-server      # standalone FastMCP Google tools server (stdio; --transport http --port 8001)
python scripts/gen_mock_calendar.py   # regenerate data/mock_calendar.json
```

## End-to-end chat sandbox (no Google credentials needed)

```bash
.venv/bin/python scripts/sandbox_api.py            # http://127.0.0.1:8000 (add --nlu rules to skip Gemini)
```

This runs the real web chat, API, orchestrator, NLU, MCP client, ToolGate, outbox worker and FastMCP server. Only Google's HTTP endpoint is swapped for the in-memory fake from the tests. The real clock and mock calendar are used, and the database is in memory.

- `GET /sandbox/google` shows the calendar holds, Docs lines and Gmail drafts the agent "wrote".
- `GET /admin/outbox?code=NL-XXXX` shows the jobs for a booking.

Things to try:

| Intent | Example |
|---|---|
| book new | "I want to talk about my SIP mandate" → "tuesday afternoon" → "the second one" → "yes" |
| advice refusal | at any point: "which fund gives the best returns?" → "yes" resumes where you were |
| waitlist | ask for an exact time that is taken, e.g. "thursday at 9:30 am" after booking it |
| reschedule | "I need to move my appointment, code is NL-A742" |
| cancel | "cancel my booking" → "NL A742" → "yes" |
| what to prepare | "what should I keep ready for KYC?" |
| availability | "any free slots on thursday?" |

### Conversational behaviour

- **Requests before the disclaimer are remembered.** For example, "yes, book a SIP call for tuesday afternoon" goes straight to slot offers. If the request comes before the "yes", the agent asks for the acknowledgement and then continues with it.
- **Weekend dates get a suggestion, not a dead end.** For example, "Saturday, 2pm" gets "How about Monday, around 2:00 PM?" A reply of "sure" accepts it.
- **Exact times that are taken are acknowledged.** For example: "Monday, 5 October 2026, 2:00 PM IST is already taken, but here's what's close…"
- **Slots can be picked by time.** For example, "1:30 works" while two slots are on offer.
- **The web UI shows each agent turn as one bubble**, with the secure link as a clickable "Open secure link".
- **The API adds a `suggestions` field with natural chip labels.** `quick_replies` is unchanged for back-compat.

All copy lives in `src/advisor_agent/orchestrator/templates.yaml`.

## NLU engines (Phase 3)

Set `AGENT_NLU_ENGINE` in `.env` (see `.env.example`):

| Engine | Behaviour |
|---|---|
| `hybrid` (default) | Rules first. Gemini is called only when the rules cannot answer what the current state asks for. Needs `AGENT_GEMINI_API_KEY`; without a key it runs rules-only. |
| `rules` | Deterministic and offline. Understands natural phrases like "I want to book a consultation about my SIP", "next Tuesday after 3 pm", "the second one", "no, Wednesday instead". |
| `stub` | The Phase 2 typed commands below. Used by the Phase 2 tests. |

Gemini only receives the user's text plus non-PII context (state, today's date, offered slots). Its output is validated against the `NLUResult` schema, and dates are always resolved by `RelativeDateResolver`, never by the model. A timeout or invalid output falls back to the rules result.

```bash
AGENT_NLU_ENGINE=hybrid AGENT_GEMINI_API_KEY=... .venv/bin/agent-chat --debug
```

The test suite never calls Gemini: it uses fakes, and `make_settings` forces `gemini_api_key=None`.

### Stub commands

Stub NLU accepts plain phrases or typed commands, for example:
- `yes`
- `book`
- `topic kyc`
- `time tuesday afternoon`
- `1` / `2` / `neither`
- `stop`

You can also combine them, as in `book topic kyc time monday morning`.

With `AGENT_MCP_TOOLS_ENABLED` off (the default), confirming a slot only reads the booking back. See below to enable real bookings.

## Real bookings: Google Calendar, Docs and Gmail through MCP (Phases 3B and 4)

1. In Google Cloud Console, enable the **Calendar, Docs and Gmail APIs**. Create an **OAuth client ID (Desktop app)** and save it as `secrets/google_oauth_client.json`. The `secrets/` folder is gitignored.
2. Create a Google Doc called "Advisor Pre-Bookings" and copy its id from the URL.
3. Set these in `.env` (see `.env.example`):
   ```
   AGENT_MCP_TOOLS_ENABLED=true
   AGENT_GOOGLE_PREBOOKING_DOC_ID=<doc id>
   AGENT_GOOGLE_ADVISOR_EMAIL=<advisor address>
   AGENT_LINK_HMAC_SECRET=<random>        # optional in dev
   AGENT_VAULT_FERNET_KEY=<fernet key>    # optional in dev
   ```
4. Run the one-time consent flow: `.venv/bin/python -m advisor_agent.mcp_server.google_auth`. It writes `secrets/google_token.json`.
5. Start the app with `.venv/bin/agent-api` or `.venv/bin/agent-chat`.

**What happens on confirm:**
- The booking code (e.g. `NL-A742`) is stored in SQLite, and an outbox runs these steps in order, each with an idempotency key:
  - `calendar_create_hold`: "Advisor Q&A — {Topic} — {Code}", tentative.
  - `docs_append_prebooking`: `{date} | {topic} | {slot} | {code} | tentative`.
  - `gmail_create_draft`: a draft only, never sent.
- The caller hears the code, the IST date and time, and a signed link `/b/{code}?t=...`. Contact details are entered there, outside the chat, and encrypted in the vault.
- Gemini proposes the tool calls. The ToolGate only allows the exact deterministic plan, so a refused or hallucinated call falls back to the plan.
- Retryable errors (429/5xx/timeouts) back off and retry. A dead calendar hold marks the booking `needs_attention`, appends a `hold_failed` line to the doc, and drafts an ops alert.
- `GET /admin/outbox?code=NL-XXXX` (outside prod) shows a booking's jobs.

The CI suite runs these tools against fake Google services through the real MCP protocol. The live smoke test, `tests/live/test_google_live.py`, creates and then deletes a hold. It is deselected by default; run it explicitly with `.venv/bin/pytest -m google_live tests/live`.

## Quality gates

```bash
.venv/bin/pytest
.venv/bin/ruff check src tests scripts
.venv/bin/lint-imports
```

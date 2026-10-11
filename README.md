# JS Automatic QuoteBot

Steps 1-2 of the build (no accounts needed; Groq/Zoho/Telegram are mocked in tests).

- `app/intake.py` — quote-request detection (quote / flag / attach_code / ignore)
- `app/rules.py` — total handling, measure vs count, safety checks, single-line fallback, markup
- `app/groq_client.py` — Groq calls, same 4-attempt model fallback as the existing QuoteBot
- `app/parser.py` — message parsing prompt (stated_total, measures, work_description, listed_as)
- `app/matching.py` — customer matching (code -> brand -> location -> strict AI), item match, HSN
- `app/zoho.py` — Zoho client (token, estimates, line descriptions, email, PDF, duplicate guard)
- `app/aliases.py` — abbreviations in customer names (VZG, COCH...) — edit freely
- `app/main.py` — Flask routes for the bridge, PIN-protected /admin panel, worker thread; `app/wsgi.py` + `Procfile` for Railway
- `bridge/` — Node.js WhatsApp bridge (Baileys 7.0.0-rc14) — NOT tested against real WhatsApp
- `DEPLOY.md` — step-by-step: where to start
- `app/engine.py` — message intake, per-message 3-minute timers, edit/delete/PO/lone-code/correction handling, stop/start, bridge watchdog
- `app/pipeline.py` — one quote start to finish (dry-run aware; email and WhatsApp independent)
- `app/store.py` — Redis state (pending timers, processed marks, switches, sender brand context)
- `app/bridge_client.py` — sends the PDF to the group through the Node bridge
- `app/notify.py` — Telegram message formats
- `app/config.py` — markup, timer, thresholds, staged-rollout defaults (dry-run ON, posting OFF)
- `tests/` — NOT part of the deployment (they use fake Zoho/Groq/Telegram/Redis). `tests/test_samples.py` — your 14 sample messages plus rule cases

Run the tests from this folder:
`python -m tests.test_samples`, `python -m tests.test_modules`, `python -m tests.test_pipeline`, `python -m tests.test_realnames`, `python -m tests.test_flask`

Next: deploy in dry-run, then live in stages (see DEPLOY.md).

## Settings added for this step
- `DEFAULT_CUSTOMER_NAME` — Zoho customer used when the store is unclear (quote gets a notice + the name from the message in its notes)
- `OWNER_NUMBERS` — your number(s); everything they send is ignored
- `BRAND_CONTEXT_SECONDS` — how long a sender's brand carries over (default 3 hours)
- `OWNER_JIDS` — your exact sender id, only if WhatsApp hides your number in groups
- attachments (photos, PDFs, voice notes...) are ignored entirely: never read, no tokens spent

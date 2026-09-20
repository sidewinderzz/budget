# Budget — Architecture

This file describes the codebase **as it currently stands**: what it's built from, how
it's structured, and how it's deployed. It is not a build log — for how any of this came
to be, what broke along the way, and when things shipped, see `IMPLEMENTATION_HISTORY.md`.
If this file and the actual code ever disagree, that's a bug in the code (or a stale doc),
not the other way around — `app/services/calc.py` in particular must match the
"Calculation Rules" section below exactly.

## What this app is

A private, single-user, self-hosted "cash-commitment and runway tracker" — not a generic
budgeting app. It answers: **"What is my real financial position right now, and what's
actually safe to spend?"**, explicitly distinguishing cash-in-hand from cash-that's-
already-spoken-for (upcoming bills, ordered-but-not-yet-billed purchases, debt minimums,
committed obligations).

The single design principle carried through every layer: **future income is never
counted in the primary/default status number.** The dashboard's main "safe to spend"
figure uses only cash already in accounts; future paychecks only ever appear in the
separate, explicitly-labeled forecast view (`app/routers/forecast.py`, backed by
`app/services/whatif.py`'s in-memory overlay). Any new feature or calculation must
preserve this separation.

The app behaves as single-user by default (no user-switching UI, no shared/household
view) but is built multi-tenant-ready: every domain table carries `user_id`, so a second
or third person can be added via CLI (`scripts/add_user.py`) or the admin-gated "Add a
user" form on `/settings` (`POST /settings/users`). Each created user gets a fully
isolated account — there is no shared-data or linked-accounts concept.

## Stack

FastAPI + SQLite + Jinja2 + HTMX. No ORM — raw `sqlite3` with hand-written parametrized
SQL (money-precision correctness over ORM convenience for this table count). No SPA
build step; HTMX is vendored locally at `app/static/vendor/htmx.min.js`, not loaded from
a CDN. No typed model/dataclass layer either — `sqlite3.Row` (dict-like, works directly
in Jinja templates) was sufficient throughout. No charting library — the dashboard's
trend sparkline (`app/sparkline.py`) is hand-rolled inline SVG generated server-side.

Conventions: money as `INTEGER` cents; dates as ISO-8601 UTC `TEXT`; booleans as
`INTEGER CHECK (x IN (0,1))`; every connection runs `PRAGMA foreign_keys=ON` and
`PRAGMA journal_mode=WAL`.

## Data model (SQLite)

Every domain table (all except `users`, `sessions`, `schema_migrations`) has a
`user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE`, indexed alongside
that table's most common filter — this is the multi-tenant-readiness mechanism: every
repository query filters by `user_id`, so nothing else changes if a second user exists.

Key tables:
- **accounts** — name, type (free text, `accounts_repo.SEED_TYPES` suggests
  checking/savings/cash/other), balance_cents, is_active.
- **debts** — name, type (free text, `debts_repo.SEED_TYPES` suggests a broader starter
  list), balance_cents, apr_bps, minimum_payment_cents, next_due_date, interest_status
  (accruing/not_accruing/promo_unknown), is_flexible_payment, priority (manual override),
  is_active.
- **obligations** — name, category (free text), amount_cents, is_recurring/
  recurrence_rule, due_date, is_required, is_paid, auto_pay, `last_bill_push_date`
  (dedup marker for the bill-due push).
- **committed_purchases** — name, category (`career_tool/school/car/debt/hobby/food/
  gambling/other`, still a fixed `CHECK`), amount_cents, amount_paid_cents,
  remaining_cents (generated column), status (`planned/ordered/arrived/partially_paid/
  paid/canceled`), order_date, expected_arrival_date, payment_deadline, priority.
- **income_events** — source, expected_amount_cents, expected_date, confidence
  (confirmed/likely/uncertain), is_received, is_recurring/recurrence_rule.
- **transactions** — polymorphic ledger: transaction_date, amount_cents (always
  positive), account_id, category, memo, target_type + exactly one of
  debt_id/obligation_id/committed_purchase_id (or none for `target_type='other'` /
  `'spending'`). Direction is carried by `target_type`, not the sign: `'other'` is
  the inflow type (income received), everything else is an outflow. `'spending'` is
  ordinary spending tied to no commitment — kept distinct from `'other'` precisely
  so the two can't be confused for each other.
- **snapshots** — one per user per day: cash_on_hand, total_debt, net_position,
  reserved_cash, safe_to_spend, forecast_position, protected_floor, window_days.
  Auto-captured on first dashboard view each day.
- **settings** — plain `(user_id, key) -> value` store, no migration needed for a new
  key. `calc.DEFAULT_SETTINGS` is the fallback until a user actually saves a value.
- **users** — username, password_hash, is_active, is_admin, auth_mode
  (password/totp/both), totp fields. No public registration route, ever.
- **sessions** — server-side revocable, per-device, CSRF secret per session.
- **bank_connections / bank_account_links / bank_sync_staging / bank_transaction_staging**
  — SimpleFIN bank-sync (see the module list below).
- **push_subscriptions** — one row per subscribed browser/device (`endpoint` unique).

Full column-level DDL lives in `migrations/0001_initial.sql` plus each subsequent
numbered migration.

## Calculation rules

Every query below is implicitly scoped by `WHERE user_id = :current_user_id`.

```
cash_on_hand   = SUM(accounts.balance_cents WHERE is_active)
total_debt     = SUM(debts.balance_cents WHERE is_active)
net_position   = cash_on_hand - total_debt
```

**Reserved-cash window** (`window_end`), per `settings.reserved_window_mode`:
- `next_paycheck`: earliest future `income_events.expected_date` (any confidence);
  falls back to `end_of_month` if none.
- `end_of_month`: last calendar day of current month.
- `fixed_days`: `today + reserved_window_fixed_days`.

**Reserved cash** (locked formula):
```
obligations_reserved = SUM(obligations.amount_cents WHERE NOT is_paid AND due_date <= window_end)

debts_reserved = SUM(debts.minimum_payment_cents
                      WHERE is_active AND next_due_date <= window_end)
                  -- required/scheduled minimum only; extra principal paydown is
                  -- tracked separately, never auto-reserved

committed_purchases_reserved =
    SUM(remaining_cents WHERE status IN ('ordered','arrived','partially_paid'))   -- always reserved
  + SUM(remaining_cents WHERE status='planned' AND payment_deadline <= window_end)

reserved_cash = obligations_reserved + debts_reserved + committed_purchases_reserved
```

**Safe to spend** (primary dashboard number, cash already in hand only):
```
safe_to_spend = cash_on_hand - reserved_cash - settings.protected_savings_floor_cents
```
Allowed to go negative — that's a meaningful signal, not clamped.

**Forecast position** (separate view only, never the default number):
```
forecast_window_end = today + settings.forecast_window_days
confirmed_set = {'confirmed'} or {'confirmed','likely'} per settings.forecast_income_confidence

forecast_income = SUM(income_events.expected_amount_cents
                       WHERE NOT is_received AND expected_date <= forecast_window_end
                       AND confidence IN confirmed_set)

forecast_position = cash_on_hand + forecast_income
                     - obligations_reserved(at forecast_window_end)
                     - debts_reserved(at forecast_window_end)
                     - committed_purchases_reserved(at forecast_window_end)
                     - total_debt
```

**Debt priority ranking:**
1. Manually pinned debts (`priority != 0`) sort first in user-set order.
2. Remaining debts split into Tier A (interest_status IN accruing/promo_unknown — unknown
   treated as maximally risky) before Tier B (not_accruing OR is_flexible_payment).
3. Tier A ordered by APR desc (NULL APR sorts as highest), then balance desc.
4. Tier B ordered by due_date asc, then balance asc (quick wins).

**Committed-purchase remaining amount:** `remaining_cents = amount_cents - amount_paid_cents`
(SQLite generated column).

**Debt payoff projection** (`calc.py::debt_payoff_projection`) — a pure, read-time-only
computed value, not part of the safe-to-spend/forecast formulas above. Standard monthly
amortization; `not_accruing` projects at 0%, unknown APR returns no projection, and a
minimum payment that doesn't cover monthly interest returns `payoff_impossible`.

**What-if / hypothetical:** no table — `app/services/whatif.py` builds a throwaway
in-memory SQLite DB seeded with a copy of the user's real rows plus hypothetical entries,
then runs it through the *same* `calc.py` functions used everywhere else. Never persisted
until the user confirms via a real create action.

## Project Structure

```
budget/
  README.md, DEVELOPMENT.md, CLAUDE.md, ARCHITECTURE.md, IMPLEMENTATION_HISTORY.md
  (PLAN.md, IDEAS.md, MAKING_PUBLIC.md, codex.md, snapshot.md, design/ are
  git-ignored -- working notes and a past design-audit input, kept local-only,
  not part of the public repo)
  app/
    main.py, config.py, db.py, security.py, deps.py, money.py, templating.py,
    ratelimit.py, sparkline.py, useragent.py, crypto.py, totp_qr.py
    repositories/    one module per table (accounts, debts, obligations,
                      committed_purchases, income_events, snapshots,
                      settings, bank_sync, transactions, push_subscriptions)
    services/         calc.py, whatif.py, export.py, recurrence.py, bills.py,
                      payments.py, bank_sync.py, bank_transactions.py, narrative.py,
                      digest.py, totp.py, push.py, bill_reminders.py,
                      low_balance_alert.py, bill_detection.py
    routers/          auth, dashboard, hubs, quick_actions (/today), accounts, debts,
                      obligations, committed_purchases, income_events,
                      forecast, snapshots, ledger, export, settings, bank
    templates/        base.html, login.html, dashboard.html, partials/ (nav.html,
                      _dashboard_summary.html, _ledger_rows.html), hubs/, bank/,
                      ledger/, one directory per entity (list.html, form.html,
                      _row.html)
    static/           css/app.css, vendor/htmx.min.js, manifest.webmanifest, sw.js,
                      icons/
  migrations/          0001_initial.sql onward, runner.py
  scripts/             init_db.py, add_user.py, reset_password.py, reset_totp.py,
                      snapshot.py, backup.py, bank_sync.py, digest.py, set_admin.py,
                      make_snapshot.py   # cron-friendly CLIs + doc tooling
  tests/                conftest.py, plus repository/service/route test files per area
  data/                # gitignored, holds budget.db + backups/
```

One deliberate departure from an earlier sketch of this app: no `models/` typed-
dataclass layer — `sqlite3.Row` proved sufficient throughout, never added.

## Layering

- `app/repositories/` — one module per table, raw SQL only, no business logic. Every
  function takes `user_id` explicitly and filters by it. Create/update functions return
  success/failure by row-count (`cur.rowcount > 0`), which is how cross-user ownership
  checks happen.
- `app/services/calc.py` — the calculation engine above. Any discrepancy with this file's
  "Calculation Rules" section is a code bug, not a spec change.
- `app/services/whatif.py` — hypothetical overlay, described above.
- `app/services/export.py` — per-table CSV export and full JSON backup/restore, scoped
  to one user's data. Restore never trusts backed-up row IDs (a single global
  `AUTOINCREMENT` sequence shared across all users) — assigns fresh IDs and remaps
  `transactions`' FK references via an ID map built during the same restore. CSV cells
  are checked for leading `=`/`+`/`-`/`@` (formula injection) and prefixed with `'`.
- `app/services/recurrence.py` — pure-stdlib date math (weekly/biweekly/monthly/yearly,
  with day-of-month/leap-day clamping) for rolling a recurring obligation/income event
  forward. `app/services/bills.py` builds on it for the `/today` quick-actions screen.
- `app/sparkline.py` — hand-rolled inline SVG trend line.
- `app/services/payments.py` — orchestrates recording a payment against a committed
  purchase from the `/today` quick-payment form: calls
  `committed_purchases_repo.record_payment`, debits the default account, writes a
  `transactions` row.
- `app/services/bank_sync.py` — SimpleFIN Bridge HTTP client (`claim_setup_token`/
  `fetch_accounts`), pure functions, no DB access, SSRF-hardened via an exact-hostname
  allowlist. Tested fully offline via `httpx.MockTransport`. `fetch_accounts` parses
  both SimpleFIN's `balance` (posted/ledger) and optional `available-balance` (posted
  minus pending holds) fields. Which one gets applied is decided in
  `app/routers/bank.py::sync_connection`/`scripts/bank_sync.py::sync_connection`, not
  here: debt-mapped links always use `balance` (SimpleFIN doesn't populate
  `available-balance` meaningfully for loan/credit products — confirmed 2026-08-05,
  came back as `0.00` regardless of real balance); account-mapped links use
  `available-balance` only when the user has opted in via the
  `bank_sync_use_available_balance` setting (default off — this changes the "cash on
  hand" number, so it's opt-in rather than a silent behavior change).
  `within_auto_apply_threshold(old, new, max_change)` is the other pure decision
  function here, backing the `bank_sync_auto_apply` setting (default off, threshold
  default $250 via `bank_sync_auto_apply_max_change_cents`): a freshly-staged
  balance within the threshold of the currently-stored one applies immediately
  (`_try_auto_apply` in both `bank.py::sync_connection` and
  `scripts/bank_sync.py::sync_connection` — duplicated between them for the same
  reason the available-balance logic is) instead of waiting on manual review at
  `/bank/{id}/review`. Exists because the manual-only review step was found
  2026-08-30 to silently stall `safe_to_spend` for days at a time — sync succeeded
  daily but nothing ever prompted the user to go apply it. A jump bigger than the
  threshold still falls through to manual review regardless of the setting, so a bad
  SimpleFIN read can't silently overwrite a real balance.
- `app/services/bank_transactions.py` — resolves a staged bank transaction three ways.
  `confirm_obligation_match`/`confirm_purchase_match` mark the commitment paid, write a
  ledger row, **and** debit the matched account — deliberately, to close the window
  where `reserved_cash` has already dropped but `cash_on_hand` hasn't caught up; the
  next sync's absolute `SET` supersedes the interim debit rather than stacking on it.
  `record_as_spending` absorbs a transaction that isn't a commitment at all ("just
  spending"), writing a `target_type='spending'` ledger row with an optional category —
  and does **not** debit, because there's no `reserved_cash` drop to compensate for and
  the posted amount is already inside the synced balance.
- `app/services/bill_detection.py` — finds probable recurring bills in raw bank
  transaction history so one can be confirmed with a tap instead of typed in by hand.
  Pure functions, no DB or network. A candidate must clear three gates — consistent
  normalized merchant, amount spread ≤ 25%, and a median gap matching a supported
  recurrence rule — which is what keeps variable spending at one merchant (a gas
  station visited weekly) off the suggestion list. Suggests only; the route persists.
- `app/services/totp.py` — pure TOTP secret/URI generation + replay-protected code
  verification, no DB access.
- `app/services/digest.py` — daily digest email via Resend's HTTP API. Structured
  **WHAT HAPPENED → COMING UP → WHERE YOU STAND**: recent activity leads, the standing
  balance is supporting context at the bottom, and the subject line carries the day's
  movement (`"Budget: down $32.40 — $965.52 safe to spend"`). Opening with a figure
  that can sit unchanged for days reads as noise rather than news; when genuinely
  nothing moved it says so in as many words instead of restating the number.
- `app/services/narrative.py` — "why did safe-to-spend move?", diffs today's live
  `safe_to_spend` against yesterday's snapshot and names the top 1-2 ledger movers.
- `app/services/push.py` — Web Push sending (RFC 8030/8291) via `pywebpush`. VAPID keys
  read lazily from the environment.
- `app/services/bill_reminders.py` — bundles unpaid obligations due today/tomorrow into
  one push (`last_bill_push_date` dedup marker per obligation).
- `app/services/low_balance_alert.py` — hysteresis-gated low-safe-to-spend push: fires
  once on the drop below a user-set threshold, silently re-arms once recovered above it.
- `app/services/email_theme.py` — the one HTML shell for outgoing mail (`digest.py`,
  `request_access.py`). Same dark tokens as `app/static/css/app.css` (kept in sync by
  hand, inlined per element because mail clients don't reliably honor stylesheets), every
  user-supplied string HTML-escaped. Senders build one list of `(heading, lines)`
  sections and derive *both* the plain-text fallback and the HTML body from it, so the
  two can never disagree; Resend gets `text` + `html` in the same payload.
- `app/crypto.py` — `Fernet` encrypt/decrypt, used for the SimpleFIN Access URL
  (`BANK_SYNC_ENCRYPTION_KEY`) and TOTP secrets (`TOTP_ENCRYPTION_KEY`, a distinct key
  so a leak of one doesn't expose the other). Keys read lazily inside `encrypt()`/
  `decrypt()`, never at import time.
- `app/routers/` — thin HTTP layer over services/repositories. Every mutating route
  (`POST`/`PUT`/`DELETE`) carries `Depends(verify_csrf_token)` except `POST /login`
  (protected instead by `app/ratelimit.py`, an in-process rate limiter).
  `app/routers/hubs.py` groups the per-entity screens into 4 top-level nav destinations
  (Home/Money/Activity/More) instead of 11. `app/routers/quick_actions.py` (mounted at
  `/today`) is the dashboard's quick-action forms — the one place using htmx
  out-of-band swaps.
- `app/templates/` — one directory per entity (`list.html`, `form.html`, `_row.html`),
  all extending `base.html`. Classic form POST+redirect for create/edit, HTMX `DELETE`
  for row removal. CSRF token embedded in `<meta name="csrf-token">`, auto-attached to
  htmx requests via `base.html`. `app/templates/partials/_dashboard_summary.html` is
  the `hx-swap-oob="true"` target shared by the full dashboard render and every
  `/today/*` quick-action response.
- `app/security.py` — session management (`list_sessions`/`delete_session_for_user`/
  `delete_other_sessions`), all ownership-scoped. `app/useragent.py` is a small
  dependency-free UA→label parser.

## PWA layer

- `app/static/manifest.webmanifest` — name/icons/`display: standalone`/theme colors,
  plus a `shortcuts` array (Today / Log a purchase / Review bank activity — Android/
  desktop Chrome only, no-ops invisibly on iOS).
- `app/static/sw.js`, served at the site root (`GET /sw.js`, registered with explicit
  `{ scope: '/' }` — a worker's default scope is the directory it's served from, so
  `/static/sw.js` would default to a scope that can never cover an actual page).
  Cache-first for `/static/*` GET requests only (version-stamped `CACHE_NAME`, bump on
  any deploy that changes files under `/static/`); passthrough everywhere else — no page
  content is ever cached, since a stale safe-to-spend number would be actively
  misleading. Also handles `push` (shows the notification, feature-detects and updates
  the Badging API) and `notificationclick` (focuses/opens the target URL, posts a
  `vibrate` message to the focused client since `navigator.vibrate` isn't available
  inside a service worker).
- `app/static/icons/` — hand-rolled PNG icons (192, 512, maskable-512) via a pure-stdlib
  PNG encoder, no imaging library dependency.
- Standalone-mode-only CSS (`@media (display-mode: standalone)` in `app/static/css/
  app.css`) — bottom nav bar with icon-over-label, `env(safe-area-inset-*)` padding,
  `overscroll-behavior: none` to kill pull-to-refresh. A normal browser tab is
  unaffected; only an installed/launched-from-home-screen instance gets this.
  `@view-transition { navigation: auto; }` gives every page a native cross-fade on
  browsers that support it, no JS.
- `GET /bank/unmatched-count` backs the Badging API — `base.html` sets/clears the app
  icon badge on load; `sw.js`'s push handler does the same so it updates without a
  foreground tab. Feature-detected; no-ops on Android Chrome (unsupported there) and
  works on desktop Chrome/Edge + iOS 16.4+ installed PWA.
- Haptics: a `vibrateShort()` helper in `base.html`, fired on successful `/today/*`
  quick-action requests (via `htmx:afterRequest`) and on notification clicks. Android
  Chrome only — iOS Safari has never supported the Vibration API.

## Screens / UX architecture

Top-level nav is 4 destinations — **Home · Money · Activity · More**
(`app/routers/hubs.py`) — not a flat module list; every entity still has its own full
route/CRUD screen, the hubs are landing pages that group them.

**Home (`/today`) is a cockpit, not a CRUD index.** Quick-action forms right on the
dashboard (update a balance, record a payment, add a purchase, mark
a bill paid, mark income received, "can I afford this?") each submit via HTMX and get
back a confirmation plus an out-of-band-swapped refresh of the whole summary section —
safe-to-spend, sparkline, debt priority all update in place, no navigation, no lost
scroll position. Three of these (bill paid, purchase payment, income received) also
debit/credit the default account and log a `transactions` row.

Dashboard shows: cash on hand, total debt, net position, reserved cash, safe-to-spend
(with an On track/Negative badge and the sparkline), next paycheck, next due payment,
debt priority list, forecast position (clearly labeled as forecast, never the primary
number).

**Activity (`/activity`) renders the recent ledger inline**, not just navigation cards,
with `/ledger` as the full history. This is the read side of the `transactions` ledger:
every write path (bills, payments, bank-transaction resolution) has always fed it, and
these are the screens that show it back. `app/templates/partials/_ledger_rows.html` is
shared by both so they can't drift on labelling or which direction an entry's sign
points.

**Detected bills (`/bank/suggestions`)** lists recurring-charge candidates from bank
history (`app/services/bill_detection.py`) with a one-tap "Add as bill" that creates a
recurring obligation — the low-friction path into the model that otherwise requires
typing every bill in by hand. The page reads locally-staged transactions by default;
its "Scan 120 days" button re-reads history straight from SimpleFIN and runs detection
in memory, deliberately **without** staging anything, since staging months of history
would dump hundreds of rows into the match queue for the user to clear by hand.

Settings (`/settings`) is split into independently-submittable cards, notifications
first: push subscribe/device list, digest email, and both alert thresholds (large
transaction, low safe-to-spend) in one "Notifications" card at the top; calculation
settings (protected floor, reserved window, forecast window, timezone, bank sync
cooldown) in a separate card below; then password, 2FA, login method, admin (if
applicable), and active sessions.

## Deployment

Runs on a VPS shared with other apps (nginx + systemd `--user` services, one Python
process per app, no Docker).

- **Domain**: `budget.flyboybyte.com`.
- **Process**: `budget.service`, a systemd `--user` unit (`~/.config/systemd/user/
  budget.service`), `Restart=always`, `enabled` (survives reboot):
  ```ini
  [Unit]
  Description=Budget cash-commitment tracker
  After=network.target

  [Service]
  Type=simple
  WorkingDirectory=/home/ubuntu/budget
  ExecStart=/home/ubuntu/budget/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 5758
  Restart=always
  RestartSec=5
  EnvironmentFile=/home/ubuntu/.config/budget/secrets.env

  [Install]
  WantedBy=default.target
  ```
- **Reverse proxy / TLS**: nginx vhost, `proxy_pass` to `127.0.0.1:5758`, TLS via
  Certbot (auto-renews). `/login` is rate-limited at the nginx layer as defense-in-depth
  alongside `app/ratelimit.py`.
- **Deploy flow**: `./deploy.sh` (repo root) — runs local tests, pushes to GitHub, SSHes
  in to `git pull`, syncs deps, runs migrations, restarts `budget.service`. Requires a
  local `.env` with `VPS_HOST=user@host` (gitignored).
- **Secrets**: `~/.config/budget/secrets.env` on the VPS, mode 600, outside the git
  working directory. Holds `BANK_SYNC_ENCRYPTION_KEY`, `TOTP_ENCRYPTION_KEY`,
  `BUDGET_RESEND_API_KEY`, `PUSH_VAPID_PUBLIC_KEY`/`PUSH_VAPID_PRIVATE_KEY`,
  `PUSH_VAPID_CLAIMS_EMAIL`. Wired in via `EnvironmentFile=` — a missing file fails the
  service to start loudly. **Losing an encryption key makes everything stored under it
  unrecoverable** (Fernet has no key-recovery path) — back both up outside the VPS.
- **Cron** (all entries invoke `.venv/bin/python -m scripts.<name>`, `cd ~/budget` first,
  logs to `~/budget/logs/<name>.log`). **Cron runs `/bin/sh`, which is `dash` on this
  VPS, not bash** — any secrets-loading line in a crontab entry must use the POSIX `.`
  command, never bash's `source`; verify any change to these lines via
  `sh -c '<exact crontab line>'`, never an interactive shell.
  ```cron
  55 23 * * * cd ~/budget && .venv/bin/python -m scripts.snapshot >> ~/budget/logs/snapshot.log 2>&1
  15 0  * * * cd ~/budget && .venv/bin/python -m scripts.backup --keep 30 >> ~/budget/logs/backup.log 2>&1
  0 6  * * * cd ~/budget && set -a && . ~/.config/budget/secrets.env && set +a && .venv/bin/python -m scripts.bank_sync >> ~/budget/logs/bank_sync.log 2>&1
  0 7  * * * cd ~/budget && set -a && . ~/.config/budget/secrets.env && set +a && .venv/bin/python -m scripts.digest >> ~/budget/logs/digest.log 2>&1
  ```
  The 7am `digest` run also fires the bill-due and low-safe-to-spend pushes
  (`scripts/digest.py::send_pushes_for_user`), independent of whether the digest email
  itself sends.
- **Backups**: `scripts/backup.py`/`scripts/snapshot.py`, cron-friendly (no-arg = all
  active users, `--user <username>` = one). Land in `data/backups/` (gitignored),
  `--keep 30` prunes to the 30 most recent per user. Off-box copying is still on the
  operator — the cron job protects against same-disk corruption/deletion, not VPS loss.

### Operational security notes

- Treat `data/backups/*.json` and CSV exports as sensitive as the live DB.
- Never expose `uvicorn` directly — it must stay behind nginx/TLS.
- `app/ratelimit.py` is in-process and resets on restart — a valid tradeoff for this
  single-process deployment, revisit if the topology ever changes.
- Periodically test an actual *restore* from a backup file, not just that backups get
  created.
- TOTP has no backup/recovery codes (deliberate) — `scripts/reset_totp.py` on the VPS is
  the only way back in if a user loses their device with no admin around.

## Adding a 2nd or 3rd user

Because every domain table is already scoped by `user_id`, enabling multi-person use is
a data-only operation, not a migration or a rewrite:
1. CLI: `python -m scripts.add_user <username> <password>`.
2. In-app, admin-gated: `POST /settings/users` (`app/routers/settings.py::add_user`).

No settings rows are pre-seeded for a new user; `calc.get_setting()` falls back to
`DEFAULT_SETTINGS` until they visit `/settings` themselves. Every dashboard/CRUD/
forecast/export/settings view shows only their own rows.

## Explicit non-goals

No shared/household budgets or linked accounts, no investing/net-worth tracking, no
receipt scanning, no AI advice, no mobile app (a PWA, not a native app — see the PWA
layer above), no subscription billing, no public SaaS features, no *public/
unauthenticated* self-serve registration route. "No charting libraries" is about
avoiding a bundled dependency, not about never showing a trend line (the sparkline is
hand-rolled SVG). Offline page rendering and background-sync-queued form submissions are
deliberately out of scope — a cached-and-replayed money-moving action against a balance
that's since changed would be actively misleading, not just stale.

## Verification

What every change should be checked against:
- `pytest` (`.venv/bin/python -m pytest tests/ -q`) — repositories, the calc engine
  (especially the reserved-cash/debt-minimum formula and debt-priority ranking), and
  **user isolation** (a query scoped to user A never returns user B's rows).
- A real running server, not just `TestClient` — several real bugs (a SQLite
  cross-thread connection error, a session cookie not sliding its expiration, two bugs
  that made push notifications completely non-functional, `display-mode: standalone`
  CSS not rendering as expected) only ever surfaced by actually running `uvicorn`/a real
  browser, despite full test coverage passing. Client-side JS/CSS specifically needs a
  real browser (Playwright) — `TestClient`/curl never execute JS.
- Export round-trip: export JSON backup, wipe DB, restore, confirm all tables match
  (including cross-user ID-collision safety).

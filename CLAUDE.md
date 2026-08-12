# Claude Quota — iCUE Widget for Corsair Xeneon Edge

## What this project is

A native iCUE widget for the Corsair Xeneon Edge that displays the user's Anthropic Claude
plan quota: the **5-hour rolling window** and the **7-day weekly window**, as two labeled
progress bars with percentage and reset time.

## Architecture (three layers — strict separation)

1. **Collector** (`collector/claude_quota.py`, Python 3 stdlib only)
   - Reads the OAuth access token from the Claude credentials file:
     `%USERPROFILE%\.claude\.credentials.json` → `claudeAiOauth.accessToken`
   - Calls `GET https://api.anthropic.com/api/oauth/usage` with headers:
     `Authorization: Bearer <token>` and `anthropic-beta: oauth-2025-04-20`
   - Writes normalized `data/latest.json` with `utilization` (0–100) and `resets_at`
     (ISO 8601) for both windows.
   - **Polls no more often than every 15 minutes** (endpoint is aggressively rate limited).
   - On any failure, keeps serving the last good result (adds `stale: true`).
   - **Auto-refreshes the OAuth token** (user-approved): if the access token is expired or
     the API returns 401, it POSTs the refresh token to
     `https://console.anthropic.com/v1/oauth/token` (Claude Code's public client id),
     backs up the credentials file to `.credentials.json.claude-quota.bak`, then atomically
     writes back only `accessToken`/`refreshToken`/`expiresAt`. At most one refresh + one
     usage retry per cycle. Disable with `CLAUDE_QUOTA_NO_REFRESH=1`.
   - Logs to `collector/collector.log` (truncated at 1 MB); exits if port already bound.
2. **Localhost HTTP server** (same Python process) — serves `data/latest.json` at
   `http://127.0.0.1:8765/latest.json` with `Access-Control-Allow-Origin: *`.
3. **Widget** (`widget/ClaudeQuota/`) — pure renderer. Fetches only the localhost JSON.
   **Holds no tokens, never calls Anthropic directly.**

## Security rules

- No token, credential, or account identifier may ever appear in the widget files,
  in `dist/`, or in anything committed to git.
- `data/` and all credential patterns are gitignored. Keep it that way.
- The collector only ever sends the token to `api.anthropic.com` over HTTPS.

## Key decisions (user-confirmed)

- Layout: responsive, all Xeneon Edge sizes (S/M/L/XL, horizontal + vertical), token-driven CSS
- Style: iCUE native dark; supports Xeneon Edge "Custom Style" (textColor/backgroundColor/…)
- Bar colors by utilization: green < 70 %, amber 70–90 %, red > 90 %
- Runtime: Python (stdlib only), single process for collector + server
- Widget name **Claude Quota**, id `com.sir.claudequota`, author "Sir", MIT, port **8765**

## Build workflow

- Source of truth for widget rules: `skills/icue-widget-builder/` (official Corsair skill —
  read `SKILL.md`, `docs/`, `references/` before touching widget code).
- Validate/package with the official CLI: `npm i -g icuewidget-cli`, then
  `icuewidget validate widget/ClaudeQuota` and `icuewidget package widget/ClaudeQuota`.
  Packaged output goes to `dist/ClaudeQuota.icuewidget`.
- Model policy: Sonnet implements clearly-specified tasks; Fable (or the strongest available
  model) plans, reviews, approves/denies, and sends work back with feedback.

## Operational lessons (learned the hard way — do not re-learn)

- **Verified live on 2026-08-12**: `/api/oauth/usage` returned `utilization` as plain
  percent (46.0, 8.0) with ISO `resets_at`. Percent-as-is normalization is correct.
- **Token refresh endpoint** (`console.anthropic.com/v1/oauth/token`) sits behind
  Cloudflare: requests without a real `User-Agent` get **403**. Always send the same
  header set as the usage call (`User-Agent`, `Accept`, `anthropic-beta`).
- Refresh responses: **400/401 = refresh token invalid** (user must `claude` → `/login`);
  403 = blocked request, not an auth problem.
- The usage endpoint **429s quickly** after repeated bad-auth hits and stays angry for
  a while. Collector has exponential backoff (cap 2 h), an **auth latch** (after a 401
  it stops calling out until the credentials file mtime changes), and a restart guard
  (skips the immediate startup poll if the last result was a fresh 429). Restarting
  the collector repeatedly is safe.
- **Claude Code can "look logged in" while its credentials file is dead** (user runs it
  inside Antigravity, which may inject its own API key). expiresAt was 19 days stale
  with an empty refreshToken. Fix is always: `claude` → `/login` in a Windows shell.
- **Microsoft Store `python` stub**: `where python` succeeds but running it prints
  "Python was not found" (exit 9009). All .bat launchers must probe interpreters by
  EXECUTING them (`X -c "raise SystemExit(0)"`), order: pythonw → pyw -3 → py -3 →
  python; always `start ""` detached so closing the window can't kill the collector.
- **Tests run in a sandbox that mounts the real repo folder** — the suite has a
  fail-loud guard so it can never write to the real `collector/collector.log` or
  `data/`. Keep that guard when adding tests.
- `collector/diagnose.bat` → `data/diag.json`: sanitized credential state (never token
  text), Credential Manager targets, scheduled-task state, running collector PIDs.
  This is the first tool to reach for on any "no data" report.
- Startup: `install_startup.bat` registers Task Scheduler task `ClaudeQuotaCollector`
  (ONLOGON, validated full interpreter path). The Claude desktop app does NOT need to
  auto-start — the collector is self-sufficient once a valid refresh token exists.

## Endpoint response shape (normalize defensively)

`/api/oauth/usage` returns JSON including `five_hour` and `seven_day` objects, each with
`utilization` (number, percent 0–100 — treated as percent as-is; if the API ever reports
fractions 0–1, set env `CLAUDE_QUOTA_ASSUME_FRACTION=1` — the collector logs a one-time
warning when values look fractional) and `resets_at` (ISO 8601). Extra windows (e.g. `seven_day_opus`) may exist; ignore them.
Never assume the shape is stable — collector must tolerate missing fields and keep last
good data.

## Normalized latest.json contract (what the widget consumes)

```json
{
  "ok": true,
  "stale": false,
  "fetched_at": "2026-08-12T08:00:00Z",
  "five_hour":  { "utilization": 42.0, "resets_at": "2026-08-12T11:00:00Z" },
  "seven_day":  { "utilization": 61.5, "resets_at": "2026-08-15T00:00:00Z" }
}
```

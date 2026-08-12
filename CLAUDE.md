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

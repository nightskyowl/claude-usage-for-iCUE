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
     `_MIN_POLL_SECONDS = 900` is a **hard floor**: `CLAUDE_QUOTA_POLL_SECONDS` below it is
     warned about and clamped, so setting it to e.g. 60 silently has no effect.
   - **Reset-aware scheduling**: on a successful cycle the next poll is pulled forward to
     just after the soonest upcoming `resets_at` (+15 s grace, floored at 60 s) when that
     lands sooner than the normal interval. This fixes the one visibly-wrong state — a
     window that has emptied still showing its pre-reset figure for a full interval. It only
     ever *shortens* the wait, never extends it, and is ignored entirely during 429 backoff
     and while the auth latch is active, so a rate-limited collector can't be dragged back
     into polling by a reset boundary. Costs ≤1 extra request per rollover.
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
   **Holds no tokens, never calls Anthropic directly.** Re-reads `latest.json` every
   **5 s** (`REFRESH_INTERVAL_MS`); this is localhost-only traffic and adds zero API load,
   so it is deliberately decoupled from the collector's 15-minute cadence.
   - A **1 Hz tick** (`renderLive()`) repaints only time-derived text: the reset countdown
     and the data-age line. It touches no bar geometry or colors, so it can never restart
     the `width`/`background-color` CSS transitions. Costs zero network traffic of any kind.
   - **Reset line**: countdown (`Resets in 4m 12s`) when the boundary is under 24 h away,
     seconds precision under an hour; absolute wall clock beyond a day, when `resets_at` is
     already past (stale snapshot), and never rendered at all when it is null.
   - **Never interpolate the percentage between polls.** A projected quota figure that reads
     high causes needless throttling and one that reads low walks the user into the cap. The
     countdown supplies the live-motion feel without inventing data.

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
- **Observed live at a window rollover (2026-08-12 18:40 +0700)**: immediately after the
  5-hour window reset, the endpoint returned `utilization: 0.0` with **`resets_at: null`** —
  the next boundary only reappears once you use Claude again and the window restarts. So
  `resets_at` is nullable *in the success path*, not just on failure. Anything reading it
  must skip nulls rather than assume a timestamp: `seconds_until_next_reset()` ignores that
  window (correctly falling back to the 7-day boundary and normal cadence), and the widget's
  `formatResetLine()` returns `''`. Confirmed no tight-poll loop results.
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
  If `schtasks` needs admin it falls back to the per-user HKCU Run key — `diagnose.bat`
  reports which one is active (`scheduled_task` vs `run_key`), and having only the Run key
  is a healthy state, not a failure.
- **Packaging is not deploying.** iCUE serves user widgets from
  `%APPDATA%\Corsair\CUE5\html_widgets\<guid>\`. To ship a widget change, extract
  `dist/ClaudeQuota.icuewidget` over that folder — replacing files **in place keeps the GUID**,
  and with it the widget's dashboard placement and configured properties (serverPort, colors).
  Importing through the iCUE UI instead registers a *new* GUID and forces re-placing and
  reconfiguring the widget.
- **iCUE reads widget files only once, at launch.** After deploying, iCUE must be fully quit
  from its **system-tray icon** (right-click → Quit) and relaunched. Clicking ✕ only minimises
  to the tray, so the process keeps rendering the version it loaded at startup — which looks
  exactly like a failed deployment. Check `Get-Process iCUE | Select-Object Id, StartTime`:
  an unchanged PID/StartTime means iCUE never restarted and the copy is not at fault.
- **Check the deployed version before diagnosing any widget complaint.** Read
  `html_widgets\<guid>\manifest.json` — the repo version is *not* necessarily what is on the
  glass. Widget 1.0.1/1.0.2/1.0.3 were all committed and pushed while iCUE was still running
  1.0.0, so a "the widget is slow" report was really about long-superseded code. Collector
  changes never drift this way: the collector runs from the repo directly.

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

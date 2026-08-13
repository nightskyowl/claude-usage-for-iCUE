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
   - **Default cadence is 15 minutes** (endpoint is aggressively rate limited). Since
     Phase 2b the floor and the default are **separate numbers**:
     `_DEFAULT_POLL_SECONDS = 900` is what you get with nothing configured, and
     `_MIN_POLL_SECONDS = 45` is the hard floor for an explicit `CLAUDE_QUOTA_POLL_SECONDS`
     opt-in (below it, warned about and clamped; a non-integer falls back to the *default*,
     never the floor, so a typo can't silently produce the fastest possible cadence).
     Keeping the default at 900 is what makes rollback free: removing the env var fully
     reverts the polling rate with no code change (`collector/rollback_cadence.bat`).
   - **Rate-limit protections are deliberately decoupled from `POLL_SECONDS`.** Both call
     `_rate_limit_guard_seconds()` = `max(POLL_SECONDS, _MIN_BACKOFF_BASE_SECONDS=900)`, read
     at call time so they cannot drift apart:
     - the 429 backoff ladder (`guard * 2**n`, capped at 2 h). Basing it on `POLL_SECONDS`
       alone (as it originally was) meant a fast cadence also made the retreat from a rate
       limit shallow — at 45 s it would take *eight* 429s to reach the 2 h cap instead of
       three, i.e. eight requests into an endpoint that has already said no.
     - `initial_poll_delay()`'s restart guard, which is what makes "restarting the collector
       repeatedly is safe" true. On a bare `POLL_SECONDS` it collapsed from 15 minutes to
       45 s at the fastest cadence — weakest exactly when polling hardest.
     A `Retry-After` header is honoured when it asks for **longer** than the ladder; a
     shorter one is ignored, since the ladder is the more conservative of the two.
     **This is a bug *class*, not two bugs**: before lowering any floor, grep every reference
     to the constant and ask whether each use is there because it *is* the cadence, or merely
     because it happened to be big enough. See `docs/phase-2b-plan.md`.
   - **Reset-aware scheduling**: on a successful cycle the next poll is pulled forward to
     just after the soonest upcoming `resets_at` (+15 s grace, floored at 60 s) when that
     lands sooner than the normal interval. This fixes the one visibly-wrong state — a
     window that has emptied still showing its pre-reset figure for a full interval. It only
     ever *shortens* the wait, never extends it, and is ignored entirely during 429 backoff
     and while the auth latch is active, so a rate-limited collector can't be dragged back
     into polling by a reset boundary. Costs ≤1 extra request per rollover.
   - **Activity-driven idle pausing**: the quota can only move when the user is using
     Claude or when a window rolls over, so polling at any other time returns a value we
     already hold. Claude Code appends to `~/.claude/projects/**/*.jsonl` on every turn,
     making the newest mtime there a free, local, zero-API "is the user working" signal
     (`is_claude_active()`). After a quiet period (`IDLE_AFTER_SECONDS`, default 300 s)
     the poller keeps sleeping, re-checking every 15 s, up to a heartbeat cap
     (`IDLE_MAX_WAIT_SECONDS`, default 1800 s).
     **Safety property: this can only ever *add* delay.** `wait_for_next_poll()` observes
     the delay chosen by `next_poll_delay()` in full *before* idleness is consulted, so
     the cadence floor, the 429 backoff and the reset-aware pull-forward all keep their
     exact meaning — it is structurally incapable of raising the request rate. An imminent
     reset is never held (`idle_extension_allowed()`), preserving the Phase 1 rollover fix.
     Fails **open**: a missing/unreadable/empty transcript tree, or `CLAUDE_QUOTA_NO_IDLE=1`,
     both mean "assume active", i.e. exactly the old behaviour. Blind spot: usage via
     claude.ai in a browser or the desktop app writes no transcript and reads as idle —
     the heartbeat is the backstop that bounds staleness there.
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
   **2 s** (`REFRESH_INTERVAL_MS`), backing off to `FAST_RETRY_MS` = 5 s until the first
   success and whenever the last fetch failed; this is localhost-only traffic and adds zero
   API load, so it is deliberately decoupled from the collector's 15-minute cadence.
   - A **1 Hz tick** (`renderLive()`) repaints only time-derived text: the reset countdown
     and the data-age line. It touches no wash geometry or colors, so it can never restart
     the `width` CSS transition. Costs zero network traffic of any kind.
   - **Reset line**: countdown (`Resets in 4m 12s`) when the boundary is under 24 h away,
     seconds precision under an hour; absolute wall clock beyond a day and when `resets_at`
     is already past (stale snapshot); **`Window not started`** when `resets_at` is null but
     the window payload exists (the real post-rollover state); empty only when there is no
     window payload at all, i.e. when we genuinely do not know.
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
- **Widget UI is "Editorial Bands" (v1.1.0)** — chosen by the user from five studies. Each
  window is a full-height band whose background wash *is* the meter, with a hero percentage,
  the label in `accentColor`, and the reset line inline on the right. See
  `docs/widget-ui-editorial-bands.md` for the rules that keep it coherent.
- Wash colors by utilization: green < 70 %, amber 70–90 %, red > 90 %. Written by JS as
  literal `rgba()` strings, **not** `color-mix()` — the latter is Chromium 111+ and the iCUE
  webview cannot be assumed that new.
- Runtime: Python (stdlib only), single process for collector + server
- Widget name **Claude Quota**, id `com.sir.claudequota`, author "Sir", MIT, port **8765**

## Build workflow

- Source of truth for widget rules: `skills/icue-widget-builder/` (official Corsair skill —
  read `SKILL.md`, `docs/`, `references/` before touching widget code).
- Validate/package with the official CLI: `npm i -g icuewidget-cli`, then
  `icuewidget validate widget/ClaudeQuota` and `icuewidget package widget/ClaudeQuota`.
  **The CLI writes the package next to the source as `widget/claude-quota.icuewidget`** (kebab
  case, CLI 0.4.47) — it does *not* write to `dist/`. Move it to `dist/ClaudeQuota.icuewidget`
  yourself; that path is this project's convention and what the deploy step reads.
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
- **Never put a pre-deploy backup inside `html_widgets\`.** iCUE scans *every* subfolder there
  at launch, so a `<guid>.bak-1.0.4` folder registers as a second widget with the **same**
  `id` and name. This happened on 2026-08-13 during the 1.1.0 deploy; iCUE relaunched seeing
  two "Claude Quota" widgets. Backups belong in `%LOCALAPPDATA%\ClaudeQuotaBackups\`.
  `robocopy <stage> <target> /E` (no `/PURGE`) is the deploy command that works — PowerShell
  `Copy-Item -Force` / `Move-Item -Force` trip the sandbox's removal guard.
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

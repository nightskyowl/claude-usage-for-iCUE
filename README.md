# Claude Quota — Corsair Xeneon Edge Widget

A native iCUE widget that shows your Anthropic Claude plan quota on the Corsair Xeneon Edge:
the **5-hour rolling window** and the **7-day weekly window** as two progress bars with
percentage and reset time. Green below 70 %, amber 70–90 %, red above 90 %.

![architecture](docs/architecture.md)

## How it works (three layers)

```
[Claude credentials file] → collector (poll ≤ 1/15 min) → data/latest.json
                                                             │
                          widget (pure renderer) ← localhost:8765/latest.json
                                  (re-reads every 5 s)
```

- **Collector + server** — `collector/claude_quota.py` (Python 3, stdlib only). Reads your
  OAuth access token from `%USERPROFILE%\.claude\.credentials.json`, calls Anthropic's
  OAuth usage endpoint every 15 minutes, caches the last good result, and serves it at
  `http://127.0.0.1:8765/latest.json`.
- **Widget** — `widget/ClaudeQuota/`. Renders the cached JSON. It holds no tokens and
  never talks to Anthropic.

### Freshness

The 15-minute cadence is set by Anthropic's usage endpoint, which rate-limits aggressively —
it is not a display limitation. Two things keep the display closer to reality without
raising the sustained request rate:

- The widget re-reads `latest.json` every **5 seconds**. That traffic never leaves your
  machine, so new collector data appears almost immediately rather than up to a minute later.
- The collector is **reset-aware**: when a usage window's `resets_at` falls sooner than the
  next scheduled poll, it polls just after that boundary instead. Without this, a window that
  emptied could keep displaying its old near-100 % figure for another full interval. This
  costs at most one extra request per rollover — a handful per day.

The percentages themselves can only be as fresh as the last poll, so the widget is explicit
about that rather than pretending otherwise:

- A **live countdown** (`Resets in 4m 12s`) replaces the static clock time whenever a reset is
  under a day away, ticking every second. It is derived purely from `resets_at` — no API call
  — so it is genuinely real-time even when the percentage beside it is minutes old.
- A **data-age line** (`Updated 3m ago`) reports exactly how stale the figures are, replacing
  a binary "Stale" badge with continuous information. While the collector is offline it keeps
  climbing against the last good reading.

Deliberately **not** implemented: interpolating or projecting the percentage between polls.
This gauge exists so you know where you actually stand — a projection that reads high makes
you throttle work needlessly, and one that reads low walks you into the cap unwarned. A
frozen figure that is true beats a moving one that is guessed.

## Requirements

- Windows with [Claude Code](https://claude.com/claude-code) logged in (the collector reads
  its credentials file; nothing is written to it)
- Python 3.9+ on PATH
- iCUE 5.44+ and a Xeneon Edge

## Setup

1. Start the collector: double-click `collector/run_collector.bat`
   (or `python collector/claude_quota.py`).
   Optional: `collector/install_startup.bat` registers it to start at logon.
2. Import the widget: double-click `dist/ClaudeQuota.icuewidget`, or in iCUE press **+**
   in the Widgets panel and pick the file.
3. Add **Claude Quota** to your Xeneon Edge layout.

## Building from source

```
npm i -g icuewidget-cli
icuewidget validate widget/ClaudeQuota
icuewidget package widget/ClaudeQuota
```

Built with the official [Corsair iCUE Widget Builder skill](https://github.com/Corsair-Labs/icue-widget-builder).

## Troubleshooting

- **First stop for any problem**: run `collector/diagnose.bat`, then read `data/diag.json`
  (sanitized — no secrets) and `collector/collector.log`.
- **Widget shows `--%` / "Collector offline"** — the collector isn't running or the port
  is taken. Run `collector/restart_collector.bat` and check `collector/collector.log`.
- **`data/latest.json` says `HTTP 401`** — the access token expired. The collector now
  refreshes it automatically (backing up `.credentials.json` first). If refresh fails,
  open Claude Code and run `/login`, then `restart_collector.bat`.
- **Percentages look ~100× too low** — set `CLAUDE_QUOTA_ASSUME_FRACTION=1`.
- Disable automatic token refresh with `CLAUDE_QUOTA_NO_REFRESH=1`.

## Security

- Your OAuth token never leaves your machine except to `api.anthropic.com` over HTTPS.
- The widget and the packaged `.icuewidget` contain no secrets.
- `data/` (cached usage) and all credential patterns are gitignored.

## License

MIT

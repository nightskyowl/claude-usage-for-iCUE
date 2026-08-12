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
```

- **Collector + server** — `collector/claude_quota.py` (Python 3, stdlib only). Reads your
  OAuth access token from `%USERPROFILE%\.claude\.credentials.json`, calls Anthropic's
  OAuth usage endpoint every 15 minutes, caches the last good result, and serves it at
  `http://127.0.0.1:8765/latest.json`.
- **Widget** — `widget/ClaudeQuota/`. Renders the cached JSON. It holds no tokens and
  never talks to Anthropic.

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

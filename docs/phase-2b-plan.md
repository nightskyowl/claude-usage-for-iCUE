# Phase 2b — sub-60-second glass latency

**Status: started 2026-08-12. Prerequisites done; live on rung 1 (300 s).**

| | State |
|---|---|
| Prerequisite code | ✅ done — floor/default split, backoff decoupled, `Retry-After` honoured |
| Widget | ✅ 1.0.4 deployed (`W` = 2 s) — **needs an iCUE tray quit + relaunch to take effect** |
| Cadence | ✅ rung 1 live: `CLAUDE_QUOTA_POLL_SECONDS=300` (persistent user env var) |
| Next | observe ≥24 h for 429s, then rung 2 (120 s) |

Phase 2a (activity-driven idle pausing) shipped earlier the same day and was confirmed in
production at 19:53–19:57: the collector held polls for 1125 s while idle and resumed
within one 15 s slice when a transcript was written again.

## Rollback — three levels, fastest first

Level 1 covers anything cadence-related, which is every failure mode this phase can
plausibly introduce. It has been tested end to end, not just written.

| Level | What it undoes | How | Needs iCUE restart? |
|---|---|---|---|
| **1. Cadence** | the faster polling | `collector\rollback_cadence.bat` | no |
| **2. Code** | all 2b code changes | `git checkout phase-2a-stable` | no |
| **3. Widget** | widget 1.0.4 → 1.0.3 | copy `%LOCALAPPDATA%\ClaudeQuotaBackups\widget-1.0.3-<guid>\*` over `%APPDATA%\Corsair\CUE5\html_widgets\<guid>\` | **yes — tray quit** |

Level 1 works without any code change because the *default* cadence is still 900 s; the
fast cadence is purely an opt-in env var. Removing that variable is a complete rollback of
the polling rate, and everything else (idle pausing, reset-aware scheduling, the decoupled
backoff) is cadence-independent and keeps working.

Known-good restore points:
- git tag **`phase-2a-stable`** (pushed to origin) — the last commit before 2b
- widget 1.0.3 folder backup at `%LOCALAPPDATA%\ClaudeQuotaBackups\widget-1.0.3-<guid>`

## The target, stated precisely

The number the user reads on the Xeneon Edge must be at most **60 seconds** behind
reality — measured end to end, not per hop:

```
quota changes at Anthropic
  │
  ├─ P  collector poll interval ......... worst case: the full interval
  ├─ W  widget re-read interval ......... worst case: the full interval  (currently 5 s)
  └─ R  fetch + paint ................... ~milliseconds (localhost, one repaint)

worst-case glass latency  =  P + W + R  ≤  60 s
```

`W` costs nothing to shrink (localhost traffic, zero API load), so the risk budget
should be spent on `W` first and on `P` only as far as necessary.

| Setting | Worst case | Typical (half of each interval) |
|---|---|---|
| P=45 s, W=2 s | **47 s** ✓ | ~24 s |
| P=45 s, W=5 s | 50 s ✓ | ~25 s |
| P=55 s, W=5 s | 60 s — no margin | ~30 s |
| P=60 s, W=5 s | 65 s ✗ | ~33 s |

**Recommended target: P = 45 s, W = 2 s.** Meets the requirement with 13 s of headroom
for a slow request or a missed tick.

## Prerequisite code changes

These are not optional — two of them are safety regressions if skipped.

1. **Split the floor from the default.** `_MIN_POLL_SECONDS = 900` is currently *both*
   the default cadence and the hard floor. Split into `_DEFAULT_POLL_SECONDS = 900`
   (unchanged default) and `_MIN_POLL_SECONDS = 45` (new floor), so a fast cadence is
   opt-in via `CLAUDE_QUOTA_POLL_SECONDS` and revertible by unsetting one env var.

2. **Decouple the 429 backoff from `POLL_SECONDS`.** ⚠️ *Hard prerequisite.*
   The backoff is currently `min(POLL_SECONDS * 2**n, 7200)`. At P=900 the ladder is
   30 m → 1 h → 2 h: three failures to reach the cap. At P=45 it becomes
   90 s → 3 m → 6 m → 12 m → 24 m → 48 m → 1.6 h → 2 h: **eight** failures, i.e. eight
   requests fired into an endpoint that has already said no. Fix by basing the backoff
   on `max(POLL_SECONDS, 900)` so the retreat is as steep as it is today regardless of
   the polling cadence.

3. **Honour `Retry-After`** on the 429 response when present, rather than only the
   exponential ladder.

4. **Widget `REFRESH_INTERVAL_MS` 5000 → 2000**, version bump to 1.0.4. Free: localhost
   only, no API load. Deploy per the in-place GUID procedure in `CLAUDE.md`
   (GUID `e0faf4f8-4977-48dd-865f-721811ac0705`), then **fully quit iCUE from the system
   tray** and relaunch — clicking ✕ only minimises, and the old version keeps rendering.

## Why 2a had to come first

Phase 2a is what makes a fast cadence affordable. It confines fast polling to the hours
the user is actually working; idle hours stay on the 30-minute heartbeat.

Daily request budget, assuming ~4 h/day of active Claude use:

| Configuration | Active | Idle + resets | Total/day | vs. today |
|---|---|---|---|---|
| P=900, no 2a (before) | 96 | — | **96** | 1.0× |
| P=900, with 2a (now) | 16 | 45 | **61** | 0.6× |
| P=300, with 2a | 48 | 45 | **93** | 1.0× |
| P=120, with 2a | 120 | 45 | **165** | 1.7× |
| P=60, with 2a | 240 | 45 | **285** | 3.0× |
| P=45, with 2a | 320 | 45 | **365** | 3.8× |

The step to **P=300 is free**: with idle time no longer wasted, five-minute freshness
costs the same daily request count as the old fifteen-minute cadence did. Only the last
two rows spend real budget.

## Rollout ladder

One rung at a time. Each rung runs for at least a full day of normal use before the next.

| Rung | `CLAUDE_QUOTA_POLL_SECONDS` | Glass latency (worst) | Gate to advance |
|---|---|---|---|
| 0 | unset (900) | 15 m | 2a stable, idle holds observed in the log ✅ |
| 1 | 300 | ~5 m | **← live since 2026-08-12 20:10.** no 429 in `collector.log` for 24 h |
| 2 | 120 | ~2 m | no 429 for 24 h |
| 3 | 60 | ~62 s | no 429 for 24 h |
| 4 | 45 | **~47 s** ✓ | target reached |

Rungs 3 and 4 assume widget 1.0.4 (`W`=2 s) is actually loaded — until iCUE has been
restarted from the tray, add 3 s to every latency figure above.

Advancing a rung is two commands, no code change and no redeploy:

```bat
setx CLAUDE_QUOTA_POLL_SECONDS 120
collector\restart_collector.bat
```

**Abort criterion:** a single 429 in `collector.log` that is not explained by an auth
storm or repeated restarts → drop back one rung and stay there. The endpoint is
undocumented; the observed limit is the only real specification.

**Why a ladder rather than jumping to 45 s:** the 900 s floor was an upfront defensive
guess, never a measurement (present in the first commit, hours before any 429 was seen).
The real limit is unknown in *both* directions, so the only way to find it is to walk
toward it while watching. The failure mode is asymmetric — a 429 costs up to two hours
of blank display — which is what makes walking, rather than jumping, worth the days.

## Verification at each rung

```
# requests actually made in the last day, and any rate limiting
Select-String -Path collector\collector.log -Pattern "poll ok"     | Measure-Object
Select-String -Path collector\collector.log -Pattern "429|backing off"

# is idle pausing still doing its job?
collector\diagnose.bat   →  data\diag.json  →  "activity" section
```

Expected healthy shape at rung 4: dense `poll ok` lines while working, an
`idle: ... holding polls` line within ~5 minutes of stopping, and an
`activity detected ...; polling now` line within ~15 s of starting again.

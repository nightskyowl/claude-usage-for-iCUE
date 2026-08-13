# Widget UI — "Editorial Bands" (v1.1.0)

Shipped 2026-08-13. Chosen by the user from five design studies rendered at true
840×344 proportions. This file records the reasoning so the layout is not
accidentally undone later.

## What was wrong with v1.0.4

Measured, not eyeballed. At S-H (840×344) the widget's `1vmin` unit is **3.44 px**:

| Region | Height |
|---|---|
| `.rows` available | 289 px |
| content inside each column | 67 px |
| **dead space** (`justify-content: center` splitting the remainder) | **2 × 111 px** |

Ink covered roughly a fifth of the panel. Three further faults:

1. The S-H media query hid `.row-reset` entirely, so the live countdown built in
   1.0.3 **never rendered on the user's device**. "Hide, don't shrink" (skill
   Rule 6) had been applied to the one number the user acts on.
2. At 0 % the bar track was an inert grey pill — indistinguishable from "no data".
3. The freshness line was 10 px at 45 % opacity, pinned 300 px from the figures
   it qualifies, despite being the only signal of whether to trust them.

The deployed files were byte-identical to the repo at the time, so this was a
design fault, not the version drift documented in `CLAUDE.md`.

## The design

```
┌──────────────────────────────────────────────────────────────┐
│ ● Updated 1m ago                        [STALE] [offline]    │  header rail
├──────────────────────────────────────────────────────────────┤
│▓▓▓▓▓▓▓│                                                      │
│  23 % 5-HOUR WINDOW                        Resets in 4h 21m  │  band
│▓▓▓▓▓▓▓│                                                      │
├──────────────────────────────────────────────────────────────┤
│▓▓▓▓▓│                                                        │
│  20 % WEEKLY WINDOW                        Resets Sun 01:00  │  band
│▓▓▓▓▓│                                                        │
└──────────────────────────────────────────────────────────────┘
```

**The row is the meter.** There is no separate track. Each `.quota-row` is a
full-height band and `.band-wash` fills it left-to-right to the utilization
percentage. This is what removes the dead space *by construction* — there is no
leftover vertical area to waste, because the meter occupies all of it.

## Rules that keep it coherent

- **The wash origin and the numeral must stay left-aligned to each other.** On
  wide slots (L-H 1688, XL-H 2536) the reading line is capped at 1100 px, but it
  is capped **left-anchored, never centered**. Centering was tried and looked
  broken: the wash's lit edge cut through the numeral and the two stopped reading
  as one object.
- **The lit right edge of the wash is `border-right`, not part of the gradient**,
  so at 0 % width it still renders at x=0. An empty window therefore reads as
  "untouched", never as "dead widget".
- **Wash colors are literal `rgba()` strings set from JS.** `color-mix()` is
  Chromium 111+; the iCUE webview cannot be assumed that new. The widget already
  relies on flex `gap` (Chromium 84+), which is a much safer floor.
- **The reset line is never hidden.** In this layout it sits inline on the right
  of the band and costs zero vertical space, which is what made deleting the old
  S-H suppression rule free.
- **The freshness line is not fine print.** It is `--font-fresh`
  (`5.4 × layout-unit`, ≈ 18.6 px at S-H) at 0.9 opacity and leads the header
  rail, with a status dot that encodes the same state as the badges in a form
  that survives being read at a distance. Sizing it down again re-creates the
  original complaint.
- **Vertical slots keep the S-V baseline** (skill Rule 5). Extra height is
  absorbed by the bands stretching and their content staying centered; the type
  does not grow.

## Rejected studies

Kept so the alternatives are not re-derived from scratch:

| Study | Idea | Why not |
|---|---|---|
| Instrument Panel | corrected version of v1.0.4 | fixes the layout but keeps the old voice |
| Twin Dials | two 240° arc gauges | less precision per pixel; round forms waste a 2.4:1 slot |
| Terminal Readout | mono, segmented meters, endpoint in footer | least legible at distance; narrow appeal |
| Ambient Field | each window a tank filling floor-to-ceiling | most decorative; no room to add a third window |

Interactive deck (all five, live feed switcher, true proportions):
<https://claude.ai/code/artifact/769d7d78-ad76-42b5-b1df-6cc836c75025>

## Verified before shipping

Rendered the **deployed** copy against the live collector, plus forced states
through the renderer: 96 % red wash, 78 % amber, `STALE` + `Collector offline`
badges, seconds-precision countdown ticking at 1 Hz, and the null-`resets_at`
path. Size slots checked: S-H 840×344, S-V 696×416, L-H 1688×696, L-V 696×1688.

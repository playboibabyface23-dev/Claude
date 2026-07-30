# PennyRun monitor

Zero-dependency Node CLI that walks a SKU watchlist through a price provider
and tracks each item down the clearance ladder. Node ≥ 18, nothing to
install. Run everything from this directory.

## Commands

```sh
node test-ladder.js       # offline self-check — run this first, exit 0 = good
node index.js once        # one sweep: read, diff, alert, write brief
node index.js watch       # sweep now, then every config.intervalMinutes
node index.js resolve SKU # SKU/UPC → internal product id (serpapi only)
node index.js export      # ripe items → data/board-export.json
```

## Providers

Selected by `PENNYRUN_PROVIDER` env var, falling back to `provider` in
`config.json`. All implement `lookup({ sku, store }) → Reading`.

| Provider | Needs | Notes |
| --- | --- | --- |
| `mock` | nothing | Offline. Walks each SKU one rung down the ladder per sweep (per process) — `watch` demos the full alert sequence. Tests drive it with scripted readings. |
| `serpapi` | `SERPAPI_API_KEY` | Home Depot engines. Only provider with `resolve` (SKU/UPC → product id). |
| `unwrangle` | `UNWRANGLE_API_KEY` | Home Depot detail API. |

Field mappings for the live providers are best-effort — check a raw response
against `src/providers.js` before trusting live numbers; fix mappings there
and nowhere else.

## config.json

| Key | Default | Meaning |
| --- | --- | --- |
| `provider` | `mock` | Fallback provider when `PENNYRUN_PROVIDER` is unset |
| `intervalMinutes` | `360` | `watch` sweep interval |
| `ripeDays` | `21` | Days after entering final markdown until "ripe" |
| `historyDepth` | `40` | Readings kept per SKU@store |
| `scoreThreshold` | `70` | Minimum score for the board export / "go check" list |
| `stores` | — | Store numbers to sweep |
| `skus` | — | `{ "sku": "...", "label": "..." }` — use `resolve` to get product ids |

**Budget check:** `calls/month = skus × stores × sweeps_per_day × 30`.
`watch` prints this projection at startup.

## Outputs (all in `data/`, all gitignored)

- `state.json` — per-SKU@store history (`historyDepth` readings deep), current
  stage, stage-entry timestamps, fired alerts, score. Delete it to start
  tracking fresh.
- `brief.md` — markdown brief rewritten each sweep; paste it to Claude and ask
  what to chase.
- `board-export.json` — written by `export`: items with score ≥
  `scoreThreshold`, hottest first. Paste into the field app's Board tab.

## Alerts

Fire at most once per SKU@store, in ladder order:

1. `first_markdown` — price hit a `.06` ending
2. `final_markdown` — price hit a `.03` ending
3. `ripe` — on final markdown for ≥ `ripeDays`
4. `pennied` — register price $0.01

A blip back up in price never resets clocks or re-arms alerts. Scoring:
penny = 100 (only a real $0.01 reading), final = 55 + up to 30 as the ripe
date approaches, first = 30, full = 5; inventory 1–5 adds 10, inventory 0
caps at 10 (likely already pulled).

## Code map

- `src/ladder.js` — pure functions only (no I/O, no clocks without `now`)
- `src/providers.js` — the three providers behind one interface
- `src/monitor.js` — sweep loop, state diffing, alert emission, brief, export
- `index.js` — argv dispatch and printing, nothing else
- `test-ladder.js` — plain-assert walk of the whole ladder; keep it green

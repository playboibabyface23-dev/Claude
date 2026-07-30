# PennyRun — project guide for Claude Code

Read this before touching anything. It is the contract for how the pieces fit
together and which invariants the code is built around.

## What PennyRun is

PennyRun hunts retail clearance "pennies": items whose price walks down a
predictable markdown ladder until the register price is $0.01 and the store is
supposed to pull them. The cent ending of the price encodes where an item is
on that ladder (Home Depot cadence, the default):

| Cent ending | Stage | Meaning |
| --- | --- | --- |
| anything else | `full` | not on the ladder |
| `.06` | `first` | first markdown — the clock starts |
| `.03` | `final` | final markdown — historically pennies out ~3 weeks later |
| price = `$0.01` | `penny` | penny — go now |

Two halves:

1. **The monitor** (`monitor/`) — a zero-dependency Node CLI that sweeps a
   watchlist of SKUs × stores through a price provider, diffs each item's
   ladder stage against history, emits alerts on transitions, predicts ripe
   dates, and exports a board of ripe items.
2. **The field app** (`app/PennyRun.jsx`) — React, no build step, renders as
   a Claude artifact. In-store companion: load the exported board, hunt the
   list, decode shelf prices, log what the register actually said.

They communicate only through `data/board-export.json`. The ladder is a
probability model; **the register price is the only truth**.

## Repo layout

```
pennyrun/
  CLAUDE.md            this file
  README.md            overview + quick start
  .env.example         provider keys + provider selection
  package.json         root scripts (thin wrappers over monitor/index.js)
  app/
    PennyRun.jsx       field app — one self-contained React component
  monitor/
    README.md          CLI + config reference
    index.js           CLI: once | watch | resolve | export
    config.json        watchlist + knobs (committed; demo values by default)
    src/ladder.js      PURE functions — cent ending → stage, proximity score, ripe date
    src/providers.js   serpapi | unwrangle | mock, one interface: lookup() → Reading
    src/monitor.js     sweep loop, state diffing, alert emission, Claude brief, export
    test-ladder.js     walks a fake SKU down the ladder, asserts alerts fire in order
    data/              gitignored: state.json, brief.md, board-export.json
```

## Commands

```sh
cd monitor
node test-ladder.js       # offline, no keys, verifies scoring end to end — run this first
node index.js once        # single sweep
node index.js watch       # loop on config.intervalMinutes
node index.js resolve SKU # SKU/UPC → internal product id (serpapi only)
node index.js export      # ripe items → data/board-export.json
```

`npm test` / `npm run once` etc. from the repo root are the same commands.
There is no `npm install` and no build step anywhere.

## Architecture invariants (the load-bearing walls)

- **`src/ladder.js` is pure.** No I/O, no env, no network, no implicit
  clocks — every time-dependent function takes `now`. All prices go through
  integer cents (`toCents`) to dodge floating point. If a function needs to
  fetch or persist, it belongs in `monitor.js` or `providers.js`, not here.
- **Providers are interchangeable.** Every provider implements
  `lookup({ sku, store }) → Reading` where
  `Reading = { sku, store, price, inventory, ts?, source }` with `null` for
  unknowns. Nothing outside `providers.js` may know which provider is in use
  (`resolve` being serpapi-only is the single sanctioned exception, enforced
  in `index.js`). Live-provider field mappings are best-effort — verify
  against provider docs before trusting live numbers, and fix mappings only
  in `providers.js`.
- **The mock provider must never set `ts`.** `sweep()` stamps its own clock;
  that is what lets `test-ladder.js` replay weeks of readings with an
  injected `now`. The default (script-less) mock walks each SKU one rung down
  the ladder per lookup **within a process** — so `watch` demos the full
  alert sequence offline, while repeated `once` runs stay at full price.
- **Alerts fire at most once per SKU@store, in ladder order:**
  `first_markdown → final_markdown → ripe → pennied` (`ALERT_ORDER` in
  `monitor.js`; `item.alerted` in state is the memory). A straight-to-penny
  jump still fires the earlier alerts first, same timestamp. A price blip
  back up never resets stage clocks or re-arms alerts (`stageRank` guard).
- **State keeps at most `historyDepth` readings (default 40) per SKU@store.**
  `data/` is gitignored in its entirety; state, briefs, and board exports are
  operational artifacts, never committed.
- **One flaky lookup must not kill a sweep.** Provider errors are collected
  per item and reported at the end.
- **The app duplicates `stageFor` on purpose** (it must render with no
  imports beyond React). If the ladder ever changes, change it in
  `src/ladder.js` and `app/PennyRun.jsx` in the same commit, and update
  `test-ladder.js` and the Decoder tab's help text.

## State schema (`monitor/data/state.json`)

```json
{
  "lastSweep": "2026-07-30T12:00:00.000Z",
  "items": {
    "1004-123-456@0121": {
      "sku": "1004-123-456",
      "store": "0121",
      "label": "LED shop light",
      "readings": [{ "price": 9.03, "inventory": 4, "ts": "…", "source": "serpapi" }],
      "stage": "final",
      "stageSince": { "first": "…", "final": "…" },
      "alerted": { "first_markdown": "…", "final_markdown": "…" },
      "score": 88
    }
  }
}
```

`stageSince.final` drives the ripe date (`+ config.ripeDays`, default 21) and
the score's time component. Scores: penny = 100, final = 55 + up to 30 as the
ripe date approaches, first = 30, full = 5; inventory 1–5 adds 10, inventory
0 caps the score at 10 (already pulled). Only a confirmed $0.01 reading may
score 100.

## API call budget

```
calls/month = skus × stores × sweeps_per_day × 30
```

Every knob in that formula is in `config.json` (`skus`, `stores`,
`intervalMinutes` ⇒ sweeps/day). `watch` prints the projection at startup —
check it against your provider plan before leaving it running. Example:
10 SKUs × 2 stores × 4 sweeps/day × 30 = 2,400 calls/month.

## Hard rules

- `node test-ladder.js` must pass offline with no keys before any push. It is
  a plain script (no test framework) — exit 0 is the contract.
- No runtime dependencies, no build system, anywhere. The monitor is `node:`
  builtins + `fetch`; the app is one JSX file.
- Never commit `data/`, `.env`, or API keys. `config.json` is committed but
  must hold only demo/personal-free values in the repo.
- Data access goes through the provider APIs (serpapi, unwrangle) the user
  pays for, or the mock. Do not add direct scraping of retailer websites.
- Keep sweep cadence honest: don't lower `intervalMinutes` below hourly by
  default, and keep the budget line in `watch` accurate.
- Penny policy varies by store and pennies are YMMV by design — keep wording
  in briefs and the app factual ("historically ~3 weeks"), not promissory.

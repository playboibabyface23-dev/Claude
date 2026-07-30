# PennyRun

Ride the clearance ladder down to a penny. Retail clearance prices encode
their markdown stage in the cent ending — `.06` first markdown, `.03` final
markdown, and roughly three weeks after `.03` an item "pennies out" to a
register price of $0.01. PennyRun watches the ladder so you walk in the day
it matters.

Two halves, no server, no dependencies:

- **Monitor** (`monitor/`) — a Node CLI that sweeps your SKU watchlist across
  stores through a price API (serpapi or unwrangle, or an offline mock),
  alerts on every ladder transition, predicts ripe dates, writes a
  paste-to-Claude brief, and exports a board of ripe items.
- **Field app** (`app/PennyRun.jsx`) — React with no build step; renders as a
  Claude artifact. Load the exported board in-store, hunt the list, decode
  any shelf price, and log what the register actually said — the register is
  the only truth.

## Quick start

Requires Node ≥ 18. Nothing to install.

```sh
cd monitor
node test-ladder.js       # offline, no keys, verifies scoring end to end — run this first
node index.js once        # single sweep
node index.js watch       # loop on config.intervalMinutes
node index.js resolve SKU # SKU/UPC → internal product id (serpapi only)
node index.js export      # ripe items → data/board-export.json
```

Out of the box everything runs on the **mock provider** — no keys, no
network. The mock walks each demo SKU one ladder rung per sweep within a
process, so `node index.js watch` demos the full alert sequence
(`first_markdown → final_markdown → ripe → pennied`) offline.

To go live: copy `.env.example` to `.env`, export a key, set
`PENNYRUN_PROVIDER=serpapi` (or `unwrangle`), and put real product ids and
store numbers in `monitor/config.json`. Use `resolve` to turn a SKU/UPC into
the internal product id the lookups need.

## Mind your API budget

```
calls/month = skus × stores × sweeps_per_day × 30
```

All three knobs live in `monitor/config.json` (`intervalMinutes` sets
sweeps/day). `watch` prints the projection at startup — check it against your
provider plan. 10 SKUs × 2 stores × every 6 hours = 2,400 calls/month.

## The loop

1. Monitor sweeps on a schedule; alerts fire as items step down the ladder.
2. `data/brief.md` is a markdown brief you can paste straight to Claude:
   what's ripe, what's close, where to go.
3. `node index.js export` writes `data/board-export.json`; paste it into the
   field app's Board tab and go hunt, hottest score first.
4. At the store: Decoder tab for shelf prices you stumble on, Log tab for
   what the register said. Shelf tags lie; scanners don't.

## Honest expectations

The ladder is a probability model, not a promise — cadences vary by retailer,
store, and season, and penny items are meant to be pulled from shelves.
PennyRun only reads data through the provider APIs you bring keys for; it
does not scrape retailer sites.

## For contributors (human or Claude)

`CLAUDE.md` is the contract: ladder semantics, provider interface, state
schema, alert-order invariant, hard rules. `monitor/README.md` has the full
CLI and config reference. `node monitor/test-ladder.js` must pass before any
push.

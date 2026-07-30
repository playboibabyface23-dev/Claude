# PennyRun — project guide for Claude Code

Read this file before touching anything else. It is the contract for how this
project fits together; the code is small on purpose and this file explains why.

## What PennyRun is

PennyRun is a two-part tool for pre-foreclosure wholesalers who hunt deals at
pennies on the dollar:

1. **The field app** (`app/PennyRun.jsx`) — a single-file React app used on a
   phone while driving for dollars. It logs distressed properties (address,
   distress signs, GPS, notes), tracks each lead through the pipeline, runs
   quick MAO math, and exports the whole lead list as JSON.
2. **The monitor** (`monitor/`) — a zero-dependency Node CLI run at a desk. It
   reads the JSON export from the field app and reports on the pipeline: leads
   going stale, auction dates coming up, and status counts.

The two halves never talk over a network. The **leads JSON file is the only
interface between them** — see "The leads schema" below. That schema is the
most important invariant in the repo.

## Repo layout

```
pennyrun/
  CLAUDE.md          this file
  README.md          user-facing overview and quick start
  .env.example       monitor configuration knobs (copy to .env)
  package.json       scripts + bin entry for the monitor
  app/
    PennyRun.jsx     the field app — one self-contained React component
  monitor/
    README.md        CLI usage in detail
    cli.js           entry point (also the `pennyrun-monitor` bin)
    lib.js           all logic: parsing, MAO, staleness, auctions, stats
    lib.test.js      node:test suite for lib.js
```

## Commands

```sh
npm test                      # node --test over monitor/ — no install needed
npm run monitor               # full report against ./leads.json
node monitor/cli.js stale ./leads.json --days 5
```

There is no build step and no `npm install`. If you find yourself wanting one,
stop and re-read "Hard rules" below.

## Architecture decisions (do not undo these casually)

- **`app/PennyRun.jsx` is deliberately one file with no imports beyond React
  and only inline styles.** It is designed to be pasted into any React host —
  a Claude artifact, a Vite scratch project, an existing app — with zero setup.
  Do not split it into modules, add a CSS file, or introduce a component
  library. If it grows, grow it inside the one file.
- **The monitor has zero runtime dependencies.** It uses only `node:` builtins
  (`fs`, `path`, `process`, `node:test`, `node:assert`). This keeps it
  runnable on any machine with Node ≥ 18 and nothing else.
- **All monitor logic lives in `lib.js`; `cli.js` only parses argv/env and
  prints.** Anything worth testing goes in `lib.js`. Never put logic in
  `cli.js` that a test would want to reach.
- **The 70% rule MAO math exists in both halves on purpose.** The app computes
  it live in the field; the monitor recomputes it from raw numbers in the
  export. They are kept in sync by the schema (raw `arv`/`repairs`/`fee` are
  exported, never a precomputed MAO), so duplication is contained. If you
  change the formula, change it in both `app/PennyRun.jsx` and
  `monitor/lib.js`, and update the tests.

## The leads schema (version 1)

The field app's "Export" button writes this shape; the monitor's
`parseLeads()` reads it (and also accepts a bare array of leads):

```json
{
  "version": 1,
  "exportedAt": "2026-07-30T15:04:05.000Z",
  "leads": [
    {
      "id": "lead_1722351845000",
      "address": "412 Fernwood Ave",
      "status": "contacted",
      "distressSigns": ["tall_grass", "boarded_windows"],
      "notes": "Neighbor says owner moved out in spring",
      "arv": 240000,
      "repairs": 45000,
      "fee": 10000,
      "auctionDate": "2026-08-19",
      "lastContact": "2026-07-24T18:00:00.000Z",
      "createdAt": "2026-07-12T16:30:00.000Z",
      "lat": 33.7489,
      "lng": -84.3902
    }
  ]
}
```

Rules:

- `status` is one of: `new`, `contacted`, `negotiating`, `under_contract`,
  `assigned`, `dead`. `assigned` and `dead` are terminal — the monitor
  ignores them for staleness and auction warnings.
- `arv`, `repairs`, `fee` are raw dollars or `null`. Never export a computed
  MAO; the monitor derives it.
- `auctionDate` is a date-only string (`YYYY-MM-DD`) or `null`.
- `lastContact` and `createdAt` are ISO timestamps; `lastContact` may be
  `null` (staleness then falls back to `createdAt`).
- Unknown extra fields must be tolerated by the monitor, not stripped —
  the field app may add fields before the monitor learns about them.

**Any change to this schema is a breaking change.** Bump `version`, keep
`parseLeads()` reading the old version, and update this section, both
READMEs, and the tests in the same commit.

## Domain glossary

- **Driving for dollars** — scouting neighborhoods in person for visibly
  distressed properties. This is what the field app is for.
- **Distress signs** — visible indicators (tall grass, boarded windows, code
  violation notices, full mailbox, tarped roof, apparent vacancy) that a
  property may have a motivated seller.
- **NOD / lis pendens** — public pre-foreclosure filings. `auctionDate` on a
  lead usually comes from these.
- **ARV** — after-repair value: what the property is worth fixed up.
- **MAO** — maximum allowable offer. PennyRun uses the 70% rule:
  `MAO = ARV × 0.70 − repairs − wholesale fee`, floored at 0. The 0.70 is
  configurable (`PENNYRUN_MAO_PERCENT` for the monitor, an input in the app).
- **Assignment / wholesale fee** — the wholesaler's profit for assigning the
  contract to an end buyer.

## Configuration

The monitor reads `.env`-style variables from the real environment (it does
not load a `.env` file itself — export them or use a wrapper). See
`.env.example` for the full list: leads-file path, stale-days threshold,
auction warning window, MAO percent. CLI flags always override env vars.

The field app has no configuration; it persists its state to
`localStorage` under the key `pennyrun.leads.v1`.

## Hard rules

- No runtime dependencies anywhere. `package.json` must keep an empty (or
  absent) `dependencies` block.
- Do not add a build system, bundler, or framework scaffold. This repo is a
  component file plus a CLI, and that is the point.
- Keep `npm test` passing; add or update tests in `monitor/lib.test.js`
  whenever `monitor/lib.js` changes behavior.
- Never commit real lead data. `leads.json`, `.env`, and anything under a
  `data/` directory stay untracked (see `.gitignore`).
- Addresses, notes, and GPS coordinates in exports are personal data about
  real homeowners. Never paste real exports into issues, PRs, tests, or docs;
  use invented fixtures like the example above.

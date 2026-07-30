# PennyRun monitor

A zero-dependency Node CLI that reads the JSON export from the PennyRun field
app and reports on your pipeline. Requires Node ≥ 18; nothing to install.

## Usage

```
pennyrun-monitor [command] [leads-file] [flags]
```

Run it from the repo as `node monitor/cli.js …` or `npm run monitor`, or link
the bin with `npm link` to get `pennyrun-monitor` on your PATH.

### Commands

| Command | What it shows |
| --- | --- |
| `report` | Everything below in one pass (default) |
| `stale` | Active leads with no touch in the last N days — your follow-up list |
| `auctions` | Active leads with an auction date inside the warning window, soonest first, with MAO |
| `stats` | Counts per status, active leads with full MAO numbers, potential fees in the active pipeline |

"Active" means any status except `assigned` and `dead`. Staleness uses
`lastContact`, falling back to `createdAt`; an active lead with no dates at
all is always flagged — those are the ones that slip.

### Flags and environment

Flags win over environment variables; both win over defaults.

| Flag | Env var | Default | Meaning |
| --- | --- | --- | --- |
| `--days N` | `PENNYRUN_STALE_DAYS` | 7 | Stale threshold in days |
| `--window N` | `PENNYRUN_AUCTION_WINDOW` | 14 | Auction warning window in days |
| `--percent P` | `PENNYRUN_MAO_PERCENT` | 0.70 | MAO percent (70% rule) |
| *(positional)* | `PENNYRUN_LEADS_FILE` | `./leads.json` | Path to the leads export |

### Examples

```sh
node monitor/cli.js                                 # full report on ./leads.json
node monitor/cli.js stale --days 5                  # tighter follow-up window
node monitor/cli.js auctions ~/exports/leads.json   # explicit file
node monitor/cli.js stats --percent 0.65            # conservative MAO math
```

## Input format

The monitor reads the version-1 export written by the field app's Export tab
(or a bare JSON array of leads). The schema — and the rules for changing it —
are documented in the repo-root `CLAUDE.md`. Unknown fields on a lead are
preserved, not stripped.

## Tests

```sh
npm test        # node --test monitor/
```

All logic lives in `lib.js`; `cli.js` only parses argv/env and prints. Put
any new behavior in `lib.js` with a test in `lib.test.js`.

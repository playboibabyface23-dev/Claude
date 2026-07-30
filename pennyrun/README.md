# PennyRun

Deals at pennies on the dollar. PennyRun is a lightweight toolkit for
pre-foreclosure wholesalers with two halves:

- **Field app** (`app/PennyRun.jsx`) — a single-file React app you run on your
  phone while driving for dollars. Log distressed properties as you spot them
  (address, distress signs, GPS, notes), move each lead through your pipeline,
  sanity-check numbers with a built-in MAO calculator, and export everything
  as JSON.
- **Monitor** (`monitor/`) — a zero-dependency Node CLI you run at your desk
  against that JSON export. It tells you which leads are going stale, which
  auction dates are bearing down on you, and where your pipeline stands.

No server, no accounts, no dependencies. The JSON export is the only bridge
between the two halves.

## Quick start

Requires Node 18 or newer. There is nothing to install.

```sh
# 1. Run the test suite
npm test

# 2. Use the field app
#    app/PennyRun.jsx is a self-contained React component (default export).
#    Paste it into any React host — a Claude artifact, a Vite scratch app,
#    an existing project — and it just works. State persists in localStorage.

# 3. Export leads from the app (Export tab) and save as leads.json,
#    then run the monitor:
npm run monitor                     # full report on ./leads.json
node monitor/cli.js stale           # only leads needing a follow-up
node monitor/cli.js auctions        # auctions inside the warning window
node monitor/cli.js stats           # pipeline counts + deal math
```

See `monitor/README.md` for every command and flag, and `.env.example` for
configuration (stale threshold, auction window, MAO percent).

## The math

PennyRun uses the standard 70% rule everywhere:

```
MAO = ARV × 0.70 − repairs − wholesale fee
```

The percentage is adjustable in both the app and the monitor
(`PENNYRUN_MAO_PERCENT`).

## Privacy

Lead exports contain addresses, notes, and GPS coordinates about real
homeowners. Keep `leads.json` and `.env` out of git (the included
`.gitignore` already does) and don't share exports.

## For contributors (human or Claude)

Read `CLAUDE.md` first — it documents the leads schema shared by both halves,
the architecture rules (single-file app, zero-dependency monitor), and the
hard rules for changes.

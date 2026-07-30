#!/usr/bin/env node
// PennyRun monitor CLI. Argv/env parsing and printing only — all logic is in
// lib.js (see CLAUDE.md). Usage: pennyrun-monitor [command] [file] [flags]

import { readFileSync } from "node:fs";
import process from "node:process";
import {
  parseLeads,
  staleLeads,
  upcomingAuctions,
  pipelineStats,
  daysSinceTouch,
  mao,
  formatMoney,
  STATUSES,
} from "./lib.js";

const COMMANDS = new Set(["report", "stale", "auctions", "stats"]);

const USAGE = `PennyRun monitor — pipeline reports from a field-app leads export

Usage:
  pennyrun-monitor [command] [leads-file] [flags]

Commands:
  report      everything below in one pass (default)
  stale       active leads with no touch in the last N days
  auctions    active leads with an auction date inside the window
  stats       pipeline counts and deal math

Flags:
  --days N      stale threshold in days      (env PENNYRUN_STALE_DAYS, default 7)
  --window N    auction window in days       (env PENNYRUN_AUCTION_WINDOW, default 14)
  --percent P   MAO percent, e.g. 0.70       (env PENNYRUN_MAO_PERCENT, default 0.70)
  --help        this text

The leads file defaults to $PENNYRUN_LEADS_FILE, then ./leads.json.`;

function envNumber(name, fallback) {
  const v = Number.parseFloat(process.env[name] ?? "");
  return Number.isFinite(v) ? v : fallback;
}

function parseArgs(argv) {
  const opts = {
    command: "report",
    file: process.env.PENNYRUN_LEADS_FILE || "./leads.json",
    days: envNumber("PENNYRUN_STALE_DAYS", 7),
    window: envNumber("PENNYRUN_AUCTION_WINDOW", 14),
    percent: envNumber("PENNYRUN_MAO_PERCENT", 0.7),
    help: false,
  };
  const positional = [];
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--help" || arg === "-h") opts.help = true;
    else if (arg === "--days") opts.days = Number.parseFloat(argv[++i]);
    else if (arg === "--window") opts.window = Number.parseFloat(argv[++i]);
    else if (arg === "--percent") opts.percent = Number.parseFloat(argv[++i]);
    else if (arg.startsWith("--")) throw new Error(`Unknown flag: ${arg}`);
    else positional.push(arg);
  }
  for (const p of positional) {
    if (COMMANDS.has(p)) opts.command = p;
    else opts.file = p;
  }
  return opts;
}

function printStale(leads, opts, now) {
  const stale = staleLeads(leads, { days: opts.days, now });
  console.log(`Stale leads (no touch in ${opts.days} days): ${stale.length}`);
  for (const lead of stale) {
    const since = daysSinceTouch(lead, now);
    const ago = since === null ? "no dates on file" : `${since}d since last touch`;
    console.log(`  ${lead.address}  [${lead.status}]  ${ago}`);
  }
}

function printAuctions(leads, opts, now) {
  const soon = upcomingAuctions(leads, { withinDays: opts.window, now });
  console.log(`Auctions within ${opts.window} days: ${soon.length}`);
  for (const { lead, days } of soon) {
    const when = days === 0 ? "TODAY" : `in ${days}d`;
    const offer = mao({ ...lead, percent: opts.percent });
    const offerNote = offer === null ? "" : `  MAO ${formatMoney(offer)}`;
    console.log(`  ${lead.address}  [${lead.status}]  ${lead.auctionDate} (${when})${offerNote}`);
  }
}

function printStats(leads, opts) {
  const stats = pipelineStats(leads, { percent: opts.percent });
  console.log(`Pipeline: ${stats.active} active of ${stats.total} leads`);
  for (const status of STATUSES) {
    if (stats.byStatus[status] > 0) {
      console.log(`  ${status.padEnd(15)} ${stats.byStatus[status]}`);
    }
  }
  console.log(`  Active leads with full MAO numbers: ${stats.withNumbers}`);
  console.log(`  Potential fees in active pipeline:  ${formatMoney(stats.potentialFees)}`);
}

export function run(argv, now = new Date()) {
  const opts = parseArgs(argv);
  if (opts.help) {
    console.log(USAGE);
    return 0;
  }

  let raw;
  try {
    raw = readFileSync(opts.file, "utf8");
  } catch {
    console.error(`Could not read leads file: ${opts.file}`);
    console.error("Export leads from the field app, or pass a path / set PENNYRUN_LEADS_FILE.");
    return 1;
  }

  const leads = parseLeads(raw);

  if (opts.command === "stale") printStale(leads, opts, now);
  else if (opts.command === "auctions") printAuctions(leads, opts, now);
  else if (opts.command === "stats") printStats(leads, opts);
  else {
    printStats(leads, opts);
    console.log("");
    printAuctions(leads, opts, now);
    console.log("");
    printStale(leads, opts, now);
  }
  return 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  try {
    process.exitCode = run(process.argv.slice(2));
  } catch (err) {
    console.error(err.message);
    process.exitCode = 1;
  }
}

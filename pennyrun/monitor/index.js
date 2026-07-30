#!/usr/bin/env node
// PennyRun monitor CLI. Commands: once | watch | resolve | export.
// Dispatch and printing only — logic lives in src/ (see CLAUDE.md).

import process from "node:process";
import { getProvider } from "./src/providers.js";
import {
  loadConfig,
  loadState,
  saveState,
  sweep,
  claudeBrief,
  exportBoard,
  writeBoard,
  writeBrief,
  scoreItem,
  BOARD_FILE,
  BRIEF_FILE,
} from "./src/monitor.js";

const USAGE = `PennyRun monitor — clearance-ladder tracker

Usage: node index.js <command>

  once           single sweep: read prices, diff stages, emit alerts, write brief
  watch          sweep now, then loop every config.intervalMinutes
  resolve <sku>  SKU/UPC → internal product id (serpapi only)
  export         ripe items (score ≥ config.scoreThreshold) → data/board-export.json

Provider comes from PENNYRUN_PROVIDER, falling back to config.provider
(serpapi | unwrangle | mock). Mock needs no keys and works offline.

API budget: calls/month = skus × stores × sweeps_per_day × 30`;

async function runSweep(config, provider) {
  const state = loadState();
  const { alerts, errors } = await sweep({ config, state, provider });
  saveState(state);
  const brief = claudeBrief(state, config);
  writeBrief(brief);
  console.log("");
  console.log(brief);
  console.log(
    `Sweep done: ${alerts.length} alert(s), ${errors.length} error(s). Brief → ${BRIEF_FILE}`,
  );
}

async function main() {
  const [command, arg] = process.argv.slice(2);
  if (!command || command === "--help" || command === "-h") {
    console.log(USAGE);
    return 0;
  }

  const config = loadConfig();
  const providerName = process.env.PENNYRUN_PROVIDER || config.provider;
  const provider = getProvider(providerName);
  config.provider = providerName;

  if (command === "once") {
    await runSweep(config, provider);
    return 0;
  }

  if (command === "watch") {
    const ms = Math.max(1, config.intervalMinutes) * 60 * 1000;
    const perDay = Math.round((24 * 60) / config.intervalMinutes);
    const monthly = config.skus.length * config.stores.length * perDay * 30;
    console.log(
      `Watching ${config.skus.length} SKU(s) × ${config.stores.length} store(s) every ` +
        `${config.intervalMinutes}m via ${provider.name} ≈ ${monthly.toLocaleString("en-US")} calls/month. Ctrl-C to stop.`,
    );
    await runSweep(config, provider);
    let running = false;
    setInterval(async () => {
      if (running) return; // never overlap sweeps
      running = true;
      try {
        await runSweep(config, provider);
      } catch (err) {
        console.error(`Sweep failed: ${err.message}`);
      } finally {
        running = false;
      }
    }, ms);
    return -1; // keep the process alive
  }

  if (command === "resolve") {
    if (!arg) throw new Error("Usage: node index.js resolve <sku-or-upc>");
    if (typeof provider.resolve !== "function") {
      throw new Error(`resolve needs the serpapi provider (current: ${provider.name})`);
    }
    const { productId, title } = await provider.resolve(arg);
    console.log(`${arg} → ${productId}  (${title})`);
    console.log("Use the product id as the `sku` in config.json.");
    return 0;
  }

  if (command === "export") {
    const state = loadState();
    const now = new Date();
    for (const item of Object.values(state.items)) {
      item.score = scoreItem(item, config, now); // re-score so clocks are current
    }
    const board = exportBoard(state, config, now);
    writeBoard(board);
    saveState(state);
    console.log(`${board.items.length} ripe item(s) → ${BOARD_FILE}`);
    console.log("Paste that file into the field app's Board tab.");
    return 0;
  }

  throw new Error(`Unknown command "${command}" — try --help`);
}

main().then(
  (code) => {
    if (code >= 0) process.exitCode = code;
  },
  (err) => {
    console.error(err.message);
    process.exitCode = 1;
  },
);

// The sweep engine: runs providers over the watchlist, diffs stages against
// state, emits alerts, and renders the Claude brief and board export.
// Injectable `provider` and `now` keep it testable offline (test-ladder.js).

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  stageFor,
  stageRank,
  ripeDate,
  isRipe,
  daysSince,
  proximityScore,
} from "./ladder.js";

const MONITOR_DIR = dirname(dirname(fileURLToPath(import.meta.url)));
export const DATA_DIR = join(MONITOR_DIR, "data");
export const STATE_FILE = join(DATA_DIR, "state.json");
export const BOARD_FILE = join(DATA_DIR, "board-export.json");
export const BRIEF_FILE = join(DATA_DIR, "brief.md");

const DEFAULTS = {
  provider: "mock",
  intervalMinutes: 360,
  ripeDays: 21,
  historyDepth: 40,
  scoreThreshold: 70,
  stores: [],
  skus: [],
};

// Alert types, in ladder order. Each fires at most once per SKU@store —
// `alerted` in state is the memory. test-ladder.js asserts this order.
export const ALERT_ORDER = ["first_markdown", "final_markdown", "ripe", "pennied"];

export function loadConfig(file = join(MONITOR_DIR, "config.json")) {
  const config = { ...DEFAULTS, ...JSON.parse(readFileSync(file, "utf8")) };
  if (config.skus.length === 0 || config.stores.length === 0) {
    throw new Error("config.json needs at least one entry in both `skus` and `stores`");
  }
  return config;
}

export function loadState(file = STATE_FILE) {
  try {
    return JSON.parse(readFileSync(file, "utf8"));
  } catch {
    return { items: {}, lastSweep: null };
  }
}

export function saveState(state, file = STATE_FILE) {
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(state, null, 2) + "\n");
}

export function scoreItem(item, config, now) {
  return proximityScore({
    stage: item.stage,
    daysOnFinal: item.stageSince?.final ? (daysSince(item.stageSince.final, now) ?? 0) : 0,
    inventory: lastReading(item)?.inventory ?? null,
    ripeDays: config.ripeDays,
  });
}

function lastReading(item) {
  return item.readings[item.readings.length - 1] ?? null;
}

// One pass over skus × stores. Mutates `state`, returns the alerts emitted
// this sweep. Provider errors are collected per item, not fatal — one flaky
// lookup must not kill a whole sweep.
export async function sweep({ config, state, provider, now = new Date(), quiet = false }) {
  const alerts = [];
  const errors = [];
  const say = quiet ? () => {} : (...args) => console.log(...args);

  for (const entry of config.skus) {
    const sku = typeof entry === "string" ? entry : entry.sku;
    const label = typeof entry === "string" ? entry : (entry.label ?? entry.sku);

    for (const store of config.stores) {
      const key = `${sku}@${store}`;
      let reading;
      try {
        reading = await provider.lookup({ sku, store });
      } catch (err) {
        errors.push({ key, message: err.message });
        continue;
      }
      reading.ts ??= now.toISOString();

      const item = (state.items[key] ??= {
        sku,
        store,
        label,
        readings: [],
        stage: null,
        stageSince: {},
        alerted: {},
      });
      item.label = label;
      item.readings.push({
        price: reading.price,
        inventory: reading.inventory,
        ts: reading.ts,
        source: reading.source,
      });
      if (item.readings.length > config.historyDepth) {
        item.readings.splice(0, item.readings.length - config.historyDepth);
      }

      const newStage = stageFor(reading.price);
      if (newStage && newStage !== item.stage) {
        // Only record forward motion; a price blip back up doesn't reset the
        // final-markdown clock or re-arm alerts.
        if (stageRank(newStage) > stageRank(item.stage)) {
          item.stageSince[newStage] ??= reading.ts;
        }
        item.stage = newStage;
      }

      const fire = (type) => {
        if (item.alerted[type]) return;
        item.alerted[type] = reading.ts;
        alerts.push({ type, key, sku, store, label, price: reading.price, ts: reading.ts });
      };
      if (stageRank(item.stage) >= stageRank("first")) fire("first_markdown");
      if (stageRank(item.stage) >= stageRank("final")) fire("final_markdown");
      if (
        item.stage !== "penny" &&
        item.stageSince.final &&
        isRipe(item.stageSince.final, config.ripeDays, now)
      ) {
        fire("ripe");
      }
      if (item.stage === "penny") {
        fire("ripe"); // keep ALERT_ORDER intact even on a straight-to-penny jump
        fire("pennied");
      }

      item.score = scoreItem(item, config, now);
    }
  }

  state.lastSweep = now.toISOString();

  for (const alert of alerts) {
    say(`🔔 ${alert.type.padEnd(14)} ${alert.label} @ store ${alert.store} — $${alert.price}`);
  }
  for (const e of errors) say(`⚠️  ${e.key}: ${e.message}`);
  return { alerts, errors };
}

// Markdown brief meant to be pasted straight to Claude ("what should I chase
// today?"). Also written to data/brief.md by `once`.
export function claudeBrief(state, config, now = new Date()) {
  const items = Object.values(state.items).sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
  const lines = [
    `# PennyRun brief — ${now.toISOString().slice(0, 10)}`,
    "",
    `Watching ${items.length} SKU/store pairs via \`${config.provider}\`; last sweep ${state.lastSweep ?? "never"}.`,
    "Score is 0–100 proximity to a penny (100 = confirmed $0.01 reading). Register price is truth.",
    "",
    "| Item | Store | Price | Inv | Stage | Days on final | Ripe date | Score |",
    "| --- | --- | --- | --- | --- | --- | --- | --- |",
  ];
  for (const item of items) {
    const last = item.readings[item.readings.length - 1] ?? {};
    const onFinal = item.stageSince?.final ? (daysSince(item.stageSince.final, now) ?? "—") : "—";
    const ripe = item.stageSince?.final ? ripeDate(item.stageSince.final, config.ripeDays) : "—";
    lines.push(
      `| ${item.label} | ${item.store} | ${last.price ?? "—"} | ${last.inventory ?? "—"} | ${item.stage ?? "—"} | ${onFinal} | ${ripe} | ${item.score ?? "—"} |`,
    );
  }
  const hot = items.filter((i) => (i.score ?? 0) >= config.scoreThreshold);
  lines.push(
    "",
    hot.length
      ? `**Go check:** ${hot.map((i) => `${i.label} (store ${i.store}, score ${i.score})`).join("; ")}.`
      : "**Nothing ripe yet.** Let the final-markdown clocks run.",
  );
  return lines.join("\n") + "\n";
}

// Ripe items for the field app's Board tab: everything at or above the score
// threshold, hottest first.
export function exportBoard(state, config, now = new Date()) {
  const items = Object.values(state.items)
    .filter((item) => (item.score ?? 0) >= config.scoreThreshold)
    .sort((a, b) => (b.score ?? 0) - (a.score ?? 0))
    .map((item) => {
      const last = item.readings[item.readings.length - 1] ?? {};
      return {
        sku: item.sku,
        label: item.label,
        store: item.store,
        price: last.price ?? null,
        inventory: last.inventory ?? null,
        stage: item.stage,
        score: item.score ?? null,
        ripeDate: item.stageSince?.final ? ripeDate(item.stageSince.final, config.ripeDays) : null,
        lastSeen: last.ts ?? null,
      };
    });
  return { version: 1, exportedAt: now.toISOString(), threshold: config.scoreThreshold, items };
}

export function writeBoard(board, file = BOARD_FILE) {
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(board, null, 2) + "\n");
}

export function writeBrief(brief, file = BRIEF_FILE) {
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, brief);
}

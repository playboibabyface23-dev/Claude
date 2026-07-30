// Offline end-to-end check: walks a fake SKU down the whole ladder and
// asserts the alerts fire in order. No keys, no network, no test framework —
// `node test-ladder.js` and a zero exit code is the contract. Run this first.

import assert from "node:assert/strict";
import { createMockProvider } from "./src/providers.js";
import { sweep, exportBoard, claudeBrief, ALERT_ORDER } from "./src/monitor.js";
import { stageFor, centEnding, ripeDate, proximityScore } from "./src/ladder.js";

let checks = 0;
function ok(fn, label) {
  fn();
  checks++;
  console.log(`  ✓ ${label}`);
}

console.log("ladder unit checks");
ok(() => assert.equal(centEnding(12.06), 6), "cent ending 12.06 → 6");
ok(() => assert.equal(stageFor(19.98), "full"), "19.98 is full price");
ok(() => assert.equal(stageFor(12.06), "first"), ".06 ending is first markdown");
ok(() => assert.equal(stageFor(9.03), "final"), ".03 ending is final markdown");
ok(() => assert.equal(stageFor(0.01), "penny"), "$0.01 is a penny");
ok(() => assert.equal(stageFor(0.06), "first"), "$0.06 total is still a .06 ending");
ok(() => assert.equal(stageFor(null), null), "null price is unknown, not full");
ok(
  () => assert.equal(ripeDate("2026-07-01T00:00:00Z", 21), "2026-07-22"),
  "ripe date = final markdown + 21 days",
);
ok(
  () => assert.equal(proximityScore({ stage: "penny" }), 100),
  "only a real penny scores 100",
);
ok(
  () =>
    assert.ok(
      proximityScore({ stage: "final", daysOnFinal: 22, inventory: 3 }) >
        proximityScore({ stage: "final", daysOnFinal: 2, inventory: 30 }),
      "score must rise",
    ),
  "ripe + low inventory outscores fresh + deep stock",
);
ok(
  () => assert.ok(proximityScore({ stage: "final", daysOnFinal: 25, inventory: 0 }) <= 10),
  "zero inventory caps the score — likely already pulled",
);

console.log("\nwalking FAKE-1 down the ladder");
const base = Date.parse("2026-07-01T09:00:00Z");
const day = (n) => new Date(base + n * 24 * 60 * 60 * 1000);

// One reading per sweep: full → first → final → still final (ripe) → penny.
const provider = createMockProvider({
  "FAKE-1@0121": [
    { price: 19.98, inventory: 12 },
    { price: 12.06, inventory: 9 },
    { price: 9.03, inventory: 6 },
    { price: 9.03, inventory: 3 },
    { price: 0.01, inventory: 2 },
  ],
});
const config = {
  provider: "mock",
  ripeDays: 21,
  historyDepth: 40,
  scoreThreshold: 70,
  stores: ["0121"],
  skus: [{ sku: "FAKE-1", label: "Fake shop light" }],
};
const state = { items: {}, lastSweep: null };

const fired = [];
const sweepDays = [0, 1, 5, 27, 30];
for (const n of sweepDays) {
  const { alerts, errors } = await sweep({ config, state, provider, now: day(n), quiet: true });
  assert.equal(errors.length, 0, `sweep on day ${n} had errors`);
  fired.push(alerts.map((a) => a.type));
}

const item = state.items["FAKE-1@0121"];

ok(() => assert.deepEqual(fired[0], []), "day 0 (19.98, full): no alerts");
ok(() => assert.deepEqual(fired[1], ["first_markdown"]), "day 1 (12.06): first_markdown");
ok(() => assert.deepEqual(fired[2], ["final_markdown"]), "day 5 (9.03): final_markdown");
ok(
  () => assert.deepEqual(fired[3], ["ripe"]),
  "day 27 (still 9.03, 22 days on final): ripe",
);
ok(() => assert.deepEqual(fired[4], ["pennied"]), "day 30 (0.01): pennied");
ok(
  () => assert.deepEqual(fired.flat(), ALERT_ORDER),
  "alerts fired exactly once each, in ladder order",
);

ok(() => assert.equal(item.stage, "penny"), "state landed on stage penny");
ok(() => assert.equal(item.score, 100), "penny scores 100");
ok(() => assert.equal(item.readings.length, sweepDays.length), "one reading kept per sweep");
ok(
  () => assert.equal(item.stageSince.final.slice(0, 10), "2026-07-06"),
  "final-markdown clock started day 5",
);

console.log("\nripe items reach the board and the brief");
const board = exportBoard(state, config, day(30));
ok(() => assert.equal(board.items.length, 1), "board export has the penny");
ok(() => assert.equal(board.items[0].sku, "FAKE-1"), "board item is FAKE-1");
ok(() => assert.equal(board.items[0].ripeDate, "2026-07-27"), "board carries the ripe date");

const brief = claudeBrief(state, config, day(30));
ok(() => assert.ok(brief.includes("Fake shop light")), "brief names the item");
ok(() => assert.ok(brief.includes("Go check:")), "brief tells you to go");

// Re-sweeping past the end of the script must not re-fire anything.
const again = await sweep({ config, state, provider, now: day(31), quiet: true });
ok(() => assert.deepEqual(again.alerts, []), "no duplicate alerts on re-sweep");

// History stays capped at historyDepth.
const tinyConfig = { ...config, historyDepth: 3 };
for (let n = 32; n < 40; n++) await sweep({ config: tinyConfig, state, provider, now: day(n), quiet: true });
ok(() => assert.equal(item.readings.length, 3), "history trimmed to historyDepth");

console.log(`\nok — ${checks} checks passed`);

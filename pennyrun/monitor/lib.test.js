import { test } from "node:test";
import assert from "node:assert/strict";
import {
  mao,
  parseLeads,
  staleLeads,
  upcomingAuctions,
  pipelineStats,
  daysSinceTouch,
  daysUntilAuction,
  formatMoney,
} from "./lib.js";

// Fixed "now" so date math is deterministic.
const NOW = new Date("2026-07-30T12:00:00Z");

function lead(overrides = {}) {
  return {
    id: "lead_1",
    address: "412 Fernwood Ave",
    status: "new",
    distressSigns: [],
    notes: "",
    arv: null,
    repairs: null,
    fee: null,
    auctionDate: null,
    lastContact: null,
    createdAt: "2026-07-01T12:00:00Z",
    ...overrides,
  };
}

test("mao applies the 70% rule", () => {
  assert.equal(mao({ arv: 200000, repairs: 30000, fee: 10000 }), 100000);
});

test("mao honors a custom percent", () => {
  assert.equal(mao({ arv: 100000, repairs: 0, fee: 0, percent: 0.65 }), 65000);
});

test("mao floors at zero and returns null without an ARV", () => {
  assert.equal(mao({ arv: 50000, repairs: 60000, fee: 10000 }), 0);
  assert.equal(mao({ arv: null, repairs: 1000 }), null);
  assert.equal(mao({}), null);
  assert.equal(mao({ arv: -5 }), null);
});

test("parseLeads reads a versioned export and a bare array", () => {
  const exported = JSON.stringify({ version: 1, exportedAt: "x", leads: [lead()] });
  assert.equal(parseLeads(exported).length, 1);
  assert.equal(parseLeads([lead(), lead({ id: "lead_2" })]).length, 2);
});

test("parseLeads normalizes bad fields but keeps unknown extras", () => {
  const [l] = parseLeads([
    { status: "bogus", arv: "not a number", customField: "keep me" },
  ]);
  assert.equal(l.status, "new");
  assert.equal(l.arv, null);
  assert.equal(l.address, "(no address)");
  assert.equal(l.customField, "keep me");
});

test("parseLeads rejects garbage and future versions", () => {
  assert.throws(() => parseLeads("{not json"), /not valid JSON/);
  assert.throws(() => parseLeads({ nope: true }), /must be an array/);
  assert.throws(() => parseLeads({ version: 2, leads: [] }), /version 2/);
});

test("staleLeads flags old active leads and skips terminal ones", () => {
  const leads = [
    lead({ id: "fresh", lastContact: "2026-07-28T12:00:00Z" }),
    lead({ id: "old", lastContact: "2026-07-10T12:00:00Z" }),
    lead({ id: "dead-old", status: "dead", lastContact: "2026-06-01T12:00:00Z" }),
    lead({ id: "assigned-old", status: "assigned", lastContact: "2026-06-01T12:00:00Z" }),
  ];
  const stale = staleLeads(leads, { days: 7, now: NOW });
  assert.deepEqual(stale.map((l) => l.id), ["old"]);
});

test("staleLeads falls back to createdAt and includes undated leads", () => {
  const leads = [
    lead({ id: "created-old", createdAt: "2026-07-01T12:00:00Z", lastContact: null }),
    lead({ id: "no-dates", createdAt: null, lastContact: null }),
  ];
  const stale = staleLeads(leads, { days: 7, now: NOW });
  assert.deepEqual(stale.map((l) => l.id).sort(), ["created-old", "no-dates"]);
});

test("daysSinceTouch prefers lastContact over createdAt", () => {
  const l = lead({ createdAt: "2026-07-01T12:00:00Z", lastContact: "2026-07-27T12:00:00Z" });
  assert.equal(daysSinceTouch(l, NOW), 3);
});

test("upcomingAuctions keeps only the window, soonest first", () => {
  const leads = [
    lead({ id: "soon", auctionDate: "2026-08-05" }),
    lead({ id: "today", auctionDate: "2026-07-30" }),
    lead({ id: "far", auctionDate: "2026-09-30" }),
    lead({ id: "past", auctionDate: "2026-07-01" }),
    lead({ id: "dead", status: "dead", auctionDate: "2026-08-01" }),
  ];
  const soon = upcomingAuctions(leads, { withinDays: 14, now: NOW });
  assert.deepEqual(soon.map((s) => s.lead.id), ["today", "soon"]);
  assert.equal(soon[0].days, 0);
});

test("daysUntilAuction returns null for missing or bad dates", () => {
  assert.equal(daysUntilAuction(lead(), NOW), null);
  assert.equal(daysUntilAuction(lead({ auctionDate: "someday" }), NOW), null);
});

test("pipelineStats counts statuses and sums active fees", () => {
  const leads = [
    lead({ id: "a", status: "new", arv: 200000, repairs: 30000, fee: 10000 }),
    lead({ id: "b", status: "under_contract", fee: 15000 }),
    lead({ id: "c", status: "assigned", fee: 12000 }),
    lead({ id: "d", status: "dead" }),
  ];
  const stats = pipelineStats(leads);
  assert.equal(stats.total, 4);
  assert.equal(stats.active, 2);
  assert.equal(stats.byStatus.new, 1);
  assert.equal(stats.byStatus.assigned, 1);
  assert.equal(stats.potentialFees, 25000); // assigned/dead fees excluded
  assert.equal(stats.withNumbers, 1); // only "a" has an ARV
});

test("formatMoney", () => {
  assert.equal(formatMoney(100000), "$100,000");
  assert.equal(formatMoney(null), "—");
});

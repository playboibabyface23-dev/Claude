// The clearance ladder — pure functions only. No I/O, no env, no implicit
// clocks: every time-dependent function takes `now`. If you need to fetch or
// persist something, you are in the wrong file (see CLAUDE.md).
//
// The ladder (Home Depot cadence, the default):
//   full   — anything not below; regular or promo price
//   first  — cents end .06: first markdown, ~25% off, clock starts
//   final  — cents end .03: final markdown; historically pennies out about
//            three weeks later (config.ripeDays, default 21)
//   penny  — $0.01: register price is one cent, item is due to be pulled
//
// The register is the source of truth; the ladder is a probability model.

export const STAGES = ["full", "first", "final", "penny"];

export const RIPE_DAYS_DEFAULT = 21;

const DAY_MS = 24 * 60 * 60 * 1000;

// Total price in integer cents, or null for garbage. All ladder math goes
// through cents to dodge floating-point (9.03 * 100 !== 903).
export function toCents(price) {
  if (!Number.isFinite(price) || price < 0) return null;
  return Math.round(price * 100);
}

// The cent ending as an integer 0–99 (12.06 → 6), or null.
export function centEnding(price) {
  const cents = toCents(price);
  return cents === null ? null : cents % 100;
}

// Cent ending → ladder stage. Null price → null (unknown, not "full").
export function stageFor(price) {
  const cents = toCents(price);
  if (cents === null) return null;
  if (cents === 1) return "penny";
  const ending = cents % 100;
  if (ending === 6) return "first";
  if (ending === 3) return "final";
  return "full";
}

// Position in the ladder; higher = closer to a penny. Unknown → -1.
export function stageRank(stage) {
  return STAGES.indexOf(stage);
}

// When a final-markdown item is expected to penny out: the day it entered
// `final` plus ripeDays. Returns an ISO date string or null.
export function ripeDate(finalSinceIso, ripeDays = RIPE_DAYS_DEFAULT) {
  const t = Date.parse(finalSinceIso);
  if (!Number.isFinite(t)) return null;
  return new Date(t + ripeDays * DAY_MS).toISOString().slice(0, 10);
}

export function daysSince(iso, now) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return null;
  return Math.floor((now.getTime() - t) / DAY_MS);
}

export function isRipe(finalSinceIso, ripeDays = RIPE_DAYS_DEFAULT, now) {
  const days = daysSince(finalSinceIso, now);
  return days !== null && days >= ripeDays;
}

// Proximity score, 0–100: how close is this item to being a penny worth
// driving for? Only an actual $0.01 reading scores 100.
//
//   penny                → 100
//   final                →  55 + up to 30 as daysOnFinal approaches ripeDays
//   first                →  30
//   full                 →   5
//   low inventory (1–5)  → +10 (stores penny out remnants, not pallets)
//   zero inventory       → capped at 10 (likely already pulled — stale lead)
export function proximityScore({
  stage,
  daysOnFinal = 0,
  inventory = null,
  ripeDays = RIPE_DAYS_DEFAULT,
} = {}) {
  if (stage === "penny") return 100;
  if (stage !== "first" && stage !== "final") return 5;

  let score = stage === "first" ? 30 : 55;
  if (stage === "final" && ripeDays > 0) {
    score += Math.round(30 * Math.min(1, Math.max(0, daysOnFinal) / ripeDays));
  }
  if (Number.isFinite(inventory)) {
    if (inventory === 0) return Math.min(score, 10);
    if (inventory <= 5) score += 10;
  }
  return Math.min(score, 99);
}

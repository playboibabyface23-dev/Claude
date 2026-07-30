// Core logic for the PennyRun monitor. cli.js only parses argv/env and
// prints; everything decision-shaped lives here so lib.test.js can reach it.

export const STATUSES = [
  "new",
  "contacted",
  "negotiating",
  "under_contract",
  "assigned",
  "dead",
];

// Terminal statuses are excluded from staleness and auction warnings.
export const TERMINAL_STATUSES = new Set(["assigned", "dead"]);

const DAY_MS = 24 * 60 * 60 * 1000;

// 70% rule: MAO = ARV × percent − repairs − fee, floored at 0.
// Returns null when ARV is missing/invalid — "no number" beats a wrong one.
export function mao({ arv, repairs = 0, fee = 0, percent = 0.7 } = {}) {
  if (!Number.isFinite(arv) || arv <= 0) return null;
  const r = Number.isFinite(repairs) ? repairs : 0;
  const f = Number.isFinite(fee) ? fee : 0;
  return Math.max(0, Math.round(arv * percent - r - f));
}

// Accepts the versioned export ({ version, leads: [...] }) or a bare array.
// Normalizes each lead but preserves unknown extra fields (schema rule:
// tolerate fields the monitor doesn't know about yet).
export function parseLeads(raw) {
  let data = raw;
  if (typeof raw === "string") {
    try {
      data = JSON.parse(raw);
    } catch (err) {
      throw new Error(`Leads file is not valid JSON: ${err.message}`);
    }
  }

  let leads;
  if (Array.isArray(data)) {
    leads = data;
  } else if (data && Array.isArray(data.leads)) {
    if (data.version !== undefined && data.version !== 1) {
      throw new Error(
        `Unsupported leads export version ${data.version} (this monitor reads version 1)`,
      );
    }
    leads = data.leads;
  } else {
    throw new Error(
      "Leads data must be an array of leads or an export object with a `leads` array",
    );
  }

  return leads.map((lead, i) => {
    if (!lead || typeof lead !== "object") {
      throw new Error(`Lead at index ${i} is not an object`);
    }
    const status = STATUSES.includes(lead.status) ? lead.status : "new";
    return {
      ...lead,
      id: lead.id ?? `lead_${i}`,
      address: typeof lead.address === "string" ? lead.address : "(no address)",
      status,
      distressSigns: Array.isArray(lead.distressSigns) ? lead.distressSigns : [],
      arv: Number.isFinite(lead.arv) ? lead.arv : null,
      repairs: Number.isFinite(lead.repairs) ? lead.repairs : null,
      fee: Number.isFinite(lead.fee) ? lead.fee : null,
      auctionDate: typeof lead.auctionDate === "string" ? lead.auctionDate : null,
      lastContact: typeof lead.lastContact === "string" ? lead.lastContact : null,
      createdAt: typeof lead.createdAt === "string" ? lead.createdAt : null,
    };
  });
}

function lastTouch(lead) {
  const iso = lead.lastContact ?? lead.createdAt;
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : null;
}

export function daysSinceTouch(lead, now = new Date()) {
  const t = lastTouch(lead);
  if (t === null) return null;
  return Math.floor((now.getTime() - t) / DAY_MS);
}

// Active leads whose last touch (lastContact, falling back to createdAt) is
// more than `days` days ago. Leads with no dates at all are included — an
// undated active lead is exactly the kind of thing that slips.
export function staleLeads(leads, { days = 7, now = new Date() } = {}) {
  return leads
    .filter((lead) => !TERMINAL_STATUSES.has(lead.status))
    .filter((lead) => {
      const since = daysSinceTouch(lead, now);
      return since === null || since > days;
    })
    .sort((a, b) => (daysSinceTouch(b, now) ?? Infinity) - (daysSinceTouch(a, now) ?? Infinity));
}

export function daysUntilAuction(lead, now = new Date()) {
  if (!lead.auctionDate) return null;
  const t = Date.parse(`${lead.auctionDate}T00:00:00`);
  if (!Number.isFinite(t)) return null;
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  return Math.round((t - today) / DAY_MS);
}

// Active leads with an auction date from today through `withinDays` out,
// soonest first. Past auction dates are excluded — that ship has sailed.
export function upcomingAuctions(leads, { withinDays = 14, now = new Date() } = {}) {
  return leads
    .filter((lead) => !TERMINAL_STATUSES.has(lead.status))
    .map((lead) => ({ lead, days: daysUntilAuction(lead, now) }))
    .filter(({ days }) => days !== null && days >= 0 && days <= withinDays)
    .sort((a, b) => a.days - b.days);
}

export function pipelineStats(leads, { percent = 0.7 } = {}) {
  const byStatus = Object.fromEntries(STATUSES.map((s) => [s, 0]));
  let potentialFees = 0;
  for (const lead of leads) {
    byStatus[lead.status] += 1;
    if (!TERMINAL_STATUSES.has(lead.status) && Number.isFinite(lead.fee)) {
      potentialFees += lead.fee;
    }
  }
  const active = leads.filter((l) => !TERMINAL_STATUSES.has(l.status));
  return {
    total: leads.length,
    active: active.length,
    byStatus,
    potentialFees,
    withNumbers: active.filter((l) => mao({ ...l, percent }) !== null).length,
  };
}

export function formatMoney(n) {
  if (!Number.isFinite(n)) return "—";
  return `$${Math.round(n).toLocaleString("en-US")}`;
}

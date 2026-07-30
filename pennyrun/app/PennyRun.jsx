// PennyRun field app — a single-file React component for logging distressed
// properties while driving for dollars. Deliberately self-contained: no
// imports beyond React, inline styles only, state in localStorage. Paste it
// into any React host and it works. See ../CLAUDE.md before restructuring.

import React, { useEffect, useMemo, useState } from "react";

const STORAGE_KEY = "pennyrun.leads.v1";

const STATUSES = ["new", "contacted", "negotiating", "under_contract", "assigned", "dead"];
const STATUS_LABELS = {
  new: "New",
  contacted: "Contacted",
  negotiating: "Negotiating",
  under_contract: "Under contract",
  assigned: "Assigned",
  dead: "Dead",
};
const STATUS_COLORS = {
  new: "#2563eb",
  contacted: "#0891b2",
  negotiating: "#d97706",
  under_contract: "#7c3aed",
  assigned: "#16a34a",
  dead: "#6b7280",
};

const DISTRESS_SIGNS = [
  ["tall_grass", "Tall grass"],
  ["boarded_windows", "Boarded windows"],
  ["code_violation", "Code violation notice"],
  ["full_mailbox", "Full mailbox"],
  ["tarp_roof", "Tarped roof"],
  ["vacant", "Looks vacant"],
];

// 70% rule — keep in sync with monitor/lib.js (see CLAUDE.md).
function mao({ arv, repairs = 0, fee = 0, percent = 0.7 }) {
  const a = Number.parseFloat(arv);
  if (!Number.isFinite(a) || a <= 0) return null;
  const r = Number.parseFloat(repairs) || 0;
  const f = Number.parseFloat(fee) || 0;
  return Math.max(0, Math.round(a * percent - r - f));
}

function money(n) {
  return n === null || !Number.isFinite(n) ? "—" : `$${Math.round(n).toLocaleString("en-US")}`;
}

function loadLeads() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

const S = {
  app: {
    maxWidth: 480,
    margin: "0 auto",
    padding: "12px 12px 80px",
    fontFamily: "system-ui, -apple-system, sans-serif",
    color: "#1f2937",
    background: "#f9fafb",
    minHeight: "100vh",
    boxSizing: "border-box",
  },
  header: { display: "flex", alignItems: "baseline", gap: 8, marginBottom: 12 },
  h1: { fontSize: 22, margin: 0, fontWeight: 800 },
  tagline: { fontSize: 12, color: "#6b7280" },
  tabs: { display: "flex", gap: 6, marginBottom: 14 },
  tab: (active) => ({
    flex: 1,
    padding: "10px 0",
    borderRadius: 8,
    border: "1px solid " + (active ? "#1f2937" : "#d1d5db"),
    background: active ? "#1f2937" : "#fff",
    color: active ? "#fff" : "#374151",
    fontSize: 13,
    fontWeight: 600,
    cursor: "pointer",
  }),
  card: {
    background: "#fff",
    border: "1px solid #e5e7eb",
    borderRadius: 10,
    padding: 12,
    marginBottom: 10,
  },
  label: { display: "block", fontSize: 12, fontWeight: 600, color: "#374151", margin: "10px 0 4px" },
  input: {
    width: "100%",
    padding: "10px",
    fontSize: 15,
    border: "1px solid #d1d5db",
    borderRadius: 8,
    boxSizing: "border-box",
  },
  signBtn: (on) => ({
    padding: "8px 10px",
    borderRadius: 999,
    border: "1px solid " + (on ? "#b45309" : "#d1d5db"),
    background: on ? "#fef3c7" : "#fff",
    color: on ? "#92400e" : "#374151",
    fontSize: 12,
    cursor: "pointer",
  }),
  primary: {
    width: "100%",
    padding: "12px",
    marginTop: 14,
    borderRadius: 8,
    border: "none",
    background: "#b45309",
    color: "#fff",
    fontSize: 15,
    fontWeight: 700,
    cursor: "pointer",
  },
  badge: (status) => ({
    display: "inline-block",
    padding: "2px 8px",
    borderRadius: 999,
    fontSize: 11,
    fontWeight: 700,
    color: "#fff",
    background: STATUS_COLORS[status] || "#6b7280",
  }),
  small: { fontSize: 12, color: "#6b7280" },
  select: { padding: "6px 8px", fontSize: 13, borderRadius: 6, border: "1px solid #d1d5db" },
  maoBox: {
    marginTop: 12,
    padding: 12,
    borderRadius: 8,
    background: "#1f2937",
    color: "#fff",
    textAlign: "center",
  },
};

const EMPTY_FORM = {
  address: "",
  distressSigns: [],
  notes: "",
  arv: "",
  repairs: "",
  fee: "",
  auctionDate: "",
};

export default function PennyRun() {
  const [tab, setTab] = useState("log");
  const [leads, setLeads] = useState(loadLeads);
  const [form, setForm] = useState(EMPTY_FORM);
  const [gps, setGps] = useState(null);
  const [statusFilter, setStatusFilter] = useState("all");
  const [calc, setCalc] = useState({ arv: "", repairs: "", fee: "", percent: "0.70" });
  const [flash, setFlash] = useState("");

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(leads));
    } catch {
      // storage full/unavailable — the in-memory list still works
    }
  }, [leads]);

  useEffect(() => {
    if (!flash) return;
    const t = setTimeout(() => setFlash(""), 2500);
    return () => clearTimeout(t);
  }, [flash]);

  function grabGps() {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(
      (pos) => setGps({ lat: pos.coords.latitude, lng: pos.coords.longitude }),
      () => setGps(null),
      { enableHighAccuracy: true, timeout: 8000 },
    );
  }

  function toggleSign(key) {
    setForm((f) => ({
      ...f,
      distressSigns: f.distressSigns.includes(key)
        ? f.distressSigns.filter((s) => s !== key)
        : [...f.distressSigns, key],
    }));
  }

  function saveLead() {
    if (!form.address.trim()) {
      setFlash("Address is required");
      return;
    }
    const num = (v) => (v === "" ? null : Number.parseFloat(v));
    const now = new Date().toISOString();
    setLeads((prev) => [
      {
        id: `lead_${Date.now()}`,
        address: form.address.trim(),
        status: "new",
        distressSigns: form.distressSigns,
        notes: form.notes.trim(),
        arv: num(form.arv),
        repairs: num(form.repairs),
        fee: num(form.fee),
        auctionDate: form.auctionDate || null,
        lastContact: null,
        createdAt: now,
        lat: gps?.lat ?? null,
        lng: gps?.lng ?? null,
      },
      ...prev,
    ]);
    setForm(EMPTY_FORM);
    setGps(null);
    setFlash("Lead saved");
  }

  function setStatus(id, status) {
    const now = new Date().toISOString();
    setLeads((prev) =>
      prev.map((l) => (l.id === id ? { ...l, status, lastContact: now } : l)),
    );
  }

  function removeLead(id) {
    setLeads((prev) => prev.filter((l) => l.id !== id));
  }

  const visibleLeads = useMemo(
    () => (statusFilter === "all" ? leads : leads.filter((l) => l.status === statusFilter)),
    [leads, statusFilter],
  );

  const exportJson = useMemo(
    () =>
      JSON.stringify(
        { version: 1, exportedAt: new Date().toISOString(), leads },
        null,
        2,
      ),
    [leads],
  );

  async function copyExport() {
    try {
      await navigator.clipboard.writeText(exportJson);
      setFlash("Copied — save as leads.json for the monitor");
    } catch {
      setFlash("Copy failed — select the text below instead");
    }
  }

  const calcResult = mao({ ...calc, percent: Number.parseFloat(calc.percent) || 0.7 });

  return (
    <div style={S.app}>
      <header style={S.header}>
        <h1 style={S.h1}>🪙 PennyRun</h1>
        <span style={S.tagline}>deals at pennies on the dollar</span>
      </header>

      <nav style={S.tabs}>
        {[
          ["log", "Log"],
          ["pipeline", `Pipeline (${leads.length})`],
          ["calc", "MAO"],
          ["export", "Export"],
        ].map(([key, label]) => (
          <button key={key} style={S.tab(tab === key)} onClick={() => setTab(key)}>
            {label}
          </button>
        ))}
      </nav>

      {flash && (
        <div style={{ ...S.card, background: "#ecfdf5", borderColor: "#6ee7b7", fontSize: 13 }}>
          {flash}
        </div>
      )}

      {tab === "log" && (
        <div style={S.card}>
          <label style={S.label}>Address *</label>
          <input
            style={S.input}
            placeholder="412 Fernwood Ave"
            value={form.address}
            onChange={(e) => setForm({ ...form, address: e.target.value })}
          />

          <label style={S.label}>Distress signs</label>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {DISTRESS_SIGNS.map(([key, label]) => (
              <button
                key={key}
                style={S.signBtn(form.distressSigns.includes(key))}
                onClick={() => toggleSign(key)}
              >
                {label}
              </button>
            ))}
          </div>

          <label style={S.label}>Notes</label>
          <textarea
            style={{ ...S.input, minHeight: 60 }}
            placeholder="What did you see? Who did you talk to?"
            value={form.notes}
            onChange={(e) => setForm({ ...form, notes: e.target.value })}
          />

          <div style={{ display: "flex", gap: 8 }}>
            {[
              ["arv", "Est. ARV $"],
              ["repairs", "Repairs $"],
              ["fee", "Your fee $"],
            ].map(([key, label]) => (
              <div key={key} style={{ flex: 1 }}>
                <label style={S.label}>{label}</label>
                <input
                  style={S.input}
                  type="number"
                  inputMode="numeric"
                  value={form[key]}
                  onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                />
              </div>
            ))}
          </div>

          <label style={S.label}>Auction date (from NOD / lis pendens, if known)</label>
          <input
            style={S.input}
            type="date"
            value={form.auctionDate}
            onChange={(e) => setForm({ ...form, auctionDate: e.target.value })}
          />

          <div style={{ marginTop: 10, display: "flex", alignItems: "center", gap: 8 }}>
            <button style={{ ...S.signBtn(!!gps), fontSize: 13 }} onClick={grabGps}>
              📍 {gps ? `${gps.lat.toFixed(4)}, ${gps.lng.toFixed(4)}` : "Tag GPS location"}
            </button>
          </div>

          {mao(form) !== null && (
            <div style={S.maoBox}>
              <div style={{ fontSize: 11, opacity: 0.7 }}>MAO (70% rule)</div>
              <div style={{ fontSize: 22, fontWeight: 800 }}>{money(mao(form))}</div>
            </div>
          )}

          <button style={S.primary} onClick={saveLead}>
            Save lead
          </button>
        </div>
      )}

      {tab === "pipeline" && (
        <div>
          <div style={{ ...S.card, display: "flex", alignItems: "center", gap: 8 }}>
            <span style={S.small}>Filter</span>
            <select
              style={S.select}
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
            >
              <option value="all">All statuses</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {STATUS_LABELS[s]}
                </option>
              ))}
            </select>
          </div>

          {visibleLeads.length === 0 && (
            <div style={{ ...S.card, textAlign: "center", color: "#6b7280" }}>
              No leads here yet. Go drive.
            </div>
          )}

          {visibleLeads.map((lead) => (
            <div key={lead.id} style={S.card}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                <strong style={{ fontSize: 15 }}>{lead.address}</strong>
                <span style={S.badge(lead.status)}>{STATUS_LABELS[lead.status]}</span>
              </div>
              <div style={{ ...S.small, marginTop: 4 }}>
                {lead.distressSigns
                  .map((k) => DISTRESS_SIGNS.find(([key]) => key === k)?.[1] ?? k)
                  .join(" · ") || "No distress signs logged"}
              </div>
              {lead.notes && <div style={{ fontSize: 13, marginTop: 6 }}>{lead.notes}</div>}
              <div style={{ ...S.small, marginTop: 6 }}>
                MAO {money(mao(lead))}
                {lead.auctionDate ? ` · Auction ${lead.auctionDate}` : ""}
                {lead.lat != null ? ` · 📍 ${lead.lat.toFixed(4)}, ${lead.lng.toFixed(4)}` : ""}
              </div>
              <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                <select
                  style={{ ...S.select, flex: 1 }}
                  value={lead.status}
                  onChange={(e) => setStatus(lead.id, e.target.value)}
                >
                  {STATUSES.map((s) => (
                    <option key={s} value={s}>
                      {STATUS_LABELS[s]}
                    </option>
                  ))}
                </select>
                <button
                  style={{ ...S.signBtn(false), color: "#b91c1c", borderColor: "#fca5a5" }}
                  onClick={() => {
                    if (window.confirm(`Delete lead for ${lead.address}?`)) removeLead(lead.id);
                  }}
                >
                  Delete
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {tab === "calc" && (
        <div style={S.card}>
          {[
            ["arv", "After-repair value (ARV) $"],
            ["repairs", "Repair estimate $"],
            ["fee", "Your wholesale fee $"],
            ["percent", "Percent (0.70 = 70% rule)"],
          ].map(([key, label]) => (
            <div key={key}>
              <label style={S.label}>{label}</label>
              <input
                style={S.input}
                type="number"
                inputMode="decimal"
                step={key === "percent" ? "0.01" : "1000"}
                value={calc[key]}
                onChange={(e) => setCalc({ ...calc, [key]: e.target.value })}
              />
            </div>
          ))}
          <div style={S.maoBox}>
            <div style={{ fontSize: 11, opacity: 0.7 }}>Maximum allowable offer</div>
            <div style={{ fontSize: 26, fontWeight: 800 }}>{money(calcResult)}</div>
            <div style={{ fontSize: 11, opacity: 0.7, marginTop: 4 }}>
              ARV × percent − repairs − fee
            </div>
          </div>
        </div>
      )}

      {tab === "export" && (
        <div style={S.card}>
          <p style={{ ...S.small, marginTop: 0 }}>
            {leads.length} lead{leads.length === 1 ? "" : "s"}. Copy this JSON and save it as{" "}
            <code>leads.json</code>, then run <code>pennyrun-monitor</code> on it. Exports contain
            addresses and GPS of real properties — keep them out of git and off the internet.
          </p>
          <button style={{ ...S.primary, marginTop: 0 }} onClick={copyExport}>
            Copy export JSON
          </button>
          <textarea
            readOnly
            style={{ ...S.input, minHeight: 180, marginTop: 10, fontFamily: "monospace", fontSize: 11 }}
            value={exportJson}
            onFocus={(e) => e.target.select()}
          />
        </div>
      )}
    </div>
  );
}

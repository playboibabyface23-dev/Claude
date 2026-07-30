// PennyRun field app — the in-store half of PennyRun. React, no build step:
// a single self-contained component (inline styles, no imports beyond React)
// that renders as a Claude artifact or in any React host. See ../CLAUDE.md.
//
// Board tab  — paste monitor/data/board-export.json, hunt the list in-store
// Decoder tab — type a shelf price, get its ladder stage and what to do
// Log tab    — record what the register actually said

import React, { useEffect, useMemo, useState } from "react";

const STORAGE_KEY = "pennyrun.field.v1";

// Keep in sync with monitor/src/ladder.js — same ladder, same words.
const STAGE_INFO = {
  full: {
    label: "Full price",
    color: "#6b7280",
    advice: "Not on the ladder. Walk on.",
  },
  first: {
    label: "First markdown (.06)",
    color: "#2563eb",
    advice: "Clock started. Note it, come back — don't buy yet.",
  },
  final: {
    label: "Final markdown (.03)",
    color: "#d97706",
    advice: "Last stop before a penny — historically ~3 weeks out. Watch it.",
  },
  penny: {
    label: "PENNY — $0.01",
    color: "#16a34a",
    advice: "Scan it at the register to confirm, buy it, be cool about it.",
  },
};

function stageFor(price) {
  const cents = Math.round(price * 100);
  if (!Number.isFinite(cents) || cents < 0) return null;
  if (cents === 1) return "penny";
  const ending = cents % 100;
  if (ending === 6) return "first";
  if (ending === 3) return "final";
  return "full";
}

const HUNT_STATES = ["hunting", "found", "gone"];
const HUNT_COLORS = { hunting: "#2563eb", found: "#16a34a", gone: "#6b7280" };

function loadSaved() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const data = raw ? JSON.parse(raw) : {};
    return {
      board: data.board ?? null,
      hunt: data.hunt ?? {},
      finds: Array.isArray(data.finds) ? data.finds : [],
    };
  } catch {
    return { board: null, hunt: {}, finds: [] };
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
  badge: (color) => ({
    display: "inline-block",
    padding: "2px 8px",
    borderRadius: 999,
    fontSize: 11,
    fontWeight: 700,
    color: "#fff",
    background: color,
  }),
  small: { fontSize: 12, color: "#6b7280" },
  primary: {
    width: "100%",
    padding: "12px",
    marginTop: 12,
    borderRadius: 8,
    border: "none",
    background: "#b45309",
    color: "#fff",
    fontSize: 15,
    fontWeight: 700,
    cursor: "pointer",
  },
  pill: (on, color) => ({
    padding: "6px 10px",
    borderRadius: 999,
    border: "1px solid " + (on ? color : "#d1d5db"),
    background: on ? color : "#fff",
    color: on ? "#fff" : "#374151",
    fontSize: 12,
    fontWeight: 600,
    cursor: "pointer",
  }),
  stageBox: (color) => ({
    marginTop: 12,
    padding: 14,
    borderRadius: 8,
    background: color,
    color: "#fff",
    textAlign: "center",
  }),
};

export default function PennyRun() {
  const saved = useMemo(loadSaved, []);
  const [tab, setTab] = useState("board");
  const [board, setBoard] = useState(saved.board);
  const [hunt, setHunt] = useState(saved.hunt); // "sku@store" → hunting|found|gone
  const [finds, setFinds] = useState(saved.finds);
  const [paste, setPaste] = useState("");
  const [flash, setFlash] = useState("");
  const [decodePrice, setDecodePrice] = useState("");
  const [findForm, setFindForm] = useState({ item: "", store: "", shelf: "", register: "", qty: "1" });

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ board, hunt, finds }));
    } catch {
      // storage unavailable — session still works in memory
    }
  }, [board, hunt, finds]);

  useEffect(() => {
    if (!flash) return;
    const t = setTimeout(() => setFlash(""), 2500);
    return () => clearTimeout(t);
  }, [flash]);

  function importBoard() {
    try {
      const data = JSON.parse(paste);
      if (!Array.isArray(data.items)) throw new Error("no items array");
      setBoard(data);
      setHunt({});
      setPaste("");
      setFlash(`Board loaded: ${data.items.length} item(s)`);
    } catch (err) {
      setFlash(`Could not read that JSON (${err.message})`);
    }
  }

  function setHuntState(key, value) {
    setHunt((h) => ({ ...h, [key]: value }));
  }

  function saveFind() {
    if (!findForm.item.trim()) {
      setFlash("Name the item");
      return;
    }
    const num = (v) => (v === "" ? null : Number.parseFloat(v));
    setFinds((prev) => [
      {
        id: `find_${Date.now()}`,
        item: findForm.item.trim(),
        store: findForm.store.trim() || null,
        shelfPrice: num(findForm.shelf),
        registerPrice: num(findForm.register),
        qty: Number.parseInt(findForm.qty, 10) || 1,
        ts: new Date().toISOString(),
      },
      ...prev,
    ]);
    setFindForm({ item: "", store: findForm.store, shelf: "", register: "", qty: "1" });
    setFlash("Logged");
  }

  const stores = useMemo(() => {
    if (!board) return [];
    const byStore = new Map();
    for (const item of board.items) {
      if (!byStore.has(item.store)) byStore.set(item.store, []);
      byStore.get(item.store).push(item);
    }
    for (const list of byStore.values()) list.sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
    return [...byStore.entries()];
  }, [board]);

  const decoded = decodePrice === "" ? null : stageFor(Number.parseFloat(decodePrice));
  const findsJson = useMemo(
    () => JSON.stringify({ version: 1, exportedAt: new Date().toISOString(), finds }, null, 2),
    [finds],
  );

  return (
    <div style={S.app}>
      <header style={S.header}>
        <h1 style={S.h1}>🪙 PennyRun</h1>
        <span style={S.tagline}>ride the ladder down to a penny</span>
      </header>

      <nav style={S.tabs}>
        {[
          ["board", board ? `Board (${board.items.length})` : "Board"],
          ["decoder", "Decoder"],
          ["log", `Log (${finds.length})`],
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

      {tab === "board" && (
        <div>
          <div style={S.card}>
            <label style={S.label}>
              Paste <code>board-export.json</code> from the monitor (`node index.js export`)
            </label>
            <textarea
              style={{ ...S.input, minHeight: 70, fontFamily: "monospace", fontSize: 11 }}
              placeholder='{"version":1,"items":[...]}'
              value={paste}
              onChange={(e) => setPaste(e.target.value)}
            />
            <button style={{ ...S.primary, marginTop: 8 }} onClick={importBoard}>
              Load board
            </button>
            {board && (
              <div style={{ ...S.small, marginTop: 8 }}>
                Exported {board.exportedAt?.slice(0, 16).replace("T", " ")} · threshold {board.threshold}
              </div>
            )}
          </div>

          {!board && (
            <div style={{ ...S.card, textAlign: "center", color: "#6b7280" }}>
              No board yet. Run the monitor, export, paste it here, go hunt.
            </div>
          )}

          {stores.map(([store, items]) => (
            <div key={store}>
              <div style={{ ...S.small, fontWeight: 700, margin: "12px 0 6px" }}>
                STORE {store} — {items.length} item(s)
              </div>
              {items.map((item) => {
                const key = `${item.sku}@${item.store}`;
                const state = hunt[key] ?? "hunting";
                const info = STAGE_INFO[item.stage] ?? STAGE_INFO.full;
                return (
                  <div key={key} style={{ ...S.card, opacity: state === "gone" ? 0.55 : 1 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                      <strong style={{ fontSize: 15 }}>{item.label}</strong>
                      <span style={S.badge(info.color)}>score {item.score}</span>
                    </div>
                    <div style={{ ...S.small, marginTop: 4 }}>
                      SKU {item.sku} · last ${item.price} · inv {item.inventory ?? "?"} ·{" "}
                      {info.label}
                      {item.ripeDate ? ` · ripe ${item.ripeDate}` : ""}
                    </div>
                    <div style={{ display: "flex", gap: 6, marginTop: 10 }}>
                      {HUNT_STATES.map((hs) => (
                        <button
                          key={hs}
                          style={S.pill(state === hs, HUNT_COLORS[hs])}
                          onClick={() => setHuntState(key, hs)}
                        >
                          {hs}
                        </button>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      )}

      {tab === "decoder" && (
        <div style={S.card}>
          <label style={S.label}>Shelf price</label>
          <input
            style={{ ...S.input, fontSize: 22, textAlign: "center" }}
            type="number"
            inputMode="decimal"
            step="0.01"
            placeholder="9.03"
            value={decodePrice}
            onChange={(e) => setDecodePrice(e.target.value)}
          />
          {decoded && (
            <div style={S.stageBox(STAGE_INFO[decoded].color)}>
              <div style={{ fontSize: 20, fontWeight: 800 }}>{STAGE_INFO[decoded].label}</div>
              <div style={{ fontSize: 13, marginTop: 6, opacity: 0.9 }}>
                {STAGE_INFO[decoded].advice}
              </div>
            </div>
          )}
          <div style={{ ...S.small, marginTop: 14, lineHeight: 1.7 }}>
            <strong>The ladder:</strong>
            <br />
            .06 ending — first markdown, the clock starts
            <br />
            .03 ending — final markdown; pennies out ~3 weeks later
            <br />
            $0.01 — penny. The shelf tag lies; the register doesn't. Scan to confirm.
          </div>
        </div>
      )}

      {tab === "log" && (
        <div>
          <div style={S.card}>
            <label style={S.label}>Item</label>
            <input
              style={S.input}
              placeholder="LED shop light"
              value={findForm.item}
              onChange={(e) => setFindForm({ ...findForm, item: e.target.value })}
            />
            <div style={{ display: "flex", gap: 8 }}>
              <div style={{ flex: 1 }}>
                <label style={S.label}>Store</label>
                <input
                  style={S.input}
                  placeholder="0121"
                  value={findForm.store}
                  onChange={(e) => setFindForm({ ...findForm, store: e.target.value })}
                />
              </div>
              <div style={{ flex: 1 }}>
                <label style={S.label}>Qty</label>
                <input
                  style={S.input}
                  type="number"
                  inputMode="numeric"
                  value={findForm.qty}
                  onChange={(e) => setFindForm({ ...findForm, qty: e.target.value })}
                />
              </div>
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <div style={{ flex: 1 }}>
                <label style={S.label}>Shelf price $</label>
                <input
                  style={S.input}
                  type="number"
                  inputMode="decimal"
                  step="0.01"
                  value={findForm.shelf}
                  onChange={(e) => setFindForm({ ...findForm, shelf: e.target.value })}
                />
              </div>
              <div style={{ flex: 1 }}>
                <label style={S.label}>Register said $</label>
                <input
                  style={S.input}
                  type="number"
                  inputMode="decimal"
                  step="0.01"
                  value={findForm.register}
                  onChange={(e) => setFindForm({ ...findForm, register: e.target.value })}
                />
              </div>
            </div>
            <button style={S.primary} onClick={saveFind}>
              Log it
            </button>
          </div>

          {finds.map((f) => {
            const pennied = f.registerPrice !== null && Math.round(f.registerPrice * 100) === 1;
            return (
              <div key={f.id} style={S.card}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                  <strong>{f.item}</strong>
                  {pennied && <span style={S.badge("#16a34a")}>PENNY ✕{f.qty}</span>}
                </div>
                <div style={{ ...S.small, marginTop: 4 }}>
                  {f.store ? `store ${f.store} · ` : ""}shelf ${f.shelfPrice ?? "?"} · register $
                  {f.registerPrice ?? "?"} · {f.ts.slice(0, 16).replace("T", " ")}
                </div>
              </div>
            );
          })}

          {finds.length > 0 && (
            <div style={S.card}>
              <label style={S.label}>Finds export (copy for your records)</label>
              <textarea
                readOnly
                style={{ ...S.input, minHeight: 120, fontFamily: "monospace", fontSize: 11 }}
                value={findsJson}
                onFocus={(e) => e.target.select()}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

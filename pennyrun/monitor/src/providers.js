// Price/inventory providers. Every provider implements the same interface:
//
//   { name, lookup({ sku, store }) → Promise<Reading> }
//
//   Reading = { sku, store, price, inventory, ts, source }
//     price     — register-facing price in dollars, or null if unknown
//     inventory — units on hand at that store, or null if unknown
//
// serpapi additionally implements resolve(query) → internal product id.
//
// Field mappings for the live providers are best-effort against their
// documented response shapes; verify against the provider's docs (and a raw
// response) before trusting live numbers. Everything else in the monitor is
// provider-agnostic — fix mappings here and only here.

function requireEnv(name) {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is not set — copy .env.example and export your key, or use PENNYRUN_PROVIDER=mock`);
  }
  return value;
}

async function getJson(url) {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`${url.hostname} responded ${res.status} ${res.statusText}`);
  }
  return res.json();
}

function firstFinite(...values) {
  for (const v of values) {
    const n = typeof v === "string" ? Number.parseFloat(v.replace(/[$,]/g, "")) : v;
    if (Number.isFinite(n)) return n;
  }
  return null;
}

// --- serpapi (https://serpapi.com, Home Depot engines) -----------------------

const serpapi = {
  name: "serpapi",

  async lookup({ sku, store }) {
    const key = requireEnv("SERPAPI_API_KEY");
    const url = new URL("https://serpapi.com/search.json");
    url.searchParams.set("engine", "home_depot_product");
    url.searchParams.set("product_id", sku);
    if (store) url.searchParams.set("store_id", store);
    url.searchParams.set("api_key", key);

    const data = await getJson(url);
    const p = data.product_results ?? data.product ?? {};
    return {
      sku,
      store,
      price: firstFinite(p.price, p.pricing?.price, p.pricing?.current_price),
      inventory: firstFinite(
        p.fulfillment?.pickup?.quantity,
        p.fulfillment?.store?.quantity,
        p.quantity,
      ),
      ts: new Date().toISOString(),
      source: "serpapi",
    };
  },

  // SKU or UPC → Home Depot internal product id (what lookup() wants).
  async resolve(query) {
    const key = requireEnv("SERPAPI_API_KEY");
    const url = new URL("https://serpapi.com/search.json");
    url.searchParams.set("engine", "home_depot");
    url.searchParams.set("q", query);
    url.searchParams.set("api_key", key);

    const data = await getJson(url);
    const hit = (data.products ?? data.organic_results ?? [])[0];
    if (!hit) throw new Error(`No product found for "${query}"`);
    return {
      productId: String(hit.product_id ?? hit.id ?? ""),
      title: hit.title ?? "(no title)",
    };
  },
};

// --- unwrangle (https://unwrangle.com, Home Depot detail API) ----------------

const unwrangle = {
  name: "unwrangle",

  async lookup({ sku, store }) {
    const key = requireEnv("UNWRANGLE_API_KEY");
    const url = new URL("https://data.unwrangle.com/api/getter/");
    url.searchParams.set("platform", "homedepot_detail");
    url.searchParams.set("item_id", sku);
    if (store) url.searchParams.set("store", store);
    url.searchParams.set("api_key", key);

    const data = await getJson(url);
    const d = data.detail ?? data.result ?? {};
    return {
      sku,
      store,
      price: firstFinite(d.price, d.price_reduced, d.current_price),
      inventory: firstFinite(d.inventory, d.in_stock_qty, d.quantity),
      ts: new Date().toISOString(),
      source: "unwrangle",
    };
  },
};

// --- mock --------------------------------------------------------------------

// Scripted mock: pass { "SKU@STORE": [{ price, inventory }, ...] } and each
// lookup() shifts the next reading (the last one repeats forever). This is
// what test-ladder.js drives.
export function createMockProvider(scripts = {}) {
  const cursors = new Map();
  return {
    name: "mock",
    async lookup({ sku, store }) {
      const keyName = `${sku}@${store}`;
      const script = scripts[keyName];
      let step;
      if (script && script.length > 0) {
        const i = cursors.get(keyName) ?? 0;
        step = script[Math.min(i, script.length - 1)];
        cursors.set(keyName, i + 1);
      } else {
        step = defaultWalkStep(keyName, cursors);
      }
      // No ts on purpose: sweep() stamps its own clock, which is what lets
      // test-ladder.js replay weeks of readings with an injected `now`.
      return { sku, store, ...step, source: "mock" };
    },
  };
}

// With no script, walk every SKU down the ladder one rung per call so
// `node index.js once` demos alerts offline with no keys.
function defaultWalkStep(keyName, cursors) {
  const walk = [
    { price: 19.98, inventory: 12 },
    { price: 12.06, inventory: 9 },
    { price: 9.03, inventory: 6 },
    { price: 9.03, inventory: 3 },
    { price: 0.01, inventory: 2 },
  ];
  const i = cursors.get(keyName) ?? 0;
  cursors.set(keyName, i + 1);
  return walk[Math.min(i, walk.length - 1)];
}

// -----------------------------------------------------------------------------

const PROVIDERS = { serpapi, unwrangle, mock: createMockProvider() };

export function getProvider(name) {
  const provider = PROVIDERS[name];
  if (!provider) {
    throw new Error(`Unknown provider "${name}" — expected serpapi, unwrangle, or mock`);
  }
  return provider;
}

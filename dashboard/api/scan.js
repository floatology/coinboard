// Scans the owner's wallets on Arc and Robinhood Chain and updates holdings, buy and sell averages.
// Stateless: it starts from the baseline (holdings as of a block, written at build time in _config.json) and replays every
// ERC-20 transfer touching the wallets since then, then checks the result against on-chain balances.
const TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";
const pad = a => "0x" + "0".repeat(24) + a.toLowerCase().slice(2);
const lc = s => (s || "").toLowerCase();

async function rpc(url, method, params, tries = 4) {
  let err;
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
      const j = await r.json();
      if (j.error) throw new Error(JSON.stringify(j.error).slice(0, 200));
      return j.result;
    } catch (e) { err = e; await new Promise(r => setTimeout(r, 400 * (i + 1))); }
  }
  throw err;
}
async function pool(items, n, fn) {
  const out = new Array(items.length); let i = 0;
  await Promise.all(Array.from({ length: n }, async () => { while (i < items.length) { const k = i++; out[k] = await fn(items[k], k); } }));
  return out;
}
async function logs(url, flt, a, b, maxSpan) {
  // split the range; halve when the node refuses
  const spans = [];
  for (let s = a; s <= b; s += maxSpan) spans.push([s, Math.min(s + maxSpan - 1, b)]);
  const one = async ([x, y]) => {
    try { return await rpc(url, "eth_getLogs", [{ ...flt, fromBlock: "0x" + x.toString(16), toBlock: "0x" + y.toString(16) }], 2); }
    catch (e) { if (y - x < 50) throw e; const m = Math.floor((x + y) / 2); return [...(await one([x, m])), ...(await one([m + 1, y]))]; }
  };
  return (await pool(spans, 8, one)).flat();
}
const hex = x => parseInt(x, 16);
async function dec(url, token) { try { return hex(await rpc(url, "eth_call", [{ to: token, data: "0x313ce567" }, "latest"], 2)); } catch { return 18; } }
async function sym(url, token) {
  try {
    const h = (await rpc(url, "eth_call", [{ to: token, data: "0x95d89b41" }, "latest"], 2)).slice(2);
    const off = parseInt(h.slice(0, 64), 16) * 2, len = parseInt(h.slice(off, off + 64), 16);
    return Buffer.from(h.slice(off + 64, off + 64 + len * 2), "hex").toString("utf8") || "?";
  } catch { return "?"; }
}
async function balanceOf(url, token, w) { try { return BigInt(await rpc(url, "eth_call", [{ to: token, data: "0x70a08231" + pad(w).slice(2) }, "latest"], 2)); } catch { return null; } }

async function scanChain(key, ch, notes) {
  const url = ch.rpc, wallets = ch.wallets.map(lc), wset = new Set(wallets);
  const stables = {}; for (const s of ch.stables) stables[lc(s.address)] = s;
  const head = hex(await rpc(url, "eth_blockNumber", []));
  const from = ch.baseBlock + 1;
  const hold = {};   // token -> {qty, cost, soldQty, soldProceeds, realised}
  for (const h of ch.baseline) hold[lc(h.token)] = { name: h.name, chain: key, token: lc(h.token), qty: h.qty, cost: h.cost, soldQty: h.soldQty || 0, soldProceeds: h.soldProceeds || 0, realised: h.realised || 0, dec: h.dec || 18, symbol: h.name };
  let txs = [];
  if (head >= from) {
    const flt = [];
    for (const w of wallets) { flt.push({ topics: [TRANSFER, null, pad(w)] }); flt.push({ topics: [TRANSFER, pad(w)] }); }
    const got = (await Promise.all(flt.map(f => logs(url, f, from, head, ch.maxSpan)))).flat();
    const hashes = [...new Set(got.map(l => l.transactionHash))];
    const receipts = await pool(hashes, 10, h => rpc(url, "eth_getTransactionReceipt", [h]));
    // ETH price for WETH-valued flows
    let ethPx = 0;
    if (ch.weth) { try { const r = await fetch(`https://api.dexscreener.com/latest/dex/tokens/${ch.weth}`); const j = await r.json(); const p = (j.pairs || []).filter(p => p.liquidity).sort((a, b) => b.liquidity.usd - a.liquidity.usd)[0]; ethPx = parseFloat(p.priceUsd) || 0; } catch { ethPx = ch.ethUsd || 0; } }
    const decs = {};
    const getDec = async t => (decs[t] ??= await dec(url, t));
    receipts.sort((a, b) => hex(a.blockNumber) - hex(b.blockNumber) || hex(a.transactionIndex) - hex(b.transactionIndex));
    for (const rc of receipts) {
      if (!rc || rc.status === "0x0") continue;
      const delta = {}; let stableMax = 0, stableWallet = 0, wethMax = 0;
      for (const l of rc.logs) {
        if (l.topics[0] !== TRANSFER || l.topics.length < 3) continue;
        const t = lc(l.address), from_ = "0x" + l.topics[1].slice(26), to_ = "0x" + l.topics[2].slice(26), raw = BigInt(l.data);
        if (stables[t]) {
          const v = Number(raw) / 10 ** stables[t].decimals; stableMax = Math.max(stableMax, v);
          if (wset.has(from_)) stableWallet -= v; if (wset.has(to_)) stableWallet += v;
        } else if (t === lc(ch.weth)) {
          wethMax = Math.max(wethMax, Number(raw) / 1e18 * ethPx);
        } else {
          const d = await getDec(t);
          const v = Number(raw) / 10 ** d;
          if (wset.has(from_)) delta[t] = (delta[t] || 0) - v;
          if (wset.has(to_)) delta[t] = (delta[t] || 0) + v;
        }
      }
      const entries = Object.entries(delta).filter(([, v]) => Math.abs(v) > 1e-12);
      if (!entries.length) continue;
      const buys = entries.filter(([, v]) => v > 0), sells = entries.filter(([, v]) => v < 0);
      const value = stableWallet < -0.01 ? -stableWallet : stableWallet > 0.01 ? stableWallet : (stableMax || wethMax || 0);
      const note = (t, s) => notes.push(`${key}: ${s} ${t}`);
      let carried = 0;
      for (const [t, q] of sells) {
        const h = hold[t] ||= { name: t.slice(0, 8), chain: key, token: t, qty: 0, cost: 0, soldQty: 0, soldProceeds: 0, realised: 0, dec: decs[t] || 18 };
        const sold = Math.min(-q, h.qty > 0 ? h.qty : -q), basis = h.qty > 0 ? h.cost * (sold / h.qty) : 0;
        h.qty -= sold; h.cost -= basis; if (h.qty < 1e-9) { h.qty = 0; h.cost = 0; }
        if (buys.length) carried += basis;
        else { const proceeds = value * (sold / -sells.reduce((a, [, x]) => a + x, 0)); h.soldQty += sold; h.soldProceeds += proceeds; h.realised += proceeds - basis; }
      }
      for (const [t, q] of buys) {
        const h = hold[t] ||= { name: t.slice(0, 8), chain: key, token: t, qty: 0, cost: 0, soldQty: 0, soldProceeds: 0, realised: 0, dec: decs[t] || 18 };
        const share = q / buys.reduce((a, [, x]) => a + x, 0);
        const cost = sells.length ? carried * share : (stableWallet < -0.01 || stableMax || wethMax) ? value * share : 0;
        h.qty += q; h.cost += cost;
        if (!sells.length && !cost) note(t, "received free");
      }
    }
  }
  // verify against on-chain balances, fetch symbols
  const toks = Object.keys(hold);
  await pool(toks, 8, async t => {
    const h = hold[t]; h.dec = await dec(url, t); if (!h.symbol || h.symbol === h.token.slice(0, 8) || h.symbol === "?") h.symbol = await sym(url, t);
    let tot = 0n; for (const w of wallets) { const b = await balanceOf(url, t, w); if (b != null) tot += b; }
    h.onchain = Number(tot) / 10 ** h.dec;
    if (Math.abs(h.onchain - h.qty) > Math.max(1e-6, 0.002 * Math.max(h.qty, h.onchain))) {
      notes.push(`${key}: ${h.symbol} balance on chain ${h.onchain.toPrecision(8)} differs from the replay ${h.qty.toPrecision(8)}; using the chain`);
      if (h.qty > 0) h.cost *= h.onchain / h.qty; h.qty = h.onchain;
    }
  });
  return { head, hold: Object.values(hold) };
}

async function scanAll(cfg) {
  const notes = [], holdings = [], closed = [], heads = {};
  const keys = Object.keys(cfg.chains);
  const results = await Promise.all(keys.map(k => scanChain(k, cfg.chains[k], notes)));
  results.forEach((r, i) => {
    heads[keys[i]] = r.head;
    for (const h of r.hold) (h.qty * 1 > 1e-9 ? holdings : closed).push(h);
  });
  return { scannedAt: Math.floor(Date.now() / 1000), heads, holdings, closed, notes };
}

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  try { res.status(200).json(await scanAll(require("./_config.json"))); }
  catch (e) { res.status(500).json({ error: String(e && e.message || e) }); }
};
module.exports.scanAll = scanAll;

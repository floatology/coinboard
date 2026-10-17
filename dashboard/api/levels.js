// Price alerts set on the page (tap a price): the 5-minute check on GitHub reads them with the database's read-only
// token and sends a Telegram message when the price gets there. Stored in the same Upstash database as the watchlist.
const KEY = "coinboard:levels";
const clean = list => (Array.isArray(list) ? list : []).slice(0, 200).map(l => ({
  chain: String(l.chain || "").slice(0, 40), token: String(l.token || "").slice(0, 100), symbol: String(l.symbol || "").slice(0, 40),
  pair: String(l.pair || "").slice(0, 100), price: Number(l.price), dir: Number(l.dir) > 0 ? 1 : -1, at: Math.floor(Number(l.at)) || 0,
  ref: Number(l.ref) || 0 })).filter(l => l.chain && l.token && Number.isFinite(l.price) && l.price > 0);

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  const url = process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN;
  if (!url || !token) return res.status(501).json({ error: "No database connected to the Vercel project yet." });
  const cmd = async args => {
    const r = await fetch(url, { method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      body: JSON.stringify(args), signal: AbortSignal.timeout(8000) });
    const j = await r.json();
    if (j.error) throw new Error(j.error);
    return j.result;
  };
  try {
    if (req.method === "GET") {
      const v = await cmd(["GET", KEY]);
      return res.status(200).json({ levels: v ? clean(JSON.parse(v)) : null });
    }
    if (req.method === "PUT" || req.method === "POST") {
      const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
      if (!Array.isArray(body.levels)) return res.status(400).json({ error: "Send {levels: [...]}" });
      await cmd(["SET", KEY, JSON.stringify(clean(body.levels))]);
      return res.status(200).json({ ok: true });
    }
    res.status(405).json({ error: "GET or PUT" });
  } catch (e) { res.status(502).json({ error: String(e && e.message || e) }); }
};

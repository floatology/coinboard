// Watchlist shared across devices. Stored in the Upstash Redis database connected to this Vercel project
// (Project > Storage). Until one is connected this answers 501 and the page keeps the watchlist in the browser.
const KEY = "coinboard:watch";
const clean = list => (Array.isArray(list) ? list : []).slice(0, 100).map(c => ({
  chain: String(c.chain || "").slice(0, 40), token: String(c.token || "").slice(0, 100),
  symbol: String(c.symbol || "").slice(0, 40), pair: String(c.pair || "").slice(0, 100) })).filter(c => c.chain && c.token);

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
      return res.status(200).json({ watch: v ? clean(JSON.parse(v)) : null });
    }
    if (req.method === "PUT" || req.method === "POST") {
      const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
      if (!Array.isArray(body.watch)) return res.status(400).json({ error: "Send {watch: [...]}" });
      await cmd(["SET", KEY, JSON.stringify(clean(body.watch))]);
      return res.status(200).json({ ok: true });
    }
    res.status(405).json({ error: "GET or PUT" });
  } catch (e) { res.status(502).json({ error: String(e && e.message || e) }); }
};

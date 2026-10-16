// "Refresh analytics" button: asks GitHub to run the coinboard workflow now (it refreshes the archives and redeploys the page).
// Needs two environment variables on the Vercel project: GH_REPO (owner/name) and GH_TOKEN (fine-grained token, Actions: read and write
// on that one repository). The page sits behind Vercel login, so only you can reach this.
module.exports = async (req, res) => {
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });
  const repo = process.env.GH_REPO, token = process.env.GH_TOKEN, ref = process.env.GH_REF || "main";
  if (!repo || !token) return res.status(501).json({ error: "Refresh is not set up yet (GH_REPO / GH_TOKEN missing on the Vercel project)." });
  const r = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/coinboard.yml/dispatches`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, accept: "application/vnd.github+json", "user-agent": "coinboard", "x-github-api-version": "2022-11-28" },
    body: JSON.stringify({ ref }),
  });
  if (r.status === 204) return res.status(202).json({ ok: true });
  res.status(502).json({ error: `GitHub answered ${r.status}`, detail: (await r.text()).slice(0, 200) });
};

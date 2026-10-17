// Hourly build step: replay the wallets' transfers since the baseline in api/_config.json (same code as the Scan button),
// then move the baseline up to the current block so the page's Scan only has to cover the last hour.
//   node scripts/advance_baseline.js dashboard/site/api/_config.json   -> rewrites the file, prints the scan result as JSON
const fs = require("fs"), path = require("path");
const file = path.resolve(process.argv[2]);
const cfg = JSON.parse(fs.readFileSync(file, "utf8"));
const { scanAll } = require(path.resolve(__dirname, "../dashboard/api/scan.js"));
scanAll(cfg).then(r => {
  for (const [k, ch] of Object.entries(cfg.chains)) {
    ch.baseBlock = r.heads[k];
    ch.baseline = [...r.holdings, ...r.closed].filter(h => h.chain === k).map(h => ({
      name: h.symbol || h.name, token: h.token, qty: h.qty, cost: h.cost, dec: h.dec,
      soldQty: h.soldQty, soldProceeds: h.soldProceeds, realised: h.realised, paid: !!h.paid }));
  }
  fs.writeFileSync(file, JSON.stringify(cfg));
  process.stdout.write(JSON.stringify(r));
}).catch(e => { console.error("advance failed:", e && e.message || e); process.exit(1); });

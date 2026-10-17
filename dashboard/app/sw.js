// Coin Board installed as an app. Pages always come from the network (the board is rebuilt every hour and prices are
// live), so nothing is cached; with no connection the app shows a short note instead of the browser's error page.
const OFFLINE = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Coin Board</title><style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:linear-gradient(180deg,#26084d 0,#0d0221 340px);color:#f7eaff;font:16px/1.5 system-ui,sans-serif;text-align:center;padding:24px}
h1{font-size:22px;margin:0 0 8px}p{color:#bea6dd;margin:0 0 20px}button{font:600 15px system-ui;border:0;border-radius:10px;padding:11px 20px;
background:linear-gradient(90deg,#ffb300,#ff8a00 55%,#ff5a1f);color:#2b1000}</style></head><body><div>
<h1>You're offline</h1><p>Coin Board needs a connection. It will load as soon as you're back online.</p>
<button onclick="location.reload()">Try again</button></div></body></html>`;
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => {
  if (e.request.mode !== "navigate") return;            // data, prices and charts go straight to the network
  e.respondWith(fetch(e.request).catch(() => new Response(OFFLINE, {headers: {"content-type": "text/html; charset=utf-8"}})));
});

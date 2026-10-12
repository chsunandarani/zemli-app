// Zemli Field service worker: lets the installed app open without network.
// Only the app screen is cached. Scans and API calls always go to the server
// (scans taken offline are kept in the page's own upload queue).
const CACHE = "zemli-app-v4";
const SHELL = ["/app", "/field", "/static/manifest.webmanifest", "/static/icon-192.png", "/static/icon-512.png", "/static/logo-mark.png", "/static/logo-full-600.png", "/static/favicon.png", "/api/crops"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  const shell = url.pathname === "/app" || url.pathname === "/field" || url.pathname.startsWith("/static/") || url.pathname === "/api/crops";
  if (!shell) return;  // scans, uploads, dashboard: always live
  // network first so updates show up, cached copy when offline
  e.respondWith(fetch(e.request).then(r => {
    const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); return r;
  }).catch(() => caches.match(e.request)));
});

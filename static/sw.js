const CACHE_NAME = "mwh-v2.4.7";
const STATIC_ASSETS = [
  "/",
  "/static/manifest.json"
];

// Install — cache static assets
self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

// Activate — clean up old caches
self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch — network first for API calls, cache first for static
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  // Always network-first for wind data and API calls
  if (url.pathname.startsWith("/data") || 
      url.pathname.startsWith("/jumprun") ||
      url.pathname.startsWith("/lastload") ||
      url.pathname.startsWith("/plane") ||
      url.pathname.startsWith("/tails")) {
    e.respondWith(fetch(e.request).catch(() => new Response("{}", {headers:{"Content-Type":"application/json"}})));
    return;
  }
  // Network first for main page to always get latest
  e.respondWith(
    fetch(e.request)
      .then(r => {
        if (r.ok && e.request.method === "GET") {
          const clone = r.clone();
          caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
        }
        return r;
      })
      .catch(() => caches.match(e.request))
  );
});

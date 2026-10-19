const CACHE_NAME = "mwh-v4.6.10";
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

// Push — takeoff alerts sent by the server (payload: {title, body, tag, url})
self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "Mango Wind Hub", {
    body: d.body || "", tag: d.tag || undefined, data: { url: d.url || "/" },
    icon: "/static/icon-192.png", badge: "/static/icon-192.png"
  }));
});

// Tapping an alert focuses the open app, or opens it
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    for (const c of list) { if ("focus" in c) return c.focus(); }
    return clients.openWindow(url);
  }));
});

// Fetch — network first for API calls, cache first for static
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  // Always network-first for wind data and API calls
  if (url.pathname.startsWith("/data") || 
      url.pathname.startsWith("/lastload") ||
      url.pathname.startsWith("/plane") ||
      url.pathname.startsWith("/tails") ||
      url.pathname.startsWith("/auth") ||
      url.pathname.startsWith("/push")) {
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

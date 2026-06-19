const CACHE_NAME = "dailycheck-v4";
const APP_SHELL = [
  "/",
  "/static/style.css",
  "/static/manifest.webmanifest",
  "/static/offline.html",
  "/stocktake",
  "/inventory",
  "/restock",
  "/restock/session",
  "/outbound",
  "/outbound/session"
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(
        () => caches.match(req).then((r) => r || caches.match("/static/offline.html"))
      )
    );
    return;
  }

  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          return res;
        })
        .catch(() => caches.match("/static/offline.html"));
    })
  );
});

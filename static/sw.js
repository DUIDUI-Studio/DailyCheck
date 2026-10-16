const CACHE_NAME = "dailycheck-v6";
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

  // 导航请求：network-first，离线回落 app shell / offline.html。
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(
        () => caches.match(req).then((r) => r || caches.match("/static/offline.html"))
      )
    );
    return;
  }

  // 只有同源 /static/ 静态资源走 cache-first。
  //
  // 其余（HTML 片段、JSON、任何接口）一律 network-only：不 respondWith 即
  // 交给浏览器直连网络。这里曾经对所有非导航 GET 无差别 cache-first +
  // cache.put，一旦页面内 fetch 一个 HTML 页面（例如 /users?<筛选>），就会
  // 把该页写进 Cache API —— 后果有两个，都已在浏览器里复现：
  //   ① 陈旧：服务端数据变了，列表仍是旧的一份，且不报错；
  //   ② 越权：缓存副本按 URL 命中即可返回，**不经过服务端鉴权**，匿名/
  //      无权限会话也能拿到 200 的管理员页面（响应自带 Vary: Cookie 挡不住
  //      Cache API 的匹配）。
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || url.pathname.indexOf("/static/") !== 0) {
    return;
  }

  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => caches.match("/static/offline.html"));
    })
  );
});

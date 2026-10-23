// Минимальный service worker: офлайн-заглушка с телефонами помощи (они нужны даже без сети).
const C = "lambda-v1", OFFLINE = "/static/web/offline.html";
self.addEventListener("install", e => e.waitUntil(caches.open(C).then(c => c.addAll([OFFLINE, "/static/web/app.css"]))));
self.addEventListener("fetch", e => { if (e.request.mode === "navigate") e.respondWith(fetch(e.request).catch(() => caches.match(OFFLINE))); });

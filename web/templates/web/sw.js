// В кэше только общие файлы. Карточки и ответы пациентов не сохраняем.
const CACHE = 'lambda-shell-v3';
const OFFLINE = '/static/web/offline.html';
const ASSETS = [OFFLINE, '/static/web/app.css', '/static/web/icon-192.png', '/static/web/icon-512.png'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(key => key.startsWith('lambda-') && key !== CACHE).map(key => caches.delete(key))
  )));
});
self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(async () => {
      const page = await caches.match(OFFLINE);
      return new Response(page ? await page.text() : 'Нет сети. Данные не сохранены.', {
        status: 503, headers: {'Content-Type': 'text/html; charset=utf-8'},
      });
    }));
  } else if (request.method === 'GET' && url.origin === self.location.origin && ASSETS.includes(url.pathname)) {
    event.respondWith(fetch(request).catch(() => caches.match(request)));
  }
});

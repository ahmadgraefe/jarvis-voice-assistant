// Jarvis — Service Worker (Mobile-Companion)
// Bewusst kein Offline-Caching (Stale-Cache-Risiko ohne echten Nutzen fuer
// eine live-Assistenz-App) — nur was fuer Installierbarkeit + Push noetig ist.

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let data = { title: 'Jarvis', body: '' };
  try {
    data = event.data.json();
  } catch (e) {
    if (event.data) data.body = event.data.text();
  }
  event.waitUntil(
    self.registration.showNotification(data.title || 'Jarvis', {
      body: data.body || '',
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if (client.url.includes('/mobile') && 'focus' in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow('/mobile');
    })
  );
});

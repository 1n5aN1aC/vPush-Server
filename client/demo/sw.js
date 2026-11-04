// Minimal service worker for the demo page: show whatever the server sends.
// A real app can build the text from its own local data instead (see README).
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { body: event.data && event.data.text() }; }
  // Every push MUST show a notification (iOS revokes subscriptions that don't).
  event.waitUntil(self.registration.showNotification(data.title || "vPush demo", {
    body: data.body || JSON.stringify(data),
    tag: data.tag || "vpush-demo",
    data,
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    if (wins.length) return wins[0].focus();
    return self.clients.openWindow("./");
  })());
});

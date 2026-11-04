/* vPush client: drop-in, no dependencies. Copy into your app.
 *
 *   const push = VPush({ server: "https://example.com/vpush", app: "vcounter" });
 *   push.supported()            -> true/false (needs an installed app on iOS)
 *   await push.enable({time})   -> asks permission (call from a tap), subscribes
 *   await push.update({time})   -> change prefs / time zone
 *   await push.disable()
 *   push.mark("done", "2026-11-03")   -> queued; retried until it gets through
 *   push.sync()                 -> call on launch/foreground: re-checks the
 *                                  subscription and flushes queued marks
 *   push.state()                -> { supported, permission, enabled }
 *
 * The page needs a registered service worker; this script only uses it.
 * Showing the notification is the service worker's job (its "push" event).
 */
(function (global) {
  "use strict";

  function VPush(opts) {
    const server = String(opts.server).replace(/\/+$/, "");
    const base = `${server}/v1/apps/${encodeURIComponent(opts.app)}`;
    const KEY = opts.storageKey || `vpush:${opts.app}`;

    function load() {
      try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; }
    }
    function store(s) {
      try { localStorage.setItem(KEY, JSON.stringify(s)); } catch {}
    }

    async function call(path, body, token) {
      const headers = { "Content-Type": "application/json" };
      if (token) headers.Authorization = "Bearer " + token;
      const res = await fetch(base + path, {
        method: body === undefined ? "GET" : "POST",
        headers: body === undefined ? undefined : headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store",
      });
      let data = null;
      try { data = await res.json(); } catch {}
      if (!res.ok) {
        const err = new Error((data && data.error) || `HTTP ${res.status}`);
        err.status = res.status;
        throw err;
      }
      return data;
    }

    function supported() {
      return "serviceWorker" in navigator && "PushManager" in global && "Notification" in global;
    }

    function tz() {
      try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { return "UTC"; }
    }

    function b64ToBytes(b64) {
      const s = atob((b64 + "===".slice((b64.length + 3) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
      return Uint8Array.from(s, (c) => c.charCodeAt(0));
    }

    async function serverKey(s) {
      const { publicKey } = await call("/key");
      if (s.key && s.key !== publicKey) s.keyChanged = true;
      s.key = publicKey;
      return publicKey;
    }

    // Get (or make) a browser subscription for the server's current key.
    async function browserSubscription(s) {
      const reg = await navigator.serviceWorker.ready;
      const key = await serverKey(s);
      let sub = await reg.pushManager.getSubscription();
      if (sub && s.keyChanged) {
        await sub.unsubscribe().catch(() => {});
        sub = null;
      }
      delete s.keyChanged;
      if (!sub) {
        sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key) });
      }
      return sub.toJSON();
    }

    async function register(s, prefs) {
      const subscription = await browserSubscription(s);
      s.prefs = prefs !== undefined ? prefs : s.prefs || {};
      s.endpoint = subscription.endpoint;
      s.tz = tz();
      const body = { subscription, tz: s.tz, prefs: s.prefs };
      try {
        const r = await call("/subscribe", body, s.token);
        s.id = r.id;
        if (r.token) s.token = r.token;
      } catch (e) {
        if (e.status !== 401 || !s.token) throw e;
        // The server forgot us (e.g. it was reset): register fresh.
        delete s.token;
        const r = await call("/subscribe", body);
        s.id = r.id;
        s.token = r.token;
      }
      s.enabled = true;
      store(s);
    }

    async function enable(prefs) {
      if (!supported()) throw new Error("Push isn't supported here (on iPhone, add the app to the Home Screen first).");
      const perm = await Notification.requestPermission();
      if (perm !== "granted") throw new Error("Notifications weren't allowed.");
      await register(load(), prefs || {});
    }

    async function update(prefs) {
      const s = load();
      if (!s.enabled) return;
      await register(s, prefs);
    }

    async function disable() {
      const s = load();
      try { if (s.token) await call("/unsubscribe", {}, s.token); } catch {}
      try {
        const reg = await navigator.serviceWorker.ready;
        const sub = await reg.pushManager.getSubscription();
        if (sub) await sub.unsubscribe();
      } catch {}
      store({ key: s.key });
    }

    async function flushMarks(s) {
      const pending = s.pendingMarks || [];
      const left = [];
      for (const m of pending) {
        try {
          await call("/mark", m, s.token);
        } catch (e) {
          // 400 = too old/invalid, will never succeed; drop it. Otherwise retry later.
          if (e.status !== 400) left.push(m);
        }
      }
      s.pendingMarks = left;
      store(s);
    }

    function mark(name, date) {
      const s = load();
      if (!s.enabled || !s.token) return Promise.resolve();
      s.pendingMarks = (s.pendingMarks || []).filter((m) => !(m.name === name && m.date === date));
      s.pendingMarks.push({ name, date });
      store(s);
      return flushMarks(s).catch(() => {});
    }

    let syncing = null;
    function sync() {
      if (syncing) return syncing;
      syncing = (async () => {
        const s = load();
        if (!s.enabled || !supported() || Notification.permission !== "granted") return;
        // Re-register if the subscription rotated, the key changed, or the time zone moved.
        const reg = await navigator.serviceWorker.ready;
        const sub = await reg.pushManager.getSubscription();
        const key = await serverKey(s);
        if (!sub || sub.endpoint !== s.endpoint || s.keyChanged || s.tz !== tz() || !s.token || key !== s.key) {
          await register(s);
        }
        await flushMarks(load());
      })().catch(() => {}).finally(() => { syncing = null; });
      return syncing;
    }

    function state() {
      const s = load();
      return {
        supported: supported(),
        permission: "Notification" in global ? Notification.permission : "unsupported",
        enabled: !!s.enabled,
        prefs: s.prefs || {},
      };
    }

    return { supported, enable, update, disable, mark, sync, state };
  }

  global.VPush = VPush;
})(typeof self !== "undefined" ? self : this);

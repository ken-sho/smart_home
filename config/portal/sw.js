// ════════════════════════════════════════════════════════════
//  Service Worker портала — браузерные уведомления о событиях
//  ------------------------------------------------------------
//  Назначение: показывать системные уведомления, когда событие
//  наступило, и снимать их по клику (ack на сервере + фокус окна).
//
//  Два поллера работают вместе и НЕ дублируют друг друга
//  (tag = 'evt:<id>' → повторный показ заменяет, а не плодит):
//    • страница (portal.html) — основной, надёжный пока открыта
//      панель/вкладка; знает базу API и Telegram initData.
//    • этот SW — best-effort фоновый опрос, пока воркер жив
//      (браузер усыпляет idle-SW ~через 30 c — поэтому именно
//      best-effort, а не гарантия фоновой доставки).
//
//  Базу API берём из scope воркера — работает и на прямом :7000/,
//  и под префиксом nginx (/portal/).
// ════════════════════════════════════════════════════════════

const POLL_INTERVAL = 60000; // раз в минуту

// Текст уведомления ОБЕЗЛИЧЕН — без имени события (приватность на
// залоченном/общем экране). Конкретику пользователь видит, открыв портал.
const NOTIFY_TITLE = 'Портал';
const NOTIFY_BODY = 'У вас есть пропущенное событие на портале';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

function apiBase() {
  // scope: https://host/  или  https://host/portal/
  return self.registration.scope.replace(/\/+$/, '') + '/api';
}

async function poll() {
  let events = [];
  try {
    const resp = await fetch(apiBase() + '/events/unacked', { credentials: 'same-origin' });
    if (!resp.ok) return;            // 401 на публичном Telegram-доступе — молчим
    const data = await resp.json();
    events = (data && data.events) || [];
  } catch (e) {
    return;                          // сеть/БД недоступны — тихо ждём следующего тика
  }
  // Сообщаем странице актуальное число неподтверждённых — чтобы бейдж
  // вкладки/иконки обновлялся в фоне, даже пока портал не открывали.
  // Шлём всегда (в т.ч. 0), чтобы бейдж снимался после ack.
  try {
    const cs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    cs.forEach((c) => c.postMessage({ type: 'unacked_count', count: events.length }));
  } catch (e) { /* нет клиентов — не страшно */ }
  for (const ev of events) {
    try {
      await self.registration.showNotification(NOTIFY_TITLE, {
        body: NOTIFY_BODY,
        tag: 'evt:' + ev.id,         // стабильный тег: одно уведомление на событие
        renotify: true,              // но КАЖДЫЙ опрос пере-оповещает (звук/вибро),
                                     // пока не подтвердят — без стопки копий
        requireInteraction: true,    // висит, пока не кликнут (Chromium/Vivaldi)
        data: { event_id: ev.id, ack_url: apiBase() + '/events/' + ev.id + '/ack' },
      });
    } catch (e) { /* showNotification может упасть — пропускаем */ }
  }
}

// Фоновый best-effort опрос (пока SW не усыплён).
setInterval(poll, POLL_INTERVAL);

// Страница может попросить опросить немедленно (например, при фокусе).
self.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'poll') poll();
});

// Клик по уведомлению: подтверждаем на сервере + фокусируем/открываем портал.
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const data = e.notification.data || {};
  e.waitUntil((async () => {
    try {
      if (data.ack_url) {
        await fetch(data.ack_url, { method: 'POST', credentials: 'same-origin' });
      }
    } catch (_) { /* ack не прошёл — повторно подтвердится при следующем клике */ }
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) {
      if ('focus' in c) { try { await c.focus(); } catch (_) {} return; }
    }
    try { await self.clients.openWindow(self.registration.scope); } catch (_) {}
  })());
});

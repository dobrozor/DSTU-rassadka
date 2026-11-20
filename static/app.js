/* Часть 3. Фронтенд: работа со схемой (SVG), заказами и авто-подбором.
   Ваш исходный код зума/панорамы сохранён (setVB / zoomAt / wheel / drag);
   поверх него добавлены: выделение областью, статусы и цвета мест, зум к заказу,
   пульсация, авто-подбор, заказы, журнал. */
(() => {
'use strict';
const $ = s => document.querySelector(s);
const svg = $('main > svg'), tip = $('#tip'), NS = svg.namespaceURI;
let W = 1266, H = 966;               // размеры схемы — берутся из viewBox выбранного зала (см. ensureHall)
let vb = [0, 0, W, H];

// ───────────────────────── утилиты ─────────────────────────
const K = (z, r, s) => `${z}|${r}|${s}`;
const esc = t => String(t ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = n => Number(n || 0).toLocaleString('ru-RU');
const debounce = (f, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => f(...a), ms); }; };
const plural = (n, f) => { n = Math.abs(n); const a = n % 10, b = n % 100; return a === 1 && b !== 11 ? f[0] : a >= 2 && a <= 4 && (b < 12 || b > 14) ? f[1] : f[2]; };
const seatsW = n => `${n} ${plural(n, ['место', 'места', 'мест'])}`;
function mix(a, b, t) {   // смешивание #rrggbb: бронь — «бледная» версия цвета заказа
  const p = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)), A = p(a), B = p(b);
  return '#' + A.map((v, i) => Math.round(v * (1 - t) + B[i] * t).toString(16).padStart(2, '0')).join('');
}
function hslHex(h, s, l) {
  s /= 100; l /= 100; const k = n => (n + h / 30) % 12, a = s * Math.min(l, 1 - l);
  const f = n => Math.round(255 * (l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)))));
  return '#' + [f(0), f(8), f(4)].map(v => v.toString(16).padStart(2, '0')).join('');
}
let toastT;
function toast(msg, err) {
  const t = $('#toast'); t.textContent = msg; t.className = 'show' + (err ? ' err' : '');
  clearTimeout(toastT); toastT = setTimeout(() => t.className = '', err ? 5000 : 2500);
}

// ───────────────────────── API ─────────────────────────
let opName = localStorage.getItem('op') || '';
async function api(method, url, data) {
  const r = await fetch(url, {
    method, body: data === undefined ? undefined : JSON.stringify(data),
    headers: { 'Content-Type': 'application/json', 'X-Operator': encodeURIComponent(opName) }
  });
  if (r.status === 204) return null;
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(j.error || 'Ошибка ' + r.status); e.status = r.status; e.data = j; throw e; }
  return j;
}
// обёртка для обработчиков: показывает ошибку, при конфликте мест обновляет схему
const safe = f => async (...a) => {
  try { await f(...a); } catch (e) {
    let m = e.message;
    if (e.data && e.data.conflicts) m += ': ' + e.data.conflicts.slice(0, 3).map(c => `${c.zone} ${c.row ? 'р.' + c.row + ' ' : ''}м.${c.seat} (${c.order})`).join(', ') + (e.data.conflicts.length > 3 ? '…' : '');
    toast(m, true);
    if (e.status === 409) loadState(true).catch(() => { });
  }
};

// ───────────────────────── места на схеме ─────────────────────────
// Схема зала не вшита в страницу: у каждого сеанса свой зал (реестр — halls.py на сервере).
const seats = new Map();             // key → {el, label, x, y, z, r, s}
let labels = null, marq = null;      // номера мест (видны при приближении) и рамка выделения
let ZONES = [];                      // зоны текущего зала
let HALLS = [], hall = null;         // реестр залов с сервера; hall — зал, чья схема сейчас на экране
const hallMeta = key => HALLS.find(h => h.key === key);
const zoneHasRows = z => !!(hall && (hall.zones.find(x => x.name === z) || {}).has_rows);
const seatObj = k => { const s = seats.get(k); return { zone: s.z, row: s.r, seat: s.s }; };
const seatName = k => { const s = seats.get(k); return s.z + (s.r ? ' · ряд ' + s.r : '') + ' · место ' + s.s; };

/** Ставит на экран схему зала key (если она там ещё не стоит): скачивает SVG, строит карту мест,
    подгоняет масштаб, зоны в «Найти место» и галочку «подбирать и в других зонах». */
async function ensureHall(key) {
  if (hall && hall.key === key) return;
  const meta = hallMeta(key);
  if (!meta) throw new Error(`Зал «${key}» не найден на сервере`);
  const r = await fetch(`/api/halls/${encodeURIComponent(key)}/svg`);
  if (!r.ok) throw new Error('Не удалось загрузить схему зала');
  const src = new DOMParser().parseFromString(await r.text(), 'image/svg+xml').documentElement;
  if (hall && hall.key === key) return;                 // пока грузили, этот же зал уже поставил другой вызов
  if (src.nodeName !== 'svg') throw new Error('Схема зала повреждена');

  W = meta.width; H = meta.height;
  svg.replaceChildren(...[...src.childNodes].map(n => document.importNode(n, true)));
  seats.clear(); selected.clear(); sug = null; clearPulse();
  labels = document.createElementNS(NS, 'g');
  svg.querySelectorAll('circle[data-z]').forEach(el => {
    if (!el.hasAttribute('data-r')) el.setAttribute('data-r', '');   // зона без рядов: нет атрибута = пустой ряд (как на сервере)
    const d = el.dataset, t = document.createElementNS(NS, 'text');
    t.setAttribute('x', d.x); t.setAttribute('y', +d.y + 2.3);
    t.setAttribute('class', 'sn' + (d.s.length > 2 ? ' s' : '')); t.textContent = d.s; labels.appendChild(t);
    seats.set(K(d.z, d.r, d.s), { el, label: t, x: +d.x, y: +d.y, z: d.z, r: d.r, s: d.s });
  });
  (svg.querySelector('g') || svg).appendChild(labels);
  marq = document.createElementNS(NS, 'rect'); marq.id = 'marq'; marq.style.display = 'none'; svg.appendChild(marq);
  hall = meta; ZONES = meta.zones.map(z => z.name);
  setVB([0, 0, W, H]);

  $('#fz').innerHTML = meta.zones.map(z => `<option>${esc(z.name)}</option>`).join('');
  $('#fr').value = $('#fs').value = ''; $('#fr').style.display = zoneHasRows($('#fz').value) ? '' : 'none';
  $('#apSideLbl').hidden = !meta.extra_zones_label; $('#apSideTxt').textContent = meta.extra_zones_label; $('#apSide').checked = false;
  $('#info').innerHTML = '<span>Наведите на место или нажмите на него</span>';
}

// ───────────────────────── состояние ─────────────────────────
let events = [], state = null, evId = +localStorage.getItem('ev') || null, sesId = +localStorage.getItem('ses') || null;
let mode = 'pan', pulseT = null, searchIdx = 0, searchQ = '';
const selected = new Set();          // выбранные ключи мест
const owner = new Map();             // key → {o: заказ, free: bool}
let sug = null;                      // предложение авто-подбора {keys:Set, summary, alt, alternatives, split, count}
const cur = () => (state && state.currency) || '';

// ───────────────────────── зум / панорама (ваш код) ─────────────────────────
function setVB(v) {
  const k = W / v[2]; const kk = Math.min(Math.max(k, 1), 10); v[2] = W / kk; v[3] = H / kk;
  v[0] = Math.min(Math.max(v[0], -40), W + 40 - v[2]); v[1] = Math.min(Math.max(v[1], -40), H + 40 - v[3]);
  vb = v; svg.setAttribute('viewBox', v.join(' ')); svg.classList.toggle('z', W / v[2] >= 2.2);
}
const pt = (x, y) => { const p = svg.createSVGPoint(); p.x = x; p.y = y; return p.matrixTransform(svg.getScreenCTM().inverse()); };
function zoomAt(f, cx, cy) {
  const p = cx == null ? { x: vb[0] + vb[2] / 2, y: vb[1] + vb[3] / 2 } : pt(cx, cy), w = vb[2] / f, h = vb[3] / f;
  setVB([p.x - (p.x - vb[0]) / f, p.y - (p.y - vb[1]) / f, w, h]);
}
svg.addEventListener('wheel', e => { e.preventDefault(); zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX, e.clientY); }, { passive: false });
$('#zi').onclick = () => zoomAt(1.5); $('#zo').onclick = () => zoomAt(1 / 1.5); $('#zr').onclick = () => setVB([0, 0, W, H]);

/** Плавно (без анимации) приблизить так, чтобы все переданные места уместились в кадр. */
function fitTo(items, pad = 45) {
  if (!items.length) return;
  const xs = items.map(s => s.x), ys = items.map(s => s.y);
  const x0 = Math.min(...xs) - pad, x1 = Math.max(...xs) + pad, y0 = Math.min(...ys) - pad, y1 = Math.max(...ys) + pad;
  const k = Math.min(W / (x1 - x0), H / (y1 - y0)), w = W / k, h = H / k;   // setVB сам ограничит масштаб 1…10
  setVB([(x0 + x1) / 2 - w / 2, (y0 + y1) / 2 - h / 2, w, h]);
}

// ───────────────────────── отрисовка ─────────────────────────
function paintSeat(k) {
  const s = seats.get(k), ow = owner.get(k), c = s.el.classList;
  if (ow) {
    const paid = ow.o.status === 'paid';
    s.el.style.fill = paid ? ow.o.color : mix(ow.o.color, '#ffffff', .6);   // оплачен — насыщенный, бронь — бледный
    c.add('booked'); c.toggle('free', ow.free); s.label.classList.toggle('w', paid);
  } else {
    const rule = rowRuleFor(s.z, s.r);               // свободное место в «цветном» ряду — мягкая подсветка его цветом
    s.el.style.fill = rule && rule.color ? mix(rule.color, '#ffffff', .68) : '';
    c.remove('booked', 'free'); s.label.classList.remove('w');
  }
  c.toggle('sel', selected.has(k)); c.toggle('sug', !!(sug && sug.keys.has(k)));
}
const paintAll = () => seats.forEach((_, k) => paintSeat(k));

function render() {
  owner.clear();
  if (state) state.orders.forEach(o => o.seats.forEach(([z, r, s, f]) => owner.set(K(z, r, s), { o, free: f })));
  for (const k of [...selected]) if (!seats.has(k)) selected.delete(k);
  if (sug && [...sug.keys].some(k => owner.has(k))) sug = null;   // кто-то занял предложенное
  paintAll(); renderStats(); renderOrders(); renderLog(); renderSel(); renderSug();
  $('#expAll').href = state ? `/api/sessions/${state.session.id}/export.csv` : '#';
  $('#expXlsx').href = state ? `/api/events/${state.session.event_id}/export.xlsx` : '#';
}

function renderStats() {
  const st = state && state.stats;
  if (!st) { $('#stats').textContent = 'Создайте мероприятие и сеанс'; $('#bar').innerHTML = ''; $('#money').textContent = ''; return; }
  $('#stats').innerHTML = `Всего: <b>${fmt(st.total)}</b> мест | Свободно: <b>${fmt(st.free)}</b> | В брони: <b>${fmt(st.reserved)}</b> | Оплачено: <b>${fmt(st.paid)}</b>`;
  const pc = n => (100 * n / st.total).toFixed(2) + '%';
  $('#bar').innerHTML = `<i style="width:${pc(st.paid)};background:var(--ok)" title="Оплачено"></i><i style="width:${pc(st.reserved)};background:var(--warn)" title="В брони"></i>`;
  $('#money').textContent = `Оплачено ${fmt(st.paid_amount)} ${cur()} · в брони ${fmt(st.reserved_amount)} ${cur()}`;
}

function renderOrders() {
  const box = $('#orders'), q = $('#q').value.trim().toLowerCase();
  const list = (state ? state.orders : []).filter(o => !q || (o.name + ' ' + o.comment).toLowerCase().includes(q));
  $('#qList').innerHTML = (state ? state.orders : []).map(o => `<option value="${esc(o.name)}">`).join('');
  if (!state) { box.innerHTML = '<div class="empty">Нет выбранного сеанса.</div>'; return; }
  if (!list.length) { box.innerHTML = `<div class="empty">${q ? 'Ничего не найдено.' : 'Заказов пока нет. Выделите места на схеме и нажмите «Новый заказ».'}</div>`; return; }
  box.innerHTML = list.map(o => `<div class="ord" data-id="${o.id}">
    <i class="chip ${o.status === 'paid' ? '' : 'res'}" style="--c:${o.color}"></i>
    <div class="ob"><b><span class="oid">№${o.id}</span> ${esc(o.name)}<span class="tag ${o.status}">${o.status === 'paid' ? 'Оплачен' : 'Бронь'}</span></b>
      <small>${seatsW(o.seats.length)}${o.free_seats ? `, из них бесплатных ${o.free_seats}` : ''} · ${fmt(o.total_amount)} ${cur()}${o.comment ? '<br>' + esc(o.comment) : ''}</small>
      <label class="prn" title="Отметьте, когда билеты напечатаны. Если в заказ добавят места, отметка снимется."><input type="checkbox" data-print="${o.id}"${o.printed ? ' checked' : ''}> Напечатано</label></div>
    <button data-edit="${o.id}" title="Изменить заказ">✎</button></div>`).join('');
}
$('#orders').addEventListener('click', e => {
  const pr = e.target.closest('[data-print]');
  if (pr) {                                   // галочка «Напечатано»: не открываем заказ, а сохраняем отметку
    e.stopPropagation();
    safe(async () => { await api('PATCH', `/api/orders/${pr.dataset.print}`, { printed: pr.checked }); await loadState(true); })();
    return;
  }
  const ed = e.target.closest('[data-edit]'), row = e.target.closest('.ord');
  const id = +((ed && ed.dataset.edit) || (row && row.dataset.id));
  const o = state && state.orders.find(o => o.id === id);
  if (!o) return;
  if (ed) openOrder(o); else focusOrder(o);
});

const tsFmt = iso => {
  const d = new Date(iso), today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
};
function renderLog() {
  const a = state ? state.audit : [];
  $('#log').innerHTML = a.length ? a.map(x => `<div class="ev"><time>${tsFmt(x.ts)}</time>${esc(x.text)} <small>— ${esc(x.actor)}</small></div>`).join('')
    : '<div class="empty">Действий пока нет.</div>';
}

// ───────────────────────── выбор мест ─────────────────────────
function setSelected(keys, how) {           // how: 'add' | 'sub' | 'replace'
  if (how === 'replace') { const old = [...selected]; selected.clear(); old.forEach(paintSeat); }
  keys.forEach(k => { how === 'sub' ? selected.delete(k) : selected.add(k); paintSeat(k); });
  renderSel();
}
function freeNote(k) {              // для свободного места — «свободно, 250 ₽», если известна цена сеанса
  const s = seats.get(k);
  return state ? `свободно, ${fmt(priceFor(s.z, s.r))} ${cur()}` : 'свободно';
}
function showInfo(k) {
  const ow = owner.get(k);
  $('#info').innerHTML = esc(seatName(k)) + (ow ? ` <span>— ${esc(ow.o.name)}, ${ow.o.status === 'paid' ? 'оплачен' : 'бронь'}${ow.free ? ', бесплатное' : ''}</span>` : ` <span>— ${esc(freeNote(k))}</span>`);
}
function renderSel() {
  const bar = $('#selbar'), n = selected.size; bar.hidden = !n;
  const freeK = [...selected].filter(k => !owner.has(k)), ownK = [...selected].filter(k => owner.has(k));
  const orders = new Set(ownK.map(k => owner.get(k).o.id));
  $('#selText').textContent = `Выбрано ${n}` + (freeK.length !== n ? ` (свободных ${freeK.length}, в заказах ${ownK.length})` : '');
  $('#selNew').disabled = !freeK.length;
  $('#selAdd').disabled = $('#selTo').disabled = !freeK.length || !(state && state.orders.length);
  $('#selDel').disabled = $('#selFree').disabled = $('#selPaid').disabled = !ownK.length;
  const cur_ = $('#selTo').value;
  $('#selTo').innerHTML = (state ? state.orders : []).map(o => `<option value="${o.id}">${esc(o.name)}</option>`).join('');
  if (cur_ && [...$('#selTo').options].some(o => o.value === cur_)) $('#selTo').value = cur_;
  else if (orders.size === 1) $('#selTo').value = [...orders][0];       // предлагаем заказ, чьи места уже выбраны рядом
}
const groupByOrder = keys => {
  const g = new Map();
  keys.forEach(k => { const ow = owner.get(k); if (ow) { if (!g.has(ow.o.id)) g.set(ow.o.id, []); g.get(ow.o.id).push(seatObj(k)); } });
  return g;
};
const seatsCall = (id, body) => api('POST', `/api/orders/${id}/seats`, body);

$('#selClear').onclick = () => setSelected([...selected], 'sub');
$('#selNew').onclick = () => openNewOrder([...selected].filter(k => !owner.has(k)));
$('#selAdd').onclick = safe(async () => {
  const free = [...selected].filter(k => !owner.has(k)), id = +$('#selTo').value;
  await seatsCall(id, { add: free.map(seatObj) });
  setSelected([...selected], 'sub'); await loadState(true);
});
$('#selDel').onclick = safe(async () => {
  const g = groupByOrder([...selected]), n = [...g.values()].reduce((a, b) => a + b.length, 0);
  if (!confirm(`Убрать ${seatsW(n)} из заказов?`)) return;
  for (const [id, arr] of g) await seatsCall(id, { remove: arr });
  setSelected([...selected], 'sub'); await loadState(true);
});
const markFree = v => safe(async () => {
  for (const [id, arr] of groupByOrder([...selected])) await seatsCall(id, v ? { set_free: arr } : { set_paid: arr });
  await loadState(true);
});
$('#selFree').onclick = markFree(true); $('#selPaid').onclick = markFree(false);

// ───────────────────────── мышь: рука / выделение областью ─────────────────────────
function setMode(m) {
  mode = m; $('#mPan').classList.toggle('on', m === 'pan'); $('#mLasso').classList.toggle('on', m === 'lasso');
  svg.classList.toggle('lasso', m === 'lasso');
}
$('#mPan').onclick = () => setMode('pan'); $('#mLasso').onclick = () => setMode('lasso');

let dn = null;
svg.addEventListener('pointerdown', e => {
  if (e.button !== 0) return;
  const lasso = mode === 'lasso' || e.shiftKey;
  dn = { x: e.clientX, y: e.clientY, vb: vb.slice(), c: e.target.closest('circle'), m: 0, lasso,
    sub: e.altKey || e.ctrlKey || e.metaKey, p0: lasso ? pt(e.clientX, e.clientY) : null, p1: null };
});
addEventListener('pointermove', e => {
  if (!dn) return;
  const dx = e.clientX - dn.x, dy = e.clientY - dn.y;
  if (!dn.m && Math.hypot(dx, dy) > 5) { dn.m = 1; tip.style.display = 'none'; if (dn.lasso) marq.style.display = ''; else svg.classList.add('drag'); }
  if (!dn.m) return;
  if (dn.lasso) {
    const p = dn.p1 = pt(e.clientX, e.clientY);
    marq.setAttribute('x', Math.min(dn.p0.x, p.x)); marq.setAttribute('y', Math.min(dn.p0.y, p.y));
    marq.setAttribute('width', Math.abs(p.x - dn.p0.x)); marq.setAttribute('height', Math.abs(p.y - dn.p0.y));
  } else {
    const r = svg.getBoundingClientRect(), k = Math.max(dn.vb[2] / r.width, dn.vb[3] / r.height);
    setVB([dn.vb[0] - dx * k, dn.vb[1] - dy * k, dn.vb[2], dn.vb[3]]);
  }
});
addEventListener('pointerup', () => {
  if (!dn) return;
  if (dn.m && dn.lasso && dn.p1) {
    const x0 = Math.min(dn.p0.x, dn.p1.x), x1 = Math.max(dn.p0.x, dn.p1.x), y0 = Math.min(dn.p0.y, dn.p1.y), y1 = Math.max(dn.p0.y, dn.p1.y);
    const hit = []; seats.forEach((s, k) => { if (s.x >= x0 && s.x <= x1 && s.y >= y0 && s.y <= y1) hit.push(k); });
    setSelected(hit, dn.sub ? 'sub' : 'add');
    if (hit.length) $('#info').innerHTML = `Рамкой ${dn.sub ? 'снято' : 'выбрано'}: ${seatsW(hit.length)}`;
  } else if (!dn.m && dn.c) {                                   // обычный клик — переключить место
    const k = K(dn.c.dataset.z, dn.c.dataset.r, dn.c.dataset.s);
    setSelected([k], selected.has(k) ? 'sub' : 'add'); showInfo(k);
  } else if (!dn.m) clearPulse();
  marq.style.display = 'none'; dn = null; svg.classList.remove('drag');
});
svg.addEventListener('mousemove', e => {
  const c = e.target.closest('circle');
  if (!c || (dn && dn.m)) { tip.style.display = 'none'; return; }
  const k = K(c.dataset.z, c.dataset.r, c.dataset.s), ow = owner.get(k);
  tip.textContent = seatName(k) + (ow ? ` — ${ow.o.name} (${ow.o.status === 'paid' ? 'оплачен' : 'бронь'}${ow.free ? ', бесплатное' : ''})` : ` — ${freeNote(k)}`);
  tip.style.display = 'block'; tip.style.left = e.clientX + 14 + 'px'; tip.style.top = e.clientY + 14 + 'px';
});
svg.addEventListener('mouseleave', () => tip.style.display = 'none');
addEventListener('keydown', e => {
  if (e.key === 'Escape' && !document.querySelector('dialog[open]')) { setSelected([...selected], 'sub'); dropSug(); clearPulse(); }
});

// поиск конкретного места (из исходного файла)
$('#fz').onchange = () => $('#fr').style.display = zoneHasRows($('#fz').value) ? '' : 'none';   // у зон без рядов поле «Ряд» не нужно
$('#fb').onclick = () => {
  const z = $('#fz').value, r = zoneHasRows(z) ? $('#fr').value.trim() : '', s = $('#fs').value.trim().replace(',', '.'), k = K(z, r, s);
  if (!seats.has(k)) { $('#info').innerHTML = '<span>Такого места нет</span>'; return; }
  setSelected([k], 'replace'); showInfo(k); fitTo([seats.get(k)], 60);
};

// ───────────────────────── умный поиск заказа: зум + пульсация ─────────────────────────
function clearPulse() { clearTimeout(pulseT); svg.querySelectorAll('circle.pulse').forEach(c => c.classList.remove('pulse')); }
function focusOrder(o) {
  clearPulse();
  const items = o.seats.map(([z, r, s]) => seats.get(K(z, r, s))).filter(Boolean);
  if (!items.length) { toast('В этом заказе пока нет мест'); return; }
  fitTo(items); items.forEach(s => s.el.classList.add('pulse'));
  pulseT = setTimeout(clearPulse, 8000);
}
function searchGo(reset) {
  const q = $('#q').value.trim().toLowerCase(); renderOrders();
  if (!q || !state) { $('#qCount').textContent = ''; clearPulse(); return; }
  const m = state.orders.filter(o => (o.name + ' ' + (o.comment || '')).toLowerCase().includes(q));
  if (!m.length) { $('#qCount').textContent = 'не найдено'; clearPulse(); return; }
  searchIdx = (reset || q !== searchQ) ? 0 : (searchIdx + 1) % m.length; searchQ = q;
  $('#qCount').textContent = m.length > 1 ? `${searchIdx + 1} из ${m.length}` : '';
  focusOrder(m[searchIdx]);
}
$('#q').addEventListener('input', debounce(() => searchGo(true), 350));
$('#q').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); searchGo(false); } });

// ───────────────────────── авто-подбор ─────────────────────────
function dropSug() { const old = sug; sug = null; if (old) old.keys.forEach(paintSeat); renderSug(); }
function renderSug() {
  const b = $('#sugbar'); b.hidden = !sug; if (!sug) return;
  $('#sugText').textContent = `Найдено ${seatsW(sug.keys.size)}: ` + sug.summary.join('; ') + (sug.split ? ' — цельного блока нет, подобрано несколькими частями' : '');
  $('#sugAlt').hidden = sug.alternatives < 2;
}
const runAutopick = safe(async alt => {
  if (!state) return toast('Сначала выберите сеанс', true);
  const n = parseInt($('#apN').value, 10);
  if (!(n > 0)) return toast('Введите, сколько мест нужно', true);
  const r = await api('POST', `/api/sessions/${state.session.id}/autopick`,
    { count: n, alt, zones: $('#apSide').checked ? ZONES : hall.default_zones });
  const old = sug; sug = { keys: new Set(r.seats.map(s => K(s.zone, s.row, s.seat))), summary: r.summary, alt: r.alt, alternatives: r.alternatives, split: r.split };
  if (old) old.keys.forEach(paintSeat);
  sug.keys.forEach(paintSeat); renderSug(); fitTo([...sug.keys].map(k => seats.get(k)));
});
$('#apGo').onclick = () => runAutopick(0);
$('#apN').addEventListener('keydown', e => { if (e.key === 'Enter') runAutopick(0); });
$('#sugAlt').onclick = () => runAutopick(sug ? sug.alt + 1 : 0);
$('#sugNo').onclick = dropSug;
$('#sugOk').onclick = () => { const keys = [...sug.keys]; dropSug(); setSelected(keys, 'replace'); openNewOrder(keys); };

// ───────────────────────── калькулятор ─────────────────────────
/** Та же логика, что и row_rule/seat_price на сервере: правило по ряду, под которое попадает место
    (первое подходящее в списке), или null — тогда действует цена зоны / базовая цена сеанса. */
function rowRuleFor(zone, row) {
  if (!state) return null;
  const r = row && /^\d+$/.test(row) ? +row : null;
  return (state.session.row_prices || []).find(x => {
    if (x.zone !== zone) return false;
    if (x.row_from == null || x.row_to == null) return true; // правило на всю зону
    return r !== null && r >= x.row_from && r <= x.row_to;
  }) || null;
}
const priceFor = (zone, row, override) => override != null && override !== '' ? +override
  : (rowRuleFor(zone, row)?.price ?? (state.session.zone_prices[zone] ?? state.session.base_price));
/** items: [{zone, row, free}] → итог; бесплатные места в сумму не входят (та же формула, что на сервере). */
function calc(items, override) {
  const paid = items.filter(i => !i.free);
  return { paid: paid.length, free: items.length - paid.length, total: paid.reduce((s, i) => s + priceFor(i.zone, i.row, override), 0) };
}

// ───────────────────────── диалоги ─────────────────────────
let dlgCtx = null;   // {mode:'new'|'edit', keys, order}
const dlg = $('#dlgOrder');
const nextColor = () => hslHex(((state ? state.orders.length : 0) * 137.508) % 360, 72, 42);

function openNewOrder(keys) {
  if (!state || !keys.length) return;
  dlgCtx = { mode: 'new', keys };
  $('#oTitle').textContent = `Новый заказ · ${seatsW(keys.length)}`;
  $('#oName').value = $('#oComment').value = $('#oPrice').value = ''; $('#oStatus').value = 'reserved';
  $('#oColor').value = nextColor(); $('#oFree').value = 0; $('#oFree').max = keys.length;
  $('#oFreeBox').hidden = false; $('#oDel').hidden = $('#oCsv').hidden = true;
  $('#oHint').textContent = 'Бесплатные места помечаются по краям блока; потом их можно поменять: выделите места на схеме и нажмите «Бесплатные» / «Платные».';
  $('#oPrice').placeholder = 'по прайсу сеанса'; $('#oOk').textContent = 'Создать заказ';
  calcPreview(); dlg.showModal(); $('#oName').focus();
}
function openOrder(o) {
  dlgCtx = { mode: 'edit', order: o };
  $('#oTitle').textContent = `Заказ №${o.id} · ${o.name}`;
  $('#oName').value = o.name; $('#oComment').value = o.comment; $('#oStatus').value = o.status; $('#oColor').value = o.color;
  $('#oPrice').value = o.price_override ?? ''; $('#oPrice').placeholder = 'по прайсу сеанса';
  // поле «бесплатных» скрыто, но по нему считаются итоги и сохранение: берём реальное число бесплатных мест заказа
  $('#oFree').max = o.seats.length; $('#oFree').value = o.seats.filter(x => x[3]).length;
  $('#oFreeBox').hidden = true; $('#oDel').hidden = $('#oCsv').hidden = false; $('#oHint').textContent = '';
  $('#oCsv').href = `/api/orders/${o.id}/export.csv`; $('#oOk').textContent = 'Сохранить';
  calcPreview(); dlg.showModal();
}
/** Итоги считаются на лету — бесплатные места не учитываются. */
function pickFreeKeys(keys, n) {              // n «крайних» мест блока (у краёв рядов — обычно сопровождающие)
  const cx = keys.reduce((a, k) => a + seats.get(k).x, 0) / keys.length;
  return new Set([...keys].sort((a, b) => Math.abs(seats.get(b).x - cx) - Math.abs(seats.get(a).x - cx)).slice(0, n));
}
const freeN = max => Math.max(0, Math.min(+$('#oFree').value || 0, max));   // введённое «бесплатных», в пределах 0…мест
/** Правка существующего заказа: какие места станут бесплатными / платными, чтобы бесплатных стало ровно n.
    Уже бесплатные сохраняются, пока их не больше n; добавляются/снимаются самые «крайние» места блока. */
function planFree(o, n) {
  const all = o.seats.map(([z, r, s, f]) => ({ k: K(z, r, s), f }));
  const cx = all.reduce((a, x) => a + seats.get(x.k).x, 0) / all.length;
  const far = (a, b) => Math.abs(seats.get(b.k).x - cx) - Math.abs(seats.get(a.k).x - cx);
  const free = all.filter(x => x.f).sort(far), paid = all.filter(x => !x.f).sort(far);
  return n > free.length ? { toFree: paid.slice(0, n - free.length).map(x => x.k), toPaid: [] }
                         : { toFree: [], toPaid: free.slice(n).map(x => x.k) };
}
function calcPreview() {
  if (!dlgCtx) return;
  const ov = $('#oPrice').value;
  let items;
  if (dlgCtx.mode === 'new') {
    const n = freeN(dlgCtx.keys.length), fr = pickFreeKeys(dlgCtx.keys, n);
    items = dlgCtx.keys.map(k => ({ zone: seats.get(k).z, row: seats.get(k).r, free: fr.has(k) }));
  } else {
    const o = dlgCtx.order, p = planFree(o, freeN(o.seats.length)), tf = new Set(p.toFree), tp = new Set(p.toPaid);
    items = o.seats.map(([z, r, s, f]) => ({ zone: z, row: r, free: tf.has(K(z, r, s)) || (f && !tp.has(K(z, r, s))) }));
  }
  const c = calc(items, ov);
  $('#oCalc').innerHTML = `Платных: <b>${c.paid}</b> · бесплатных: <b>${c.free}</b><br>Итого: <b>${fmt(c.total)} ${cur()}</b>`
    + (ov === '' ? '' : ` <small>(цена билета ${fmt(ov)} ${cur()})</small>`);
}
['#oPrice', '#oFree'].forEach(s => $(s).addEventListener('input', calcPreview));$('#oOk').addEventListener('click', safe(async e => {
  e.preventDefault(); const f = dlg.querySelector('form'); if (!f.reportValidity()) return;
  const body = { name: $('#oName').value, comment: $('#oComment').value, status: $('#oStatus').value,
    color: $('#oColor').value, price_override: $('#oPrice').value === '' ? null : +$('#oPrice').value };
  if (dlgCtx.mode === 'new') {
    const n = Math.max(0, Math.min(+$('#oFree').value || 0, dlgCtx.keys.length)), fr = pickFreeKeys(dlgCtx.keys, n);
    body.seats = dlgCtx.keys.map(k => ({ ...seatObj(k), is_free: fr.has(k) }));
    await api('POST', `/api/sessions/${state.session.id}/orders`, body);
    setSelected([...selected], 'sub'); toast('Заказ создан');
    } else {
    const o = dlgCtx.order, p = planFree(o, freeN(o.seats.length));
    await api('PATCH', `/api/orders/${o.id}`, body);
    if (p.toFree.length) await seatsCall(o.id, { set_free: p.toFree.map(seatObj) });   // бесплатные места
    if (p.toPaid.length) await seatsCall(o.id, { set_paid: p.toPaid.map(seatObj) });
    toast('Сохранено');
  }
  dlg.close(); await loadState(true);
}));
$('#oDel').onclick = safe(async () => {
  const o = dlgCtx.order; if (!confirm(`Удалить заказ «${o.name}» и освободить ${seatsW(o.seats.length)}?`)) return;
  await api('DELETE', `/api/orders/${o.id}`); dlg.close(); await loadState(true);
});
document.querySelectorAll('dialog').forEach(d => d.addEventListener('click', e => { if (e.target === d) d.close(); }));
// «Отмена» — обычная кнопка, а не submit: иначе Enter в поле формы сработал бы как отмена
document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => b.closest('dialog').close());

// мероприятие
$('#evAdd').onclick = () => { $('#eName').value = $('#eDesc').value = ''; $('#dlgEvent').showModal(); $('#eName').focus(); };
$('#eOk').addEventListener('click', safe(async e => {
  e.preventDefault(); if (!$('#eName').value.trim()) return $('#eName').reportValidity();
  const r = await api('POST', '/api/events', { name: $('#eName').value, description: $('#eDesc').value });
  $('#dlgEvent').close(); evId = r.id; sesId = null; await loadEvents(); toast('Мероприятие создано — добавьте сеанс');
}));

// удаление мероприятия (каскадом уходят сеансы, заказы, брони и история)
$('#evDel').onclick = safe(async () => {
  const ev = events.find(e => e.id === evId);
  if (!ev) return toast('Мероприятие не выбрано', true);
  const n = ev.sessions.length, o = ev.orders || 0;
  const what = n ? `Будет удалено вместе с ним: сеансов — ${n}, заказов — ${o}, а также все брони и история. Отменить нельзя.` : 'Сеансов в нём нет.';
  if (!confirm(`Удалить мероприятие «${ev.name}»?\n\n${what}`)) return;
  if (o && prompt(`В мероприятии есть заказы (${o}). Для подтверждения введите его название:\n${ev.name}`) !== ev.name) return toast('Название не совпало — удаление отменено');
  await api('DELETE', `/api/events/${ev.id}`);
  evId = null; sesId = null; await loadEvents(); toast('Мероприятие удалено');
});


// удаление сеанса (одной даты/времени): вместе с ним уходят заказы, брони и история этого сеанса
$('#sesDel').onclick = safe(async () => {
  const ev = events.find(e => e.id === evId), ses = ev && ev.sessions.find(x => x.id === sesId);
  if (!ses) return toast('Сеанс не выбран', true);
  const t = ses.starts_at, label = `${t.slice(8, 10)}.${t.slice(5, 7)} ${t.slice(11, 16)}`;
  const o = state && state.session.id === ses.id ? state.orders.length : 0;
  const what = o ? `Будет удалено вместе с ним: заказов — ${o}, а также все брони и история этого сеанса. Отменить нельзя.` : 'Заказов в нём нет.';
  if (!confirm(`Удалить сеанс ${label}?\n\n${what}`)) return;
  if (o && prompt(`В сеансе есть заказы (${o}). Для подтверждения введите дату и время сеанса:\n${label}`) !== label) return toast('Не совпало — удаление отменено');
  await api('DELETE', `/api/sessions/${ses.id}`);
  sesId = null; await loadEvents(); toast('Сеанс удалён');
});

// сеанс (создание / правка цен / копия)
let sesEditing = false;
function openSession(edit, copy = false) {
  if (!evId) return toast('Сначала создайте мероприятие', true);
  if ((edit || copy) && !state) return toast('Сеанс не выбран', true);
  sesEditing = edit; const s = edit || copy ? state.session : null;
  $('#sTitle').textContent = edit ? 'Изменить сеанс' : (copy ? 'Копия сеанса (новая дата)' : 'Новый сеанс');
  const d = new Date(); d.setDate(d.getDate() + 1); d.setHours(10, 0, 0, 0);
  const pad = n => String(n).padStart(2, '0'), local = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  $('#sDate').value = s && edit ? s.starts_at : local(d);
  $('#sPrice').value = s ? s.base_price : 0;
  const hallKey = s ? s.hall : (hall ? hall.key : HALLS[0].key);          // новый сеанс — в том зале, что открыт сейчас
  renderHallPick(hallKey, edit && state.orders.some(o => o.seats.length));   // с занятыми местами зал менять нельзя
  renderZonePrices(s ? s.zone_prices : {});
  $('#sRows').innerHTML = '';
  (s ? s.row_prices : []).forEach(addRowRule);
  $('#dlgSession').showModal();
}
$('#sesAdd').onclick = () => openSession(false);
$('#sesEdit').onclick = () => openSession(true);
$('#sesCopy').onclick = () => openSession(false, true);

/** Одна строка правила «зона, ряд с … по …, цена» + подсветка на схеме одним из 10 цветов.
    Список зон в <select> — от зала, выбранного в диалоге (zoneOptsFor). */
const hallZones = key => (hallMeta(key) || hall).zones.map(z => z.name);
const dlgHallKey = () => ($('#sHall input:checked') || {}).value || (hall && hall.key);
const zoneOptsFor = key => hallZones(key).map(z => `<option value="${esc(z)}">${esc(z)}</option>`).join('');
/** Выбор зала в диалоге сеанса. Если залов меньше двух — блок скрыт, сеанс идёт в единственный. */
function renderHallPick(key, locked) {
  $('#sHallBox').hidden = HALLS.length < 2; $('#sHallNote').hidden = !locked;
  $('#sHall').innerHTML = HALLS.map(h => `<label><input type="radio" name="sHall" value="${esc(h.key)}"${h.key === key ? ' checked' : ''}${locked ? ' disabled' : ''}> ${esc(h.name)} <small>${seatsW(h.total)}</small></label>`).join('');
  $('#sHall').querySelectorAll('input').forEach(i => i.onchange = onHallPick);
}
/** Цены по зонам выбранного зала; values — {зона: цена}, что уже введено/сохранено. */
function renderZonePrices(values) {
  $('#sZones').innerHTML = hallZones(dlgHallKey()).map(z => `<label>${esc(z)}<input type="number" min="0" data-zone="${esc(z)}" value="${values[z] != null ? values[z] : ''}" placeholder="как по умолчанию"></label>`).join('');
}
/** Сменили зал в диалоге: переносим введённые цены на зоны с теми же названиями, лишние правила по рядам убираем. */
function onHallPick() {
  const typed = {}; $('#sZones').querySelectorAll('input').forEach(i => { if (i.value !== '') typed[i.dataset.zone] = +i.value; });
  renderZonePrices(typed);
  const key = dlgHallKey(), zs = hallZones(key); let dropped = 0;
  [...$('#sRows').children].forEach(row => {
    const sel = row.querySelector('[data-f=zone]'), was = sel.value;
    if (!zs.includes(was)) { row.remove(); dropped++; return; }
    sel.innerHTML = zoneOptsFor(key); sel.value = was;
  });
  if (dropped) toast(`Убрано правил цен по рядам: ${dropped} — таких зон нет в выбранном зале`);
}
function swatchesHtml(selected) {
  const want = selected || '';
  const sw = c => `<button type="button" class="${c ? '' : 'none'}${c === want ? ' on' : ''}" data-c="${c}" style="${c ? `--sw:${c}` : ''}" title="${c || 'Без подсветки'}">${c ? '' : '∅'}</button>`;
  return `<div class="swatches">${sw('')}${ROW_COLORS.map(sw).join('')}</div>`;
}
function addRowRule(rule) {
  const row = document.createElement('div'); row.className = 'rrow';
  row.innerHTML = `<div class="rtop"><select data-f="zone">${zoneOptsFor(dlgHallKey())}</select>
    <input data-f="row_from" type="number" min="1" placeholder="все">
    <input data-f="row_to" type="number" min="1" placeholder="все">
    <input data-f="price" type="number" min="0" placeholder="цена, ${cur()}" required>
    <button type="button" title="Удалить правило">✕</button></div>
    ${swatchesHtml(rule && rule.color)}`;
  if (rule) {
    row.querySelector('[data-f=zone]').value = rule.zone;
    row.querySelector('[data-f=row_from]').value = rule.row_from ?? '';
    row.querySelector('[data-f=row_to]').value = rule.row_to ?? '';
    row.querySelector('[data-f=price]').value = rule.price;
  }
  row.querySelector('.rtop button').onclick = () => row.remove();
  row.querySelectorAll('.swatches button').forEach(b => b.onclick = () => {
    row.querySelectorAll('.swatches button').forEach(x => x.classList.remove('on')); b.classList.add('on');
  });
  $('#sRows').appendChild(row);
}
$('#sRowAdd').onclick = () => addRowRule(null);

$('#sOk').addEventListener('click', safe(async e => {
  e.preventDefault(); if (!$('#dlgSession form').reportValidity()) return;
  const zp = {}; $('#sZones').querySelectorAll('input').forEach(i => { if (i.value !== '') zp[i.dataset.zone] = +i.value; });
  const rowRules = [...$('#sRows').children].map(r => {
    const rf = r.querySelector('[data-f=row_from]').value;
    const rt = r.querySelector('[data-f=row_to]').value;
    return {
      zone: r.querySelector('[data-f=zone]').value,
      row_from: rf === '' ? null : +rf,
      row_to: rt === '' ? null : +rt,
      price: +r.querySelector('[data-f=price]').value,
      color: r.querySelector('.swatches button.on')?.dataset.c || null
    };
  });
  if (rowRules.some(r => r.row_from !== null && r.row_to !== null && r.row_to < r.row_from)) return toast('В правиле «ряд по» не может быть меньше «ряд с»', true);
  const body = { hall: dlgHallKey(), starts_at: $('#sDate').value, base_price: +$('#sPrice').value || 0, zone_prices: zp, row_prices: rowRules };
  if (sesEditing) await api('PATCH', `/api/sessions/${sesId}`, body);
  else { const r = await api('POST', `/api/events/${evId}/sessions`, body); sesId = r.id; }
  $('#dlgSession').close(); await loadEvents(); toast('Сохранено');
}));

// ───────────────────────── загрузка данных ─────────────────────────
const sesLabel = (iso, hallKey) => new Date(iso).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  + (HALLS.length > 1 ? ' · ' + ((hallMeta(hallKey) || {}).name || hallKey) : '');   // название зала — только если залов несколько
async function loadEvents() {
  events = await api('GET', '/api/events');
  if (!events.some(e => e.id === evId)) evId = events.length ? events[0].id : null;
  $('#evSel').innerHTML = events.map(e => `<option value="${e.id}">${esc(e.name)}</option>`).join('') || '<option>— нет мероприятий —</option>';
  if (evId) $('#evSel').value = evId;
  const ev = events.find(e => e.id === evId), sess = ev ? ev.sessions : [];
  if (!sess.some(s => s.id === sesId)) sesId = sess.length ? sess[0].id : null;
  $('#sesSel').innerHTML = sess.map(s => `<option value="${s.id}">${sesLabel(s.starts_at, s.hall)}</option>`).join('') || '<option>— нет сеансов —</option>';
  if (sesId) $('#sesSel').value = sesId;
  localStorage.setItem('ev', evId || ''); localStorage.setItem('ses', sesId || '');
  await loadState(true);
}
async function loadState(force) {
  if (!sesId) { state = null; selected.clear(); sug = null; await ensureHall(hall ? hall.key : HALLS[0].key); render(); return; }
  const st = await api('GET', `/api/sessions/${sesId}/state`);
  if (!force && state && state.session.id === st.session.id && state.rev === st.rev) return;   // ничего не изменилось
  await ensureHall(st.session.hall);                                                          // у сеанса свой зал — подменяем схему
  if (state && state.session.id !== st.session.id) { selected.clear(); sug = null; clearPulse(); }
  state = st; render();
}
$('#evSel').onchange = () => { evId = +$('#evSel').value; sesId = null; loadEvents().catch(e => toast(e.message, true)); };
$('#sesSel').onchange = () => { sesId = +$('#sesSel').value; localStorage.setItem('ses', sesId); loadState(true).catch(e => toast(e.message, true)); };
$('#tabOrders').onclick = () => tab(true); $('#tabLog').onclick = () => tab(false);
function tab(orders) { $('#orders').hidden = !orders; $('#log').hidden = orders; $('#tabOrders').classList.toggle('on', orders); $('#tabLog').classList.toggle('on', !orders); }

// оператор (кто совершает действия — попадает в журнал)
function askOperator(force) {
  const n = prompt('Ваше имя — оно будет записано в журнал действий:', opName || '');
  if (n !== null && n.trim()) { opName = n.trim().slice(0, 64); localStorage.setItem('op', opName); }
  else if (!opName) opName = 'Оператор';
  $('#op').textContent = '👤 ' + opName;
}
$('#op').onclick = () => askOperator(true);

// ───────────────────────── старт ─────────────────────────
if (!opName) askOperator(); else $('#op').textContent = '👤 ' + opName;
let ROW_COLORS = sv_defaultPalette();   // подменится настоящей палитрой из /api/config, но свотчи доступны сразу
function sv_defaultPalette() { return ['#e53935', '#fb8c00', '#fdd835', '#7cb342', '#00897b', '#1e88e5', '#3949ab', '#8e24aa', '#d81b60', '#6d4c41']; }
api('GET', '/api/config').then(c => { ROW_COLORS = c.row_colors; }).catch(() => { });   // источник правды — сервер; это лишь запасной вариант на случай сбоя сети
api('GET', '/api/halls').then(h => { HALLS = h; return loadEvents(); }).catch(e => toast(e.message, true));
// «реальное время»: раз в 10 с подтягиваем изменения, сделанные с других компьютеров
setInterval(() => { if (!document.hidden && !dn && !document.querySelector('dialog[open]')) loadState().catch(() => { }); }, 10000);
})();
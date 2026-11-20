const $ = s => document.querySelector(s);
const esc = t => String(t ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const PAGE = 100;
let rows = [];          // все загруженные записи, новые сверху
let hasMore = false;

const tsFmt = iso => new Date(iso).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
const evQuery = () => $('#evFilter').value ? '&event_id=' + $('#evFilter').value : '';

function render() {
  $('#glog').innerHTML = rows.length ? rows.map(x =>
    `<div class="ev"><time>${tsFmt(x.ts)}</time>` +
    `<span class="ses" title="${esc(x.event)}">${esc(x.session)}</span>` +
    `${esc(x.text)} <small>— ${esc(x.actor)} · ${esc(x.event)}</small></div>`).join('')
    : '<div class="empty">Действий пока нет.</div>';
  $('#more').hidden = !hasMore;
}

async function get(qs) {
  const r = await fetch('/api/audit?limit=' + PAGE + qs);
  if (!r.ok) throw new Error('Ошибка ' + r.status);
  return r.json();
}

// свежие записи: добавляем сверху только то, чего ещё нет
async function refresh() {
  const maxId = rows.length ? rows[0].id : 0;
  const fresh = (await get(evQuery())).filter(x => x.id > maxId);
  if (!rows.length) hasMore = fresh.length === PAGE;
  rows = fresh.concat(rows);
  render();
}

async function more() {
  const older = await get('&before=' + rows[rows.length - 1].id + evQuery());
  rows = rows.concat(older);
  hasMore = older.length === PAGE;
  render();
}

async function reset() { rows = []; hasMore = false; await refresh(); }

(async () => {
  try {
    const evs = await (await fetch('/api/events')).json();
    $('#evFilter').insertAdjacentHTML('beforeend', evs.map(e => `<option value="${e.id}">${esc(e.name)}</option>`).join(''));
    await reset();
  } catch (e) { $('#glog').innerHTML = '<div class="empty">Не удалось загрузить журнал.</div>'; }
})();

$('#evFilter').onchange = () => reset().catch(() => { });
$('#reload').onclick = () => refresh().catch(() => { });
$('#more').onclick = () => more().catch(() => { });
setInterval(() => { if (!document.hidden) refresh().catch(() => { }); }, 10000);

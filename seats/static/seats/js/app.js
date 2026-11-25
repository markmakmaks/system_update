/* LibraryOS front end.
 *
 * Seat data lives in the Django database. This script renders it, sends every
 * action to the JSON API (see seats/urls.py) and re-renders from the fresh
 * state the server returns. It also polls the server so several browsers stay in sync.
 */

const CONFIG = JSON.parse(document.getElementById('app-config').textContent);
const CSRF_TOKEN = document.querySelector('meta[name="csrf-token"]').content;

let state = JSON.parse(document.getElementById('initial-state').textContent);
let ZONES = state.zones;
let seats = state.seats;
let USERS = state.users;

let currentRole = state.role;
let selectedProfSeats = [];
let targetStudentSeatToBook = null;
let studentZoneFilter = 'all';
let lastNotifId = Math.max(0, ...((state.notifications || {}).items || []).map(n => n.id));

/* ------------------------------------------------------------------ helpers */
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

function seatUrl(name, seatId) {
  return CONFIG.urls[name].replace(CONFIG.seatPlaceholder, encodeURIComponent(seatId));
}

function findSeat(seatId) {
  return seats.find(s => s.id === seatId);
}

function resUrl(name, id) {
  return CONFIG.urls[name].replace(CONFIG.idPlaceholder, encodeURIComponent(id));
}

// the student's one pending / accepted / checked-in reservation, if any
function currentStudentBooking() {
  return (state.myReservations || []).find(r => r.role === 'student');
}

// the server clock, so countdowns do not depend on the browser clock being right
let clockOffset = Date.parse(CONFIG.serverTime) - Date.now();
function serverNow() { return Date.now() + clockOffset; }

function fmtStamp(iso) {
  return new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
function fmtClock(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function applyState(newState) {
  state = newState;
  ZONES = newState.zones;
  seats = newState.seats;
  USERS = newState.users;
  handleNewNotifications();
  // drop professor selections that were taken by someone else in the meantime
  selectedProfSeats = selectedProfSeats.filter(id => {
    const s = findSeat(id);
    return s && s.status !== 'maintenance';
  });
}

async function callApi(url, payload = {}) {
  let res;
  let data = {};
  try {
    res = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    showToast('Could not reach the server. Please try again.', 'warning');
    return null;
  }
  try { data = await res.json(); } catch (err) { /* non-JSON error page */ }

  if (!res.ok || !data.ok) {
    showToast(data.error || `Request failed (${res.status}). Try reloading the page.`, 'warning');
    await refreshState();
    return { ok: false, ...data };
  }
  applyState(data.state);
  renderAllViews();
  if (data.message) showToast(data.message, data.level || 'info');
  return data;
}

async function refreshState() {
  try {
    const res = await fetch(CONFIG.urls.live, { credentials: 'same-origin', cache: 'no-store' });
    if (res.status === 401) { window.location.reload(); return; }   // signed out elsewhere
    const data = await res.json();
    if (data.serverTime) clockOffset = Date.parse(data.serverTime) - Date.now();
    if (data.ok && JSON.stringify(data.state) !== JSON.stringify(state)) {
      applyState(data.state);
      renderAllViews();
    }
    refreshLiveReport();
  } catch (err) { /* offline: keep showing the last known state */ }
}

/* --------------------------------------------------------------------- init */
document.addEventListener('DOMContentLoaded', () => {
  updateTimeDisplay();
  setInterval(updateTimeDisplay, 1000);
  setInterval(tickCountdowns, 1000);
  setRoleUI(currentRole);
  applyHash();
  window.addEventListener('hashchange', applyHash);
  setInterval(() => { if (!document.hidden) refreshState(); }, CONFIG.pollMs);
});

function updateTimeDisplay() {
  const now = new Date();
  document.getElementById('current-date-display').innerText = now.toLocaleDateString('en-US', {
    weekday: 'long', month: 'short', day: 'numeric', year: 'numeric'
  });
  document.getElementById('current-time-display').innerText = now.toLocaleTimeString('en-US', {
    hour12: false
  });
}

/* ------------------------------------------------------------ roles & tabs */
function setRoleUI(role) {
  currentRole = role;
  const u = USERS[role];
  document.getElementById('user-name').innerText = u.name;
  document.getElementById('user-role-badge').innerText = u.roleText;
  document.getElementById('user-avatar').innerText = u.avatar;
  document.getElementById('user-subtitle').innerText = u.sub;

  document.getElementById('view-admin').classList.toggle('hidden', role !== 'admin');
  document.getElementById('view-professor').classList.toggle('hidden', role !== 'professor');
  document.getElementById('view-student').classList.toggle('hidden', role !== 'student');

  renderAllViews();
}

function switchTab(role, tabName) {
  if (role === 'admin') {
    ['overview', 'seatmap', 'requests', 'reservations', 'reserve', 'analytics', 'forecast'].forEach(t => {
      document.getElementById(`admin-panel-${t}`).classList.toggle('hidden', t !== tabName);
      const btn = document.getElementById(`admin-tab-${t}`);
      btn.className = t === tabName
        ? "pb-3 tab-active flex items-center space-x-2 whitespace-nowrap"
        : "pb-3 text-slate-500 hover:text-slate-700 flex items-center space-x-2 whitespace-nowrap";
    });
    if (tabName === 'analytics' && !reportLoaded) loadDateReport();
    if (tabName === 'forecast') loadForecast();
  } else if (role === 'prof') {
    ['reserve', 'sessions'].forEach(t => {
      document.getElementById(`prof-panel-${t}`).classList.toggle('hidden', t !== tabName);
      const btn = document.getElementById(`prof-tab-${t}`);
      btn.className = t === tabName
        ? "pb-3 tab-active flex items-center space-x-2"
        : "pb-3 text-slate-500 hover:text-slate-700 flex items-center space-x-2";
    });
  } else if (role === 'student') {
    ['find', 'bookings'].forEach(t => {
      document.getElementById(`student-panel-${t}`).classList.toggle('hidden', t !== tabName);
      const btn = document.getElementById(`student-tab-${t}`);
      btn.className = t === tabName
        ? "pb-3 tab-active flex items-center space-x-2"
        : "pb-3 text-slate-500 hover:text-slate-700 flex items-center space-x-2";
    });
  }
}

/* ---------------------------------------------------------------- rendering */
function renderAllViews() {
  updateKPIs();
  renderAdminAlerts();
  renderAdminRequests();
  renderAdminZoneSummaryBars();
  renderAdminSeatMap();
  renderAdminReservationsTable();
  renderAdminAnalytics();
  renderProfSeatMap();
  renderProfSessions();
  renderStudentSeatMap(studentZoneFilter);
  renderStudentActiveBookings();
  renderNotifications();
  updateBanner();
  tickCountdowns();
  refreshSeatSchedule();
}

function updateKPIs() {
  const total = seats.length;
  const avail = seats.filter(s => s.status === 'available').length;
  const occ = seats.filter(s => s.status === 'occupied').length;
  const res = seats.filter(s => s.status === 'reserved').length;
  const pct = n => total ? Math.round((n / total) * 100) : 0;

  const globalAvail = document.getElementById('global-available-count');
  const globalTotal = document.getElementById('global-total-count');
  if (globalAvail) globalAvail.innerText = avail;
  if (globalTotal) globalTotal.innerText = total;
  document.getElementById('admin-kpi-available').innerText = avail;
  document.getElementById('admin-kpi-occupied').innerText = occ;
  document.getElementById('admin-kpi-reserved').innerText = res;
  document.getElementById('admin-kpi-avail-percent').innerText = `${pct(avail)}% Free Capacity`;
  document.getElementById('admin-kpi-occ-percent').innerText = `${pct(occ)}% Real-time Occupancy`;
}

function renderAdminAlerts() {
  const container = document.getElementById('admin-alerts');
  const noShows = seats.filter(s => s.noShow);
  const hogged = seats.filter(s => s.unattended).map(s => `
    <div class="p-3 bg-sky-50 border border-sky-200 rounded-lg text-sky-900 flex items-start space-x-2.5">
      <i class="fa-solid fa-bag-shopping text-sky-600 text-base mt-0.5"></i>
      <div>
        <span class="font-bold">Unattended Belongings (${esc(s.id)}):</span>
        <p class="text-sky-800 mt-0.5">Items are on this seat with nobody present. Please check the seat.</p>
      </div>
    </div>`).join('');
  if (noShows.length === 0 && hogged) {
    container.innerHTML = hogged;
    return;
  }
  if (noShows.length === 0) {
    container.innerHTML = `
      <div class="p-3 bg-emerald-50 border border-emerald-200 rounded-lg text-emerald-900 flex items-start space-x-2.5">
        <i class="fa-solid fa-circle-check text-emerald-600 text-base mt-0.5"></i>
        <div><span class="font-bold">All clear:</span>
          <p class="text-emerald-800 mt-0.5">No expired reservations are waiting for release.</p></div>
      </div>`;
    return;
  }
  container.innerHTML = hogged + noShows.map(s => `
    <div class="p-3 bg-amber-50 border border-amber-200 rounded-lg text-amber-900 flex items-start space-x-2.5">
      <i class="fa-solid fa-triangle-exclamation text-amber-600 text-base mt-0.5"></i>
      <div>
        <span class="font-bold">No-Show Alert (${esc(s.zone)}-${esc(s.number)}):</span>
        <p class="text-amber-800 mt-0.5">${esc(s.user)}'s reservation expired without check-in. Auto-release ready.</p>
        <button onclick="forceReleaseSeat('${esc(s.id)}')" class="mt-2 bg-amber-600 text-white px-2.5 py-1 rounded text-[11px] font-semibold hover:bg-amber-700">Release Seat ${esc(s.id)}</button>
      </div>
    </div>`).join('');
}

function renderAdminZoneSummaryBars() {
  const container = document.getElementById('admin-zone-summary-bars');
  container.innerHTML = '';
  ZONES.forEach(z => {
    const zSeats = seats.filter(s => s.zone === z.id);
    const freeCount = zSeats.filter(s => s.status === 'available').length;
    const occCount = zSeats.length - freeCount;
    const pctFree = zSeats.length ? Math.round((freeCount / zSeats.length) * 100) : 0;

    container.innerHTML += `
      <div class="space-y-1">
        <div class="flex justify-between text-xs">
          <span class="font-bold text-slate-700">${esc(z.name)}</span>
          <span class="text-slate-500">${freeCount} available / ${occCount} in use</span>
        </div>
        <div class="w-full bg-slate-100 h-3 rounded-full overflow-hidden flex border border-slate-200">
          <div class="bg-emerald-500 h-full" style="width: ${pctFree}%" title="Available"></div>
          <div class="bg-rose-500 h-full" style="width: ${100 - pctFree}%" title="Occupied/Reserved"></div>
        </div>
      </div>
    `;
  });
}

function renderAdminSeatMap() {
  const container = document.getElementById('admin-seatmap-grid');
  container.innerHTML = '';
  ZONES.forEach(z => {
    const zSeats = seats.filter(s => s.zone === z.id);
    let seatsHTML = '';
    zSeats.forEach(s => {
      let colorClass = 'bg-emerald-50 border-emerald-300 text-emerald-800 hover:bg-emerald-100';
      if (s.status === 'occupied') colorClass = 'bg-rose-500 border-rose-600 text-white';
      if (s.status === 'reserved') colorClass = 'bg-amber-400 border-amber-500 text-navy-900 font-bold';
      if (s.status === 'maintenance') colorClass = 'bg-slate-400 border-slate-500 text-white';
      if (s.status === 'pending') colorClass = 'bg-amber-50 border-amber-400 border-dashed text-amber-800 font-bold';

      seatsHTML += `
        <button onclick="openAdminSeatOverride('${esc(s.id)}')" class="seat-btn relative p-2 text-center rounded-lg border text-xs font-mono font-bold transition-all shadow-sm ${colorClass}" title="${esc(s.id)}: ${esc(s.status)}${s.queueCount ? ` - ${s.queueCount} queued today` : ''}">
          ${esc(s.id)}${queueBadge(s)}
        </button>
      `;
    });

    container.innerHTML += `
      <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm space-y-3">
        <div class="flex justify-between items-center border-b border-slate-100 pb-2">
          <h4 class="font-bold text-xs text-navy-900">${esc(z.name)}</h4>
          <span class="text-[11px] text-slate-500 font-medium">${zSeats.filter(s => s.status === 'available').length}/${z.total} Available</span>
        </div>
        <div class="grid grid-cols-6 gap-2">
          ${seatsHTML}
        </div>
      </div>
    `;
  });
}

function renderAdminReservationsTable() {
  const tbody = document.getElementById('admin-reservations-tbody');
  tbody.innerHTML = '';
  const rows = seats.filter(s => s.status !== 'available');

  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="p-4 text-center text-slate-400">No active reservations recorded.</td></tr>`;
    return;
  }

  const BADGES = {
    pending: ['PENDING', 'bg-sky-100 text-sky-800'],
    reserved: ['UPCOMING', 'bg-amber-100 text-amber-800'],
    occupied: ['ACTIVE', 'bg-emerald-100 text-emerald-800'],
    maintenance: ['BLOCKED', 'bg-slate-200 text-slate-800'],
  };
  const btn = (label, onclick, cls) =>
    `<button onclick="${onclick}" class="${cls} px-2 py-1 rounded text-[10px] font-semibold">${label}</button>`;
  const GREEN = 'bg-emerald-50 text-emerald-700 hover:bg-emerald-100 border border-emerald-200';
  const RED = 'bg-rose-50 text-rose-700 hover:bg-rose-100 border border-rose-200';
  const GREY = 'bg-slate-100 text-slate-700 hover:bg-slate-200 border border-slate-200';

  rows.forEach(s => {
    const [label, cls] = BADGES[s.status] || ['—', 'bg-slate-100 text-slate-700'];
    let actions = '';
    if (s.status === 'pending') {
      actions = btn('Accept', `acceptReservation(${s.reservationId})`, GREEN)
        + btn('Reject', `rejectReservation(${s.reservationId})`, RED)
        + btn('Block', `toggleMaintenance('${esc(s.id)}')`, GREY);
    } else if (s.status === 'maintenance') {
      actions = btn('Unblock', `toggleMaintenance('${esc(s.id)}')`, GREY);
    } else {
      actions = btn('Force Release', `forceReleaseSeat('${esc(s.id)}')`, RED)
        + btn('Block', `toggleMaintenance('${esc(s.id)}')`, GREY);
    }
    tbody.innerHTML += `
      <tr class="hover:bg-slate-50 border-b border-slate-100">
        <td class="p-3 font-mono font-bold text-slate-600">${s.reservationId ? `#RES-${s.reservationId}` : `#${esc(s.id)}`}</td>
        <td class="p-3 font-bold text-navy-900">${esc(s.id)} <span class="text-slate-400 font-normal">(${esc(s.zone)})</span></td>
        <td class="p-3 font-medium text-slate-800">${esc(s.user || 'N/A')}</td>
        <td class="p-3 uppercase text-[10px] font-bold text-slate-500">${esc(s.role || 'System')}</td>
        <td class="p-3 text-slate-600">${esc(s.time || 'N/A')}</td>
        <td class="p-3"><span class="${cls} px-2 py-0.5 rounded-full text-[10px] font-bold">${label}</span></td>
        <td class="p-3 text-right space-x-1 whitespace-nowrap">${actions}</td>
      </tr>
    `;
  });
  filterAdminReservations();   // keep any search/filter the admin typed
}

/* --------------------------------------------------------- analytics report */
function anGet(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? o : o[k]), obj);
}

function anBars(items, valueOf, colour) {
  return items.map(i => `
    <div class="space-y-1">
      <div class="flex justify-between text-xs">
        <span class="font-semibold text-slate-700">${esc(i.label)}</span>
        <span class="text-slate-500">${esc(i.detail)}</span>
      </div>
      <div class="h-2.5 bg-slate-100 rounded-full overflow-hidden">
        <div class="h-full ${i.colour || colour} transition-all duration-500" style="width: ${Math.min(100, valueOf(i))}%"></div>
      </div>
    </div>`).join('');
}

function renderAdminAnalytics() {
  const a = state.analytics;
  if (!a) return;

  document.querySelectorAll('[data-an]').forEach(el => {
    const v = anGet(a, el.dataset.an);
    if (v !== undefined) el.textContent = v;
  });
  document.querySelectorAll('[data-an-bar]').forEach(el => {
    const v = anGet(a, el.dataset.anBar);
    if (v !== undefined) el.style.width = `${Math.min(100, v)}%`;
  });

  document.getElementById('an-zones').innerHTML = a.zones.length ? a.zones.map(z => `
    <div class="space-y-1">
      <div class="flex justify-between text-xs">
        <span class="font-semibold text-slate-700">${esc(z.name)}</span>
        <span class="text-slate-500">${z.occupied.count} occupied (${z.occupied.pct}%) &bull; ${z.reserved.count} reserved (${z.reserved.pct}%) &bull; ${z.utilization}% used</span>
      </div>
      <div class="h-2.5 bg-slate-100 rounded-full overflow-hidden flex">
        <div class="h-full bg-rose-500 transition-all duration-500" style="width: ${z.occupied.pct}%"></div>
        <div class="h-full bg-amber-400 transition-all duration-500" style="width: ${z.reserved.pct}%"></div>
      </div>
    </div>`).join('') : '<p class="text-xs text-slate-400">No zones yet.</p>';

  const st = a.reservations.status;
  document.getElementById('an-status-rows').innerHTML = anBars([
    { label: 'Awaiting approval', detail: `${st.awaitingApproval.count} • ${st.awaitingApproval.pct}%`, v: st.awaitingApproval.pct, colour: 'bg-sky-400' },
    { label: 'Accepted, waiting for check-in', detail: `${st.pending.count} • ${st.pending.pct}%`, v: st.pending.pct, colour: 'bg-amber-400' },
    { label: 'In session', detail: `${st.inSession.count} • ${st.inSession.pct}%`, v: st.inSession.pct, colour: 'bg-emerald-500' },
    { label: 'Completed (checked in)', detail: `${st.completed.count} • ${st.completed.pct}%`, v: st.completed.pct, colour: 'bg-royal-500' },
    { label: 'Cancelled before check-in', detail: `${st.cancelled.count} • ${st.cancelled.pct}%`, v: st.cancelled.pct, colour: 'bg-slate-400' },
    { label: 'Expired (no-show)', detail: `${st.expired.count} • ${st.expired.pct}%`, v: st.expired.pct, colour: 'bg-rose-500' },
    { label: 'Rejected', detail: `${st.rejected.count} • ${st.rejected.pct}%`, v: st.rejected.pct, colour: 'bg-slate-300' },
  ], i => i.v);

  document.getElementById('an-roles').innerHTML = anBars(
    a.roles.map(r => ({ label: r.label, detail: `${r.count} • ${r.pct}%`, v: r.pct })),
    i => i.v, 'bg-royal-500');

  document.getElementById('an-top-seats').innerHTML = a.topSeats.length ? a.topSeats.map(t => `
    <tr>
      <td class="p-3 font-bold text-navy-900">${esc(t.seat)} <span class="text-slate-400 font-normal">(${esc(t.zone)})</span></td>
      <td class="p-3">${t.count}</td>
      <td class="p-3">${t.pct}%</td>
      <td class="p-3 text-slate-600">${Math.floor(t.minutes / 60)}h ${t.minutes % 60}m</td>
    </tr>`).join('') : '<tr><td colspan="4" class="p-4 text-center text-slate-400">No sessions recorded yet.</td></tr>';

  const maxHour = Math.max(1, ...a.hours.reservations, ...a.hours.occupancy);
  const bar = (n, cls) => `<div class="flex-1 ${cls} rounded-t" style="height: ${Math.round(n / maxHour * 100)}%"></div>`;
  document.getElementById('an-hours').innerHTML = a.hours.reservations.map((n, h) => `
    <div class="flex-1 h-full flex items-end gap-px" title="${String(h).padStart(2, '0')}:00 — ${n} reservations, ${a.hours.occupancy[h]} check-ins/walk-ins">
      ${bar(n, 'bg-amber-400')}${bar(a.hours.occupancy[h], 'bg-royal-500')}
    </div>`).join('');
  const peakText = peaks => peaks.length ? peaks.map(p => `${p.label} (${p.count})`).join(', ') : 'no data yet';
  document.getElementById('an-peak-res').textContent = peakText(a.hours.peakReservations);
  document.getElementById('an-peak-occ').textContent = peakText(a.hours.peakOccupancy);
}

function filterAdminReservations() {
  const query = document.getElementById('admin-search-input').value.toLowerCase();
  const filter = document.getElementById('admin-status-filter').value;
  const rows = document.querySelectorAll('#admin-reservations-tbody tr');

  rows.forEach(row => {
    const text = row.innerText.toLowerCase();
    const matchesQuery = text.includes(query);
    const matchesFilter = filter === 'all' || text.includes(filter);
    row.style.display = (matchesQuery && matchesFilter) ? '' : 'none';
  });
}

/* --------------------------------------------------------------- professor */
function renderProfSeatMap() {
  // professors and admins share the multi-select reservation grid
  const isAdmin = currentRole === 'admin';
  const container = document.getElementById(isAdmin ? 'admin-reserve-grid' : 'prof-seatmap-grid');
  container.innerHTML = '';
  ZONES.forEach(z => {
    const zSeats = seats.filter(s => s.zone === z.id);
    let seatsHTML = '';
    zSeats.forEach(s => {
      const isSelected = selectedProfSeats.includes(s.id);
      const blocked = s.status === 'maintenance';
      let colorClass = 'bg-emerald-50 border-emerald-300 text-emerald-800 hover:bg-emerald-100';
      if (s.status === 'occupied' || s.status === 'reserved') {
        colorClass = 'bg-amber-50 border-amber-300 text-amber-800 hover:bg-amber-100';   // busy now, a later slot may be free
      }
      if (blocked) {
        colorClass = 'bg-slate-100 border-slate-200 text-slate-400 cursor-not-allowed';
      }
      if (isSelected) {
        colorClass = 'bg-royal-600 border-royal-700 text-white font-bold ring-2 ring-royal-300';
      }

      seatsHTML += `
        <button onclick="toggleProfSeatSelection('${esc(s.id)}')" ${blocked ? 'disabled' : ''} class="seat-btn relative p-2 text-center rounded-lg border text-xs font-mono font-bold transition-all ${colorClass}" title="${esc(s.id)}: ${esc(s.status)}${s.queueCount ? ` - ${s.queueCount} queued today` : ''}">
          ${esc(s.id)}${queueBadge(s)}
        </button>
      `;
    });

    container.innerHTML += `
      <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm space-y-3">
        <div class="flex justify-between items-center border-b border-slate-100 pb-2">
          <h4 class="font-bold text-xs text-navy-900">${esc(z.name)}</h4>
          <span class="text-[11px] text-slate-500 font-medium">${zSeats.filter(s => s.status === 'available').length}/${z.total} Available</span>
        </div>
        <div class="grid grid-cols-6 gap-2">
          ${seatsHTML}
        </div>
      </div>
    `;
  });
  document.getElementById(isAdmin ? 'admin-selected-count' : 'prof-selected-count').innerText = selectedProfSeats.length;
}

function toggleProfSeatSelection(seatId) {
  const idx = selectedProfSeats.indexOf(seatId);
  if (idx > -1) selectedProfSeats.splice(idx, 1);
  else selectedProfSeats.push(seatId);
  renderProfSeatMap();
}

function clearProfSelections() {
  selectedProfSeats = [];
  renderProfSeatMap();
}

function openBatchModal() {
  if (selectedProfSeats.length === 0) {
    showToast('Please select at least one seat first.', 'warning');
    return;
  }
  document.getElementById('prof-batch-count-text').innerText = selectedProfSeats.length;
  const container = document.getElementById('prof-batch-seats-list');
  container.innerHTML = selectedProfSeats.map(s => `<button type="button" onclick="openSeatSchedule('${esc(s)}')" title="View this seat's queue" class="bg-royal-100 text-royal-800 text-xs font-bold px-2 py-0.5 rounded border border-royal-200 hover:bg-royal-200">${esc(s)} <i class="fa-regular fa-clock text-[10px]"></i></button>`).join('');
  initSlotInputs('prof-res', batchMaxMinutes(), 120);
  showSlotConflict('prof-slot-conflict', '');
  updateSlotHint('prof');
  openModal('modal-prof-batch');
}

function batchMaxMinutes() {
  return currentRole === 'admin' ? CONFIG.slots.adminMaxMinutes : CONFIG.slots.professorMaxMinutes;
}

async function confirmProfBatchBooking() {
  const titleInput = document.getElementById('prof-session-title-input');
  const slot = readSlot('prof-res');
  const problem = slotProblem(slot, batchMaxMinutes());
  if (problem) { showSlotConflict('prof-slot-conflict', problem); return; }
  const res = await callApi(CONFIG.urls.batchReserve, {
    seat_ids: selectedProfSeats,
    title: titleInput.value.trim(),
    ...slot,
  });
  if (res && res.ok) {
    selectedProfSeats = [];
    titleInput.value = '';
    closeModal('modal-prof-batch');
    renderAllViews();
  } else if (res && res.code === 'slot_conflict') {
    showSlotConflict('prof-slot-conflict', res.error);
  }
}

function renderProfSessions() {
  const container = document.getElementById('prof-sessions-container');
  if (currentRole !== 'professor') return;
  const groups = groupReservations((state.myReservations || []).filter(r => r.role === 'professor'));
  document.getElementById('prof-session-count').innerText = groups.length;

  if (groups.length === 0) {
    container.innerHTML = `
      <div class="bg-white p-8 rounded-xl border border-slate-200 text-center space-y-2">
        <i class="fa-solid fa-folder-open text-slate-300 text-3xl"></i>
        <p class="text-xs text-slate-500">You currently have no faculty seat requests or reservations.</p>
      </div>
    `;
    return;
  }
  container.innerHTML = groups.map(g => g.batchId ? groupCardHTML(g) : reservationCardHTML(g.items[0])).join('');
}

/* ----------------------------------------------------------------- student */
function renderStudentSeatMap(filterZone) {
  const container = document.getElementById('student-seatmap-grid');
  container.innerHTML = '';
  const zonesToDisplay = filterZone === 'all' ? ZONES : ZONES.filter(z => z.id === filterZone);

  zonesToDisplay.forEach(z => {
    const zSeats = seats.filter(s => s.zone === z.id);
    let seatsHTML = '';
    zSeats.forEach(s => {
      let colorClass = 'bg-emerald-50 border-emerald-300 text-emerald-800 hover:bg-emerald-100 cursor-pointer';
      let clickFn = `handleStudentSeatClick('${esc(s.id)}')`;

      // an occupied / reserved seat is only busy *now*: its later time slots can still be requested
      if (s.status === 'occupied') {
        colorClass = 'bg-rose-50 border-rose-300 text-rose-500 hover:bg-rose-100 cursor-pointer';
      } else if (s.status === 'reserved') {
        colorClass = 'bg-amber-100 border-amber-300 text-amber-800 hover:bg-amber-200 cursor-pointer';
      } else if (s.status === 'maintenance') {
        colorClass = 'bg-slate-100 border-slate-200 text-slate-400 cursor-not-allowed';
        clickFn = `showToast('Seat ${esc(s.id)} is under maintenance.', 'warning')`;
      }

      seatsHTML += `
        <button onclick="${clickFn}" class="seat-btn relative p-2 text-center rounded-lg border text-xs font-mono font-bold transition-all ${colorClass}" title="${esc(s.id)}: ${esc(s.status)}${s.queueCount ? ` - ${s.queueCount} booked today` : ''}">
          ${esc(s.id)}${queueBadge(s)}
        </button>
      `;
    });

    container.innerHTML += `
      <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm space-y-3">
        <div class="flex justify-between items-center border-b border-slate-100 pb-2">
          <h4 class="font-bold text-xs text-navy-900">${esc(z.name)}</h4>
          <span class="text-[11px] text-slate-500 font-medium">${zSeats.filter(s => s.status === 'available').length}/${z.total} Free</span>
        </div>
        <div class="grid grid-cols-6 gap-2">
          ${seatsHTML}
        </div>
      </div>
    `;
  });
}

function filterStudentZone(zoneId) {
  studentZoneFilter = zoneId;
  ['all', ...ZONES.map(z => z.id)].forEach(z => {
    const btn = document.getElementById(`zone-filter-${z}`);
    if (!btn) return;
    btn.className = z === zoneId
      ? "px-3 py-1.5 rounded-lg font-semibold bg-royal-600 text-white"
      : "px-3 py-1.5 rounded-lg font-semibold bg-slate-100 text-slate-600 hover:bg-slate-200";
  });
  renderStudentSeatMap(zoneId);
}

function handleStudentSeatClick(seatId) {
  const existing = currentStudentBooking();
  if (existing) {
    targetStudentSeatToBook = seatId;
    document.getElementById('single-rule-current-text').innerText = `Seat ${existing.seat} • ${existing.zoneName} (${existing.time})`;
    openModal('modal-single-rule');
    return;
  }
  openStudentBookingModal(seatId);
}

function openStudentBookingModal(seatId) {
  const s = findSeat(seatId);
  if (!s) return;
  targetStudentSeatToBook = seatId;
  document.getElementById('modal-seat-title').innerText = `Seat ${s.id}`;
  document.getElementById('modal-seat-zone').innerText = s.zoneName;
  document.getElementById('modal-seat-features').innerText = s.features;
  initSlotInputs('modal-res', CONFIG.slots.studentMaxMinutes, 60);
  showSlotConflict('modal-slot-conflict', '');
  updateSlotHint('student');
  loadModalQueue();
  openModal('modal-student-reserve');
}

async function confirmStudentBooking() {
  if (!targetStudentSeatToBook) return;
  const slot = readSlot('modal-res');
  const problem = slotProblem(slot, CONFIG.slots.studentMaxMinutes);
  if (problem) { showSlotConflict('modal-slot-conflict', problem); return; }
  const res = await callApi(seatUrl('reserve', targetStudentSeatToBook), slot);
  if (res && res.ok) {
    closeModal('modal-student-reserve');
    targetStudentSeatToBook = null;
    switchTab('student', 'bookings');
  } else if (res && res.code === 'slot_conflict') {
    showSlotConflict('modal-slot-conflict', res.error);
    loadModalQueue();
  } else {
    closeModal('modal-student-reserve');
  }
}

async function cancelStudentCurrentAndBookNew() {
  const existing = currentStudentBooking();
  if (existing) {
    const res = await callApi(resUrl('cancel', existing.id));
    if (!res || !res.ok) return;
  }
  closeModal('modal-single-rule');
  openStudentBookingModal(targetStudentSeatToBook);
}

function renderStudentActiveBookings() {
  const container = document.getElementById('student-active-booking-container');
  const booking = currentStudentBooking();

  document.getElementById('student-booking-badge').innerText = booking ? '1' : '0';

  if (!booking) {
    container.innerHTML = `
      <div class="bg-white p-8 rounded-xl border border-slate-200 text-center space-y-3">
        <i class="fa-solid fa-ticket text-slate-300 text-4xl"></i>
        <p class="text-sm font-semibold text-slate-700">No Active Seat Booking Found</p>
        <p class="text-xs text-slate-500 max-w-sm mx-auto">You do not have any pending, active or upcoming reservations. Select a seat on the floorplan to request one.</p>
        <button onclick="switchTab('student', 'find')" class="bg-royal-600 text-white text-xs font-bold px-4 py-2 rounded-xl hover:bg-royal-700">Browse Available Seats</button>
      </div>
    `;
    return;
  }
  container.innerHTML = reservationCardHTML(booking);
}

/* One reservation, as shown to a student or professor: status, confirmation code, grace countdown. */
const RES_BADGES = {
  pending: ['PENDING APPROVAL', 'bg-amber-100 text-amber-800'],
  reserved: ['ACCEPTED - CHECK IN NOW', 'bg-sky-100 text-sky-800'],
  active: ['CHECKED IN', 'bg-emerald-100 text-emerald-800'],
};

function reservationCardHTML(r) {
  let [label, badgeCls] = RES_BADGES[r.status] || [String(r.status).toUpperCase(), 'bg-slate-100 text-slate-700'];
  const upcoming = r.status === 'reserved' && r.live === false;
  if (upcoming) { label = 'APPROVED - UPCOMING'; badgeCls = 'bg-indigo-100 text-indigo-800'; }

  let codeBlock = '';
  if (r.status === 'pending') {
    codeBlock = `
      <div class="bg-amber-50 border border-dashed border-amber-300 rounded-xl p-4 text-center space-y-1">
        <span class="inline-block text-xs font-bold px-3 py-1 rounded-full bg-amber-100 text-amber-800 uppercase tracking-wider">Waiting to accept</span>
        <p class="text-[11px] text-amber-900">Your confirmation code appears here once the admin accepts your request.</p>
      </div>`;
  } else if (r.code && r.status !== 'active') {
    const hint = r.status === 'reserved'
      ? 'Show this code to the library admin to confirm your Time-In.'
      : 'Keep this code. You will show it to the admin once your request is accepted.';
    codeBlock = `
      <div class="bg-navy-900 text-white rounded-xl p-4 text-center space-y-1">
        <p class="text-[11px] uppercase tracking-wider text-sky-200">Your confirmation code</p>
        <p class="font-mono text-3xl font-bold tracking-widest select-all">${esc(r.code)}</p>
        <p class="text-[11px] text-sky-100/80">${hint}</p>
      </div>`;
  }

  let note = '';
  if (r.status === 'pending') {
    note = `<div class="bg-amber-50 border border-amber-200 rounded-xl p-3 text-xs text-amber-900">
      <i class="fa-solid fa-hourglass-half mr-1"></i>Waiting for the admin to accept your request. Once accepted, the 15-minute check-in window starts when your time slot starts.</div>`;
  } else if (upcoming) {
    note = `<div class="bg-indigo-50 border border-indigo-200 rounded-xl p-3 text-xs text-indigo-900">
      <i class="fa-solid fa-calendar-check mr-1"></i>Approved and queued. The seat is yours from ${esc(fmtClock(r.startAt))}; show your code to the admin within 15 minutes after the slot starts.</div>`;
  } else if (r.status === 'reserved' && r.graceExpiresAt) {
    note = `<div class="bg-sky-50 border border-sky-200 rounded-xl p-3 text-xs text-sky-900 flex flex-wrap items-center justify-between gap-2">
      <span><i class="fa-solid fa-stopwatch mr-1"></i>Check in with the admin before the grace period ends, or the seat is released.</span>
      <span class="text-sm">Time left: ${countdownHTML(r.graceExpiresAt)}</span></div>`;
  } else if (r.status === 'active' && r.checkedInAt) {
    note = `<div class="bg-emerald-50 border border-emerald-200 rounded-xl p-3 text-xs text-emerald-900">
      <i class="fa-solid fa-circle-check mr-1"></i>Time-in confirmed at ${esc(fmtClock(r.checkedInAt))}. Enjoy your session!</div>`;
  }

  const cancelLabel = r.status === 'active' ? 'End Session & Check-Out' : r.status === 'pending' ? 'Cancel Request' : 'Cancel Reservation';
  return `
    <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm space-y-4">
      <div class="flex flex-wrap justify-between items-start border-b border-slate-100 pb-4 gap-2">
        <div>
          <span class="${badgeCls} text-xs font-bold px-2.5 py-1 rounded-full uppercase tracking-wider">${label}</span>
          <h3 class="text-2xl font-bold text-navy-900 mt-2">Seat ${esc(r.seat)}</h3>
          <p class="text-xs text-slate-500">${esc(r.zoneName)}</p>
        </div>
        <div class="text-right">
          <span class="text-xs text-slate-400 block">Session Time:</span>
          <span class="text-sm font-bold text-slate-700">${esc(r.time)}</span>
          <span class="text-[11px] text-slate-400 block">Requested ${esc(fmtStamp(r.requestedAt))}</span>
        </div>
      </div>
      ${codeBlock}
      ${note}
      <button onclick="cancelReservation(${r.id})" class="w-full bg-rose-50 text-rose-700 border border-rose-200 hover:bg-rose-100 font-bold py-2.5 rounded-xl text-xs flex items-center justify-center space-x-1.5">
        <i class="fa-solid fa-circle-xmark"></i><span>${cancelLabel}</span>
      </button>
    </div>
  `;
}

/* ---------------------------------------------------- group (batch) bookings */
function groupReservations(list) {
  const groups = [];
  const byBatch = {};
  list.forEach(r => {
    if (!r.batchId) { groups.push({ key: `r${r.id}`, batchId: '', items: [r] }); return; }
    if (!byBatch[r.batchId]) {
      byBatch[r.batchId] = { key: r.batchId, batchId: r.batchId, items: [] };
      groups.push(byBatch[r.batchId]);
    }
    byBatch[r.batchId].items.push(r);
  });
  return groups;
}

const STATUS_CHIP = {
  pending: 'bg-amber-100 text-amber-800 border-amber-200',
  reserved: 'bg-sky-100 text-sky-800 border-sky-200',
  active: 'bg-emerald-100 text-emerald-800 border-emerald-200',
};
const STATUS_LABEL = { pending: 'Waiting to accept', reserved: 'Accepted', active: 'Checked in' };

function groupCode(g) {
  const withCode = g.items.find(i => i.code);
  return withCode ? withCode.code : '';
}

function groupHeadline(g) {
  const has = st => g.items.some(i => i.status === st);
  if (has('reserved')) {
    const live = g.items.some(i => i.status === 'reserved' && i.live !== false);
    return live ? ['ACCEPTED - CHECK IN NOW', 'bg-sky-100 text-sky-800'] : ['APPROVED - UPCOMING', 'bg-indigo-100 text-indigo-800'];
  }
  if (has('active') && !has('pending')) return ['CHECKED IN', 'bg-emerald-100 text-emerald-800'];
  if (has('pending') && !has('active')) return ['PENDING APPROVAL', 'bg-amber-100 text-amber-800'];
  return ['IN PROGRESS', 'bg-slate-100 text-slate-700'];
}

function seatChips(g) {
  return g.items.map(i =>
    `<span class="font-mono text-xs font-bold px-2 py-0.5 rounded border ${STATUS_CHIP[i.status] || ''}">${esc(i.seat)}</span>`).join(' ');
}

// the professor's single card for a whole group: one code, click to see every seat
function groupCardHTML(g) {
  const [label, badgeCls] = groupHeadline(g);
  const code = groupCode(g);
  const pending = g.items.every(i => i.status === 'pending');
  const reserved = g.items.find(i => i.status === 'reserved');
  const first = g.items[0];

  const codeBlock = pending
    ? `<div class="bg-amber-50 border border-dashed border-amber-300 rounded-xl p-4 text-center space-y-1">
         <span class="inline-block text-xs font-bold px-3 py-1 rounded-full bg-amber-100 text-amber-800 uppercase tracking-wider">Waiting to accept</span>
         <p class="text-[11px] text-amber-900">Your group confirmation code appears here once the admin accepts the request.</p>
       </div>`
    : (code && !g.items.every(i => i.status === 'active')
        ? `<div class="bg-navy-900 text-white rounded-xl p-4 text-center space-y-1">
             <p class="text-[11px] uppercase tracking-wider text-sky-200">Group confirmation code</p>
             <p class="font-mono text-3xl font-bold tracking-widest select-all">${esc(code)}</p>
             <p class="text-[11px] text-sky-100/80">One code for all ${g.items.length} seats. Show it to the library admin to confirm your Time-In.</p>
           </div>`
        : '');
  const timer = reserved && reserved.live === false
    ? `<div class="bg-indigo-50 border border-indigo-200 rounded-xl p-3 text-xs text-indigo-900"><i class="fa-solid fa-calendar-check mr-1"></i>Approved. Your seats are yours from ${esc(fmtClock(reserved.startAt))}; check in within 15 minutes after that.</div>`
    : reserved && reserved.graceExpiresAt
    ? `<div class="bg-sky-50 border border-sky-200 rounded-xl p-3 text-xs text-sky-900 flex flex-wrap items-center justify-between gap-2">
         <span><i class="fa-solid fa-stopwatch mr-1"></i>Check in before the grace period ends, or the seats are released.</span>
         <span class="text-sm">Time left: ${countdownHTML(reserved.graceExpiresAt)}</span></div>`
    : '';

  return `
    <div class="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm space-y-4">
      <div class="cursor-pointer space-y-3" onclick="openGroupModal('${esc(g.key)}', 'mine')" title="Click to see every seat in this group">
        <div class="flex flex-wrap justify-between items-start gap-2">
          <div>
            <span class="${badgeCls} text-xs font-bold px-2.5 py-1 rounded-full uppercase tracking-wider">${label}</span>
            <h3 class="text-xl font-bold text-navy-900 mt-2">${g.items.length} seat${g.items.length > 1 ? 's' : ''} &bull; ${esc(first.title || 'Group booking')}</h3>
            <p class="text-xs text-slate-500">${esc(first.time)}</p>
          </div>
          <span class="text-xs font-semibold text-royal-600"><i class="fa-solid fa-list mr-1"></i>View seats</span>
        </div>
        <div class="flex flex-wrap gap-1.5">${seatChips(g)}</div>
      </div>
      ${codeBlock}
      ${timer}
      <button onclick="cancelReservation(${first.id})" class="w-full bg-rose-50 text-rose-700 border border-rose-200 hover:bg-rose-100 font-bold py-2.5 rounded-xl text-xs flex items-center justify-center space-x-1.5">
        <i class="fa-solid fa-circle-xmark"></i><span>${pending ? 'Cancel Request (all seats)' : 'Cancel / End Whole Group'}</span>
      </button>
    </div>`;
}

let openGroup = null;   // { key, mode } of the group modal currently showing

function groupSources(mode) {
  if (mode === 'mine') return (state.myReservations || []).filter(r => r.role === 'professor');
  return [...(state.pendingRequests || []), ...(state.awaiting || [])];
}

function openGroupModal(key, mode) {
  openGroup = { key, mode };
  renderGroupModal();
  openModal('modal-group');
}

function refreshGroupModal() {
  if (openGroup && !document.getElementById('modal-group').classList.contains('hidden')) renderGroupModal();
}

function renderGroupModal() {
  const g = groupReservations(groupSources(openGroup.mode)).find(x => x.key === openGroup.key);
  if (!g) { closeModal('modal-group'); openGroup = null; return; }
  const mine = openGroup.mode === 'mine';
  const first = g.items[0];
  const code = mine ? groupCode(g) : '';
  const reserved = g.items.find(i => i.status === 'reserved');

  document.getElementById('group-modal-title').textContent = `${g.items.length} seat${g.items.length > 1 ? 's' : ''} booked`;
  document.getElementById('group-modal-sub').textContent = mine ? (first.title || 'Group booking') : `${first.user} • ${first.title || 'Group booking'}`;

  const codeBlock = !mine ? '' : (code
    ? `<div class="bg-navy-900 text-white rounded-xl p-3 text-center">
         <p class="text-[11px] uppercase tracking-wider text-sky-200">Group confirmation code</p>
         <p class="font-mono text-2xl font-bold tracking-widest select-all">${esc(code)}</p></div>`
    : `<div class="bg-amber-50 border border-dashed border-amber-300 rounded-xl p-3 text-center">
         <span class="inline-block text-xs font-bold px-3 py-1 rounded-full bg-amber-100 text-amber-800 uppercase tracking-wider">Waiting to accept</span></div>`);
  const timer = reserved && reserved.live === false
    ? `<p class="text-xs text-indigo-900 bg-indigo-50 border border-indigo-200 rounded-lg p-2">Starts at ${esc(fmtClock(reserved.startAt))}</p>`
    : reserved && reserved.graceExpiresAt
    ? `<p class="text-xs text-sky-900 bg-sky-50 border border-sky-200 rounded-lg p-2">Time left to check in: ${countdownHTML(reserved.graceExpiresAt)}</p>` : '';

  const rows = g.items.map(i => {
    const action = mine
      ? `<button onclick="cancelReservation(${i.id}, true)" class="text-[11px] font-semibold text-rose-600 hover:underline">Release this seat</button>`
      : (i.status === 'pending' ? '' : `<button onclick="releaseReservationById(${i.id})" class="text-[11px] font-semibold text-rose-600 hover:underline">Force release</button>`);
    return `
      <div class="flex items-center justify-between gap-3 p-3 border border-slate-200 rounded-lg">
        <div>
          <span class="font-mono font-bold text-navy-900">${esc(i.seat)}</span>
          <span class="text-xs text-slate-500">${esc(i.zoneName)}</span>
          <div class="text-[11px] text-slate-400">${esc(i.time)}</div>
        </div>
        <div class="flex items-center space-x-3">
          <span class="text-[10px] font-bold px-2 py-0.5 rounded-full border ${STATUS_CHIP[i.status] || ''}">${esc(STATUS_LABEL[i.status] || i.status)}</span>
          ${action}
        </div>
      </div>`;
  }).join('');

  const footer = mine
    ? `<button onclick="cancelReservation(${first.id})" class="w-full bg-rose-50 text-rose-700 border border-rose-200 hover:bg-rose-100 font-bold py-2 rounded-xl text-xs">Cancel / end the whole group</button>`
    : '';
  document.getElementById('group-modal-body').innerHTML = codeBlock + timer + rows + footer;
  tickCountdowns();
}

async function forceReleaseGroup(reservationIds) {
  for (const id of reservationIds) await callApi(resUrl('releaseReservation', id));
  refreshGroupModal();
}

async function releaseReservationById(id) {
  const res = await callApi(resUrl('releaseReservation', id));
  if (res && res.ok) refreshGroupModal();
  return res;
}

function countdownHTML(expiresAt) {
  return `<span class="countdown font-mono font-bold" data-expires="${esc(expiresAt)}">--:--</span>`;
}

let expiryRefreshAt = 0;
function tickCountdowns() {
  document.querySelectorAll('.countdown[data-expires]').forEach(el => {
    const left = Date.parse(el.dataset.expires) - serverNow();
    if (left <= 0) {
      el.textContent = '00:00';
      el.className = 'countdown font-mono font-bold text-rose-600';
      if (Date.now() - expiryRefreshAt > 3000) { expiryRefreshAt = Date.now(); refreshState(); }
      return;
    }
    const m = Math.floor(left / 60000);
    const sec = Math.floor((left % 60000) / 1000);
    el.textContent = `${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
    el.className = 'countdown font-mono font-bold ' + (left < 120000 ? 'text-rose-600' : left < 300000 ? 'text-amber-600' : 'text-emerald-600');
  });
}

/* ------------------------------------------------------- shared seat actions */
async function forceReleaseSeat(seatId) {
  await callApi(seatUrl('release', seatId));
}

async function toggleMaintenance(seatId) {
  await callApi(seatUrl('maintenance', seatId));
}

function openAdminSeatOverride(seatId) {
  // clicking a seat shows its reservation queue; release / block actions live in that inspector
  openSeatSchedule(seatId);
}

async function acceptReservation(id) {
  await callApi(resUrl('accept', id));
}

async function rejectReservation(id) {
  await callApi(resUrl('reject', id));
}

async function cancelReservation(id, seatOnly = false) {
  const res = await callApi(resUrl('cancel', id), seatOnly ? { seat_only: true } : {});
  if (res && res.ok) refreshGroupModal();
}

async function verifyCode() {
  const input = document.getElementById('verify-code-input');
  const res = await callApi(CONFIG.urls.verify, { code: input.value });
  if (res && res.ok) input.value = '';
}

/* ------------------------------------------------------------ modals & toast */
function openModal(id) {
  document.getElementById(id).classList.remove('hidden');
}

function closeModal(id) {
  document.getElementById(id).classList.add('hidden');
}

function showToast(message, type = 'info', duration = 3000, onClick = null) {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  let bg = 'bg-navy-900 text-white';
  if (type === 'success') bg = 'bg-emerald-600 text-white';
  if (type === 'warning') bg = 'bg-amber-500 text-white';
  if (type === 'info') bg = 'bg-royal-600 text-white';

  toast.className = `${bg} px-4 py-3 rounded-xl shadow-xl text-xs font-semibold flex items-center space-x-2 border border-white/20 transition-all transform translate-y-2 pointer-events-auto max-w-sm`;
  const icon = document.createElement('i');
  icon.className = 'fa-solid fa-circle-info';
  const text = document.createElement('span');
  text.textContent = message;          // textContent: never interpret server/user text as HTML
  toast.append(icon, text);
  if (onClick) {
    toast.style.cursor = 'pointer';
    toast.addEventListener('click', () => { toast.remove(); onClick(); });
  }
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

/* ------------------------------------------------- admin: requests & time-in */
function renderAdminRequests() {
  const pending = groupReservations(state.pendingRequests || []);
  const awaiting = groupReservations(state.awaiting || []);
  const badge = document.getElementById('admin-tab-requests-badge');
  if (badge) {
    badge.textContent = pending.length;
    badge.classList.toggle('hidden', pending.length === 0);
  }
  setText('req-pending-count', pending.length);
  setText('req-awaiting-count', awaiting.length);

  const pendingBox = document.getElementById('req-pending-list');
  if (!pendingBox) return;

  const who = g => {
    const r = g.items[0];
    return `<div class="font-medium text-slate-800">${esc(r.user)}${r.title ? ` &bull; ${esc(r.title)}` : ''}</div>`;
  };
  const head = g => {
    const r = g.items[0];
    const grouped = !!g.batchId;
    return `<div>${grouped
      ? `<span class="text-[10px] uppercase font-bold px-1.5 py-0.5 rounded bg-royal-600 text-white">Group of ${g.items.length}</span> ${seatChips(g)}`
      : `<span class="font-mono font-bold text-navy-900">${esc(r.seat)}</span> <span class="text-slate-400">(${esc(r.zone)})</span>`}
      <span class="ml-1 text-[10px] uppercase font-bold px-1.5 py-0.5 rounded bg-sky-100 text-sky-800">${esc(r.role)}</span></div>`;
  };
  const clickAttr = g => g.batchId ? `class="text-xs space-y-0.5 cursor-pointer" onclick="openGroupModal('${esc(g.key)}', 'admin')" title="Click to see every seat"` : 'class="text-xs space-y-0.5"';

  pendingBox.innerHTML = pending.length ? pending.map(g => {
    const r = g.items[0];
    return `
    <div class="p-3 flex flex-wrap items-center justify-between gap-3">
      <div ${clickAttr(g)}>
        ${head(g)}
        ${who(g)}
        <div class="text-slate-500">${esc(r.time)} &bull; requested ${esc(fmtStamp(r.requestedAt))}${g.batchId ? ' &bull; <span class="text-royal-600 font-semibold">view seats</span>' : ''}</div>
      </div>
      <div class="flex space-x-2">
        <button onclick="acceptReservation(${r.id})" class="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-semibold">Accept${g.batchId ? ' group' : ''}</button>
        <button onclick="rejectReservation(${r.id})" class="px-3 py-1.5 rounded-lg bg-rose-600 hover:bg-rose-700 text-white text-xs font-semibold">Reject</button>
      </div>
    </div>`;
  }).join('') : '<p class="p-4 text-xs text-slate-500">No requests are waiting for approval.</p>';

  const awaitingBox = document.getElementById('req-awaiting-list');
  awaitingBox.innerHTML = awaiting.length ? awaiting.map(g => {
    const r = g.items[0];
    const seatIds = JSON.stringify(g.items.map(i => i.id)).replace(/"/g, '&quot;');
    const timing = r.live === false
      ? `<span class="text-indigo-600 font-semibold">Starts ${esc(fmtClock(r.startAt))}</span> (queued)`
      : `${countdownHTML(r.graceExpiresAt)} left to check in`;
    return `
    <div class="p-3 flex flex-wrap items-center justify-between gap-3">
      <div ${clickAttr(g)}>
        ${head(g)}
        ${who(g)}
        <div class="text-slate-500">Accepted ${esc(fmtClock(r.acceptedAt))} &bull; ${timing}${g.batchId ? ' &bull; <span class="text-royal-600 font-semibold">view seats</span>' : ''}</div>
      </div>
      <button onclick="forceReleaseGroup(${seatIds})" class="px-3 py-1.5 rounded-lg bg-rose-50 text-rose-700 hover:bg-rose-100 border border-rose-200 text-xs font-semibold">Force Release${g.batchId ? ' all' : ''}</button>
    </div>`;
  }).join('') : '<p class="p-4 text-xs text-slate-500">Nobody is waiting to check in.</p>';

  const approvals = document.getElementById('approvals-badge');
  if (approvals && state.accountPending !== undefined) {
    approvals.textContent = state.accountPending;
    approvals.classList.toggle('hidden', !state.accountPending);
  }
  refreshGroupModal();
}

/* ------------------------------------------------------------ notifications */
const NOTIF_DOT = { success: 'bg-emerald-500', warning: 'bg-amber-500', info: 'bg-royal-500' };

function handleNewNotifications() {
  const items = (state.notifications || {}).items || [];
  items.filter(n => n.id > lastNotifId && !n.read).reverse()
    .forEach(n => showToast(n.message, n.kind, 7000, () => openNotification(n.id, n.url)));
  lastNotifId = Math.max(lastNotifId, ...items.map(n => n.id));
}

function renderNotifications() {
  const data = state.notifications || { unread: 0, items: [] };
  const badge = document.getElementById('notif-badge');
  if (!badge) return;
  badge.textContent = data.unread > 99 ? '99+' : data.unread;
  badge.classList.toggle('hidden', data.unread === 0);
  document.getElementById('notif-list').innerHTML = data.items.length ? data.items.map(n => `
    <button onclick="openNotification(${n.id}, '${esc(n.url)}')" class="w-full text-left px-4 py-3 flex items-start space-x-3 hover:bg-slate-50 ${n.read ? 'opacity-60' : ''}">
      <span class="mt-1.5 w-2 h-2 rounded-full flex-shrink-0 ${n.read ? 'bg-slate-300' : (NOTIF_DOT[n.kind] || NOTIF_DOT.info)}"></span>
      <span class="min-w-0">
        <span class="block text-xs text-slate-800 ${n.read ? '' : 'font-semibold'}">${esc(n.message)}</span>
        <span class="block text-[10px] text-slate-400 mt-0.5">${esc(fmtStamp(n.at))}</span>
      </span>
    </button>`).join('') : '<p class="px-4 py-6 text-xs text-center text-slate-400">No notifications yet.</p>';
}

function toggleNotifications(event) {
  event.stopPropagation();
  document.getElementById('notif-dropdown').classList.toggle('hidden');
}

// clicking a notification marks it read, then takes the user to the page or section it is about
async function openNotification(id, url) {
  const dropdown = document.getElementById('notif-dropdown');
  if (dropdown) dropdown.classList.add('hidden');
  await markNotificationsRead([id]);
  goTo(url);
}

function goTo(url) {
  if (!url) return;
  const target = new URL(url, window.location.origin);
  if (target.pathname === window.location.pathname && !target.search) {
    window.location.hash = target.hash;       // same page: just open the right tab
    applyHash();
  } else {
    window.location.href = target.pathname + target.search + target.hash;
  }
}

const HASH_TABS = {
  admin: ['overview', 'requests', 'seatmap', 'reservations', 'reserve', 'analytics', 'forecast'],
  professor: { sessions: 'sessions', reserve: 'reserve' },
  student: { bookings: 'bookings', find: 'find' },
};

function applyHash() {
  const name = window.location.hash.replace('#', '');
  if (!name) return;
  if (currentRole === 'admin' && HASH_TABS.admin.includes(name)) switchTab('admin', name);
  if (currentRole === 'professor' && HASH_TABS.professor[name]) switchTab('prof', name);
  if (currentRole === 'student' && HASH_TABS.student[name]) switchTab('student', name);
}

async function markNotificationsRead(ids) {
  await callApi(CONFIG.urls.notificationsRead, ids ? { ids } : {});
}

document.addEventListener('click', event => {
  const dropdown = document.getElementById('notif-dropdown');
  if (dropdown && !event.target.closest('#notif-wrap')) dropdown.classList.add('hidden');
});

/* ------------------------------------------------- admin: report for a date */
let reportRange = { start: CONFIG.serverDate, end: CONFIG.serverDate };
let reportLoaded = false;
let reportIncludesToday = false;

function shiftDate(iso, days) {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

function setReportPreset(preset) {
  const today = CONFIG.serverDate;
  if (preset === 'today') reportRange = { start: today, end: today };
  if (preset === 'yesterday') reportRange = { start: shiftDate(today, -1), end: shiftDate(today, -1) };
  if (preset === 'week') reportRange = { start: shiftDate(today, -6), end: today };
  document.getElementById('rep-start').value = reportRange.start;
  document.getElementById('rep-end').value = reportRange.end;
  loadDateReport();
}

function onReportDatesChanged() {
  const start = document.getElementById('rep-start').value;
  if (!start) return;
  reportRange = { start, end: document.getElementById('rep-end').value || start };
  loadDateReport();
}

function reportQuery() {
  return new URLSearchParams(reportRange).toString();
}

async function loadDateReport() {
  try {
    const res = await fetch(`${CONFIG.urls.analytics}?${reportQuery()}`, { credentials: 'same-origin', cache: 'no-store' });
    const data = await res.json();
    if (!data.ok) {
      showToast(data.error || 'Could not load the report.', 'warning');
      return;
    }
    reportLoaded = true;
    reportIncludesToday = data.report.includesToday;
    renderDateReport(data.report);
  } catch (err) { /* offline: keep the last report */ }
}

// a report that includes today changes as the day goes on, so keep it fresh while it is on screen
function refreshLiveReport() {
  const panel = document.getElementById('admin-panel-analytics');
  if (currentRole === 'admin' && reportLoaded && reportIncludesToday && panel && !panel.classList.contains('hidden')) {
    loadDateReport();
  }
}

function exportDateReport() {
  window.location.href = `${CONFIG.urls.dateReportCsv}?${reportQuery()}`;
}

function renderDateReport(rep) {
  document.getElementById('rep-start').value = rep.start;
  document.getElementById('rep-end').value = rep.end;
  setText('rep-range-label', rep.start === rep.end ? rep.start : `${rep.start} to ${rep.end}`);
  document.getElementById('rep-live-note').classList.toggle('hidden', !rep.includesToday);

  document.querySelectorAll('[data-rp]').forEach(el => {
    const v = anGet(rep, el.dataset.rp);
    if (v !== undefined) el.textContent = v;
  });
  document.querySelectorAll('[data-rp-bar]').forEach(el => {
    const v = anGet(rep, el.dataset.rpBar);
    if (v !== undefined) el.style.width = `${Math.min(100, v)}%`;
  });

  document.getElementById('rep-roles').innerHTML = anBars(
    rep.roles.map(r => ({ label: r.label, detail: `${r.count} • ${r.pct}%`, v: r.pct })),
    i => i.v, 'bg-royal-500');

  document.getElementById('rep-top-seats').innerHTML = rep.topSeats.length ? rep.topSeats.map(t => `
    <tr>
      <td class="p-3 font-bold text-navy-900">${esc(t.seat)} <span class="text-slate-400 font-normal">(${esc(t.zone)})</span></td>
      <td class="p-3">${t.count}</td>
      <td class="p-3">${t.pct}%</td>
      <td class="p-3 text-slate-600">${Math.floor(t.minutes / 60)}h ${t.minutes % 60}m</td>
    </tr>`).join('') : '<tr><td colspan="4" class="p-4 text-center text-slate-400">No seat sessions on this date.</td></tr>';
}

/* ------------------------------------------------------------- edit profile */
function updateBanner() {
  const u = USERS[currentRole];
  if (!u) return;
  document.getElementById('user-name').innerText = u.name;
  document.getElementById('user-avatar').innerText = u.avatar;
  document.getElementById('user-subtitle').innerText = u.sub;
}

function openEditProfile() {
  const u = USERS[currentRole] || {};
  document.getElementById('edit-first-name').value = u.firstName || '';
  document.getElementById('edit-middle-name').value = u.middleName || '';
  document.getElementById('edit-last-name').value = u.lastName || '';
  const note = document.getElementById('edit-profile-pending');
  const pending = state.profileRequest;
  note.classList.toggle('hidden', !pending);
  if (pending) note.textContent = `You already asked to change your name to "${pending.name}" (${fmtStamp(pending.at)}). Sending a new request replaces it.`;
  openModal('modal-edit-profile');
}

async function submitNameRequest() {
  const res = await callApi(CONFIG.urls.nameRequest, {
    first_name: document.getElementById('edit-first-name').value.trim(),
    middle_name: document.getElementById('edit-middle-name').value.trim(),
    last_name: document.getElementById('edit-last-name').value.trim(),
  });
  if (res && res.ok) closeModal('modal-edit-profile');
}


/* ------------------------------------------- time slots, seat queue & inspector */
const QUEUE_STYLE = {
  pending:  'bg-amber-50 border-amber-200 text-amber-900',
  upcoming: 'bg-indigo-50 border-indigo-200 text-indigo-900',
  reserved: 'bg-sky-50 border-sky-200 text-sky-900',
  active:   'bg-emerald-50 border-emerald-200 text-emerald-900',
};

function queueBadge(seat) {
  return seat.queueCount
    ? `<span class="absolute -top-1.5 -right-1.5 bg-royal-600 text-white text-[9px] leading-none rounded-full min-w-[16px] h-4 px-1 flex items-center justify-center">${seat.queueCount}</span>`
    : '';
}

function addMinutes(hhmm, minutes) {
  const [h, m] = hhmm.split(':').map(Number);
  const total = Math.min(23 * 60 + 59, Math.max(0, h * 60 + m + minutes));
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
}

function minutesOf(hhmm) {
  const [h, m] = (hhmm || '0:0').split(':').map(Number);
  return h * 60 + m;
}

// date / start / end inputs share an id prefix: modal-res (student) or prof-res (professor, admin)
function initSlotInputs(prefix, maxMinutes, defaultMinutes) {
  const date = document.getElementById(`${prefix}-date`);
  date.min = CONFIG.serverDate;
  date.max = shiftDate(CONFIG.serverDate, CONFIG.slots.maxAdvanceDays);
  date.value = CONFIG.serverDate;
  const [h, m] = CONFIG.serverClock.split(':').map(Number);
  const next = Math.ceil((h * 60 + m + 1) / 15) * 15;          // next quarter hour
  const start = addMinutes('00:00', Math.min(next, 23 * 60 + 45));
  document.getElementById(`${prefix}-start`).value = start;
  document.getElementById(`${prefix}-end`).value = addMinutes(start, Math.min(defaultMinutes, maxMinutes));
}

function readSlot(prefix) {
  return {
    date: document.getElementById(`${prefix}-date`).value,
    start_time: document.getElementById(`${prefix}-start`).value,
    end_time: document.getElementById(`${prefix}-end`).value,
  };
}

function slotProblem(slot, maxMinutes) {
  if (!slot.date || !slot.start_time || !slot.end_time) return 'Choose a date, a start time and an end time.';
  const length = minutesOf(slot.end_time) - minutesOf(slot.start_time);
  if (length <= 0) return 'The end time must be later than the start time.';
  if (length < CONFIG.slots.minMinutes) return `A reservation must be at least ${CONFIG.slots.minMinutes} minutes long.`;
  if (length > maxMinutes) return `A reservation can be at most ${maxMinutes / 60} hours long.`;
  return '';
}

function showSlotConflict(id, message) {
  const box = document.getElementById(id);
  if (!box) return;
  box.classList.toggle('hidden', !message);
  box.innerHTML = message ? `<i class="fa-solid fa-triangle-exclamation mr-1"></i>${esc(message)}` : '';
}

function updateSlotHint(kind) {
  const student = kind === 'student';
  const prefix = student ? 'modal-res' : 'prof-res';
  const max = student ? CONFIG.slots.studentMaxMinutes : batchMaxMinutes();
  const slot = readSlot(prefix);
  const length = minutesOf(slot.end_time) - minutesOf(slot.start_time);
  const shown = length > 0 ? `${Math.floor(length / 60)}h ${length % 60}m selected. ` : '';
  document.getElementById(student ? 'modal-slot-hint' : 'prof-slot-hint').textContent =
    `${shown}Max ${max / 60} hours, up to ${CONFIG.slots.maxAdvanceDays} days ahead. Other people can book this seat at other times.`;
}

function onSlotInputsChanged(kind) {
  updateSlotHint(kind);
  showSlotConflict(kind === 'student' ? 'modal-slot-conflict' : 'prof-slot-conflict', '');
  if (kind === 'student') loadModalQueue();
}

function queueRowsHTML(items, emptyText, admin = false) {
  if (!items.length) return `<p class="text-[11px] text-slate-500 bg-emerald-50 border border-emerald-200 rounded-lg p-2">${esc(emptyText)}</p>`;
  return items.map(i => {
    const actions = !admin ? '' : (i.status === 'pending'
      ? `<button onclick="acceptReservation(${i.id})" class="text-[11px] font-semibold text-emerald-700 hover:underline">Accept</button>
         <button onclick="rejectReservation(${i.id})" class="text-[11px] font-semibold text-rose-600 hover:underline">Reject</button>`
      : `<button onclick="releaseReservationById(${i.id})" class="text-[11px] font-semibold text-rose-600 hover:underline">Release</button>`);
    return `
      <div class="border rounded-lg px-3 py-2 text-xs flex flex-wrap items-center justify-between gap-2 ${QUEUE_STYLE[i.status] || 'bg-slate-50 border-slate-200'}">
        <div>
          <span class="font-mono font-bold">${esc(i.span)}</span>
          <span class="ml-1 text-[10px] font-bold uppercase">${esc(i.statusLabel)}</span>
          <div class="text-[11px] opacity-80">${esc(i.mine ? 'You' : i.who)}${i.title ? ` &bull; ${esc(i.title)}` : ''}</div>
        </div>
        <div class="flex items-center gap-3">${actions}</div>
      </div>`;
  }).join('');
}

async function fetchSchedule(seatId, date) {
  const res = await fetch(`${seatUrl('schedule', seatId)}?date=${encodeURIComponent(date)}`, { credentials: 'same-origin', cache: 'no-store' });
  const data = await res.json();
  return data.ok ? data.schedule : null;
}

async function loadModalQueue() {
  if (!targetStudentSeatToBook) return;
  const date = document.getElementById('modal-res-date').value || CONFIG.serverDate;
  setText('modal-queue-date', date === CONFIG.serverDate ? 'today' : date);
  const box = document.getElementById('modal-seat-queue');
  try {
    const sch = await fetchSchedule(targetStudentSeatToBook, date);
    box.innerHTML = sch ? queueRowsHTML(sch.items, 'Nobody has booked this seat yet - any time is free.') : '';
  } catch (err) { box.innerHTML = ''; }
}

/* seat inspector: a seat's reservation queue for one date */
let scheduleSeat = null;

function openSeatSchedule(seatId) {
  scheduleSeat = seatId;
  document.getElementById('seat-schedule-date').value = CONFIG.serverDate;
  document.getElementById('seat-schedule-body').innerHTML = '<p class="text-xs text-slate-400">Loading&hellip;</p>';
  openModal('modal-seat-schedule');
  loadSeatSchedule();
}

function shiftSeatSchedule(days) {
  const input = document.getElementById('seat-schedule-date');
  input.value = shiftDate(input.value || CONFIG.serverDate, days);
  loadSeatSchedule();
}

function refreshSeatSchedule() {
  const modal = document.getElementById('modal-seat-schedule');
  if (scheduleSeat && modal && !modal.classList.contains('hidden')) loadSeatSchedule();
}

async function loadSeatSchedule() {
  if (!scheduleSeat) return;
  const date = document.getElementById('seat-schedule-date').value || CONFIG.serverDate;
  let sch = null;
  try { sch = await fetchSchedule(scheduleSeat, date); } catch (err) { /* offline */ }
  if (!sch) return;
  const admin = currentRole === 'admin';
  setText('seat-schedule-title', `Seat ${sch.seat}`);
  setText('seat-schedule-sub', `${sch.zoneName} \u2022 now: ${sch.status}`);
  setText('seat-schedule-today-label', sch.isToday ? "Today's queue" : '');
  document.getElementById('seat-schedule-body').innerHTML =
    queueRowsHTML(sch.items, 'No reservations queued for this date.', admin);
  const actions = document.getElementById('seat-schedule-actions');
  actions.innerHTML = !admin ? '' : [
    sch.status === 'occupied' || sch.status === 'reserved'
      ? `<button onclick="forceReleaseSeat('${esc(sch.seat)}')" class="px-3 py-1.5 rounded-lg bg-rose-50 text-rose-700 hover:bg-rose-100 border border-rose-200 text-xs font-semibold">Release current session</button>` : '',
    `<button onclick="toggleMaintenance('${esc(sch.seat)}')" class="px-3 py-1.5 rounded-lg bg-slate-100 text-slate-700 hover:bg-slate-200 border border-slate-200 text-xs font-semibold">${sch.status === 'maintenance' ? 'Back in service' : 'Block for maintenance'}</button>`,
  ].join('');
}

/* ----------------------------------------------- admin: predictive analytics */
let forecast = null;
let forecastLoadedAt = 0;
let forecastDay = 0;

async function loadForecast(force = false) {
  if (!force && forecast && Date.now() - forecastLoadedAt < 60000) { renderForecast(); return; }
  try {
    const res = await fetch(CONFIG.urls.forecast, { credentials: 'same-origin', cache: 'no-store' });
    const data = await res.json();
    if (!data.ok) { setText('fc-note', data.error || 'Could not load the forecast.'); return; }
    forecast = data.forecast;
    forecastLoadedAt = Date.now();
    renderForecast();
  } catch (err) { setText('fc-note', 'Could not reach the server.'); }
}

setInterval(() => {
  const panel = document.getElementById('admin-panel-forecast');
  if (panel && !panel.classList.contains('hidden') && !document.hidden) loadForecast(true);
}, 60000);

const RISK_STYLE = {
  low: ['bg-emerald-100 text-emerald-800', 'bg-emerald-400'],
  moderate: ['bg-amber-100 text-amber-800', 'bg-amber-400'],
  peak: ['bg-rose-100 text-rose-800', 'bg-rose-500'],
};
const REC_STYLE = {
  critical: 'bg-rose-50 border-rose-200 text-rose-900',
  warning: 'bg-amber-50 border-amber-200 text-amber-900',
  info: 'bg-sky-50 border-sky-200 text-sky-900',
};

function riskBadge(risk, label) {
  return `<span class="text-[10px] font-bold px-2 py-0.5 rounded-full ${RISK_STYLE[risk][0]} whitespace-nowrap">${esc(label)}</span>`;
}

function selectForecastDay(i) { forecastDay = i; renderForecast(); }

function renderForecast() {
  const f = forecast;
  if (!f) return;
  setText('fc-note', f.meta.note);
  const conf = document.getElementById('fc-confidence');
  conf.textContent = `${f.meta.confidence} confidence`;
  conf.className = 'text-[10px] font-bold px-2 py-0.5 rounded-full uppercase tracking-wider ' +
    (f.meta.confidence === 'High' ? 'bg-emerald-100 text-emerald-800' : f.meta.confidence === 'Medium' ? 'bg-amber-100 text-amber-800' : 'bg-slate-100 text-slate-600');

  // congestion windows
  document.getElementById('fc-windows').innerHTML = f.windows.length ? f.windows.map(w => `
    <div class="p-3 rounded-lg border flex items-start space-x-2.5 text-xs ${w.level === 'high' ? 'bg-rose-50 border-rose-200 text-rose-900' : 'bg-sky-50 border-sky-200 text-sky-900'}">
      <i class="fa-solid ${w.level === 'high' ? 'fa-fire' : 'fa-chart-simple'} text-base mt-0.5"></i>
      <div><span class="font-bold">${esc(w.message)}</span></div>
    </div>`).join('') : '<p class="text-xs text-slate-500">No congestion expected in the coming hours.</p>';

  // capacity & no-show cards
  const c = f.capacity, n = f.noShow;
  const card = (title, value, sub, accent) => `
    <div class="bg-white p-5 rounded-xl border border-slate-200 shadow-sm space-y-1">
      <p class="text-xs font-semibold text-slate-500 uppercase tracking-wider">${title}</p>
      <p class="text-3xl font-bold ${accent}">${value}</p>
      <p class="text-[11px] text-slate-500">${sub}</p>
    </div>`;
  document.getElementById('fc-capacity').innerHTML =
    card('Predicted peak occupancy', `${Math.round(c.peakPct)}%`, `${esc(c.peakDay)}${c.peakWindow ? ` &bull; ${esc(c.peakWindow)}` : ''}`, c.peakPct >= 70 ? 'text-rose-600' : c.peakPct >= 40 ? 'text-amber-600' : 'text-emerald-600') +
    card('Walk-in buffer', `${c.recommendedWalkInBuffer} seat${c.recommendedWalkInBuffer === 1 ? '' : 's'}`, `Recommended to keep free (walk-ins ~${Math.round(c.walkInShare)}% of sessions)`, 'text-royal-600') +
    card('Expected no-show rate', `${n.rate}%`, `Students ${n.byRole.student}% &bull; Professors ${n.byRole.professor}%`, 'text-amber-600') +
    card('Expected no-shows', `${n.perDay}/day`, `${n.nextSevenDays} over the next 7 days &bull; headroom ${c.overbookHeadroom} seat${c.overbookHeadroom === 1 ? '' : 's'}`, 'text-navy-900');

  // 7-day trend + cards
  const maxVol = Math.max(1, ...f.days.map(d => d.reservations));
  document.getElementById('fc-trend').innerHTML = f.days.map((d, i) => `
    <button onclick="selectForecastDay(${i})" class="flex-1 h-full flex flex-col justify-end items-center gap-1 ${i === forecastDay ? 'opacity-100' : 'opacity-70 hover:opacity-100'}" title="${esc(d.weekday)}: ${d.reservations} reservations, peak ${d.peakUtilization}%">
      <span class="text-[10px] font-bold text-rose-600">${Math.round(d.peakUtilization)}%</span>
      <div class="w-full max-w-[44px] flex flex-col justify-end rounded-t overflow-hidden" style="height: ${Math.max(3, Math.round(d.reservations / maxVol * 80))}%">
        <div class="bg-amber-400" style="height: ${d.reservations ? d.professor / d.reservations * 100 : 0}%"></div>
        <div class="bg-royal-500" style="height: ${d.reservations ? d.student / d.reservations * 100 : 0}%"></div>
      </div>
    </button>`).join('');
  document.getElementById('fc-days').innerHTML = f.days.map((d, i) => `
    <button onclick="selectForecastDay(${i})" class="text-left p-3 rounded-lg border space-y-1 ${i === forecastDay ? 'border-royal-500 ring-2 ring-royal-200 bg-royal-50' : 'border-slate-200 bg-white hover:bg-slate-50'}">
      <div class="flex items-center justify-between"><span class="text-xs font-bold text-navy-900">${esc(d.short)}${d.isToday ? ' (today)' : ''}</span></div>
      <div class="text-[11px] text-slate-500">${esc(d.date)}</div>
      <div class="text-sm font-bold text-navy-900">${d.reservations} <span class="text-[10px] font-normal text-slate-500">reservations</span></div>
      <div class="text-[10px] text-slate-500">${d.student} students &bull; ${d.professor} prof.${d.bookedSoFar ? ` &bull; ${d.bookedSoFar} booked` : ''}</div>
      <div class="text-[11px] text-slate-600">Avg ${Math.round(d.avgUtilization)}% &bull; peak ${Math.round(d.peakUtilization)}% at ${esc(d.peakHourLabel)}</div>
      ${riskBadge(d.risk, d.riskLabel)}
    </button>`).join('');

  // hourly breakdown for the selected day
  const day = f.days[Math.min(forecastDay, f.days.length - 1)];
  setText('fc-hourly-title', `Hourly crowd forecast - ${day.weekday}, ${day.date}${day.isToday ? ' (today)' : ''}`);
  document.getElementById('fc-hourly-chart').innerHTML = day.hourly.map(h => `
    <div class="flex-1 h-full flex items-end ${h.past ? 'opacity-40' : ''}" title="${esc(h.label12)}: ~${h.pct}% (${esc(h.riskLabel)})">
      <div class="w-full ${RISK_STYLE[h.risk][1]} rounded-t" style="height: ${Math.max(2, h.pct)}%"></div>
    </div>`).join('');
  const rows = day.hourly.filter(h => !h.past && (h.pct >= 5 || h.bookedPct > 0));
  document.getElementById('fc-hourly-rows').innerHTML = rows.length ? rows.map(h => `
    <tr>
      <td class="p-3 font-semibold text-navy-900">${esc(h.label12)}</td>
      <td class="p-3"><div class="flex items-center gap-2"><div class="w-28 h-2 bg-slate-100 rounded-full overflow-hidden"><div class="h-full ${RISK_STYLE[h.risk][1]}" style="width: ${Math.min(100, h.pct)}%"></div></div><span>~${Math.round(h.pct)}%</span></div></td>
      <td class="p-3 text-slate-600">${h.bookedPct ? `${Math.round(h.bookedPct)}%` : '-'}</td>
      <td class="p-3">${riskBadge(h.risk, h.riskLabel)}</td>
    </tr>`).join('') : '<tr><td colspan="4" class="p-4 text-center text-slate-400">No notable demand expected for the remaining hours.</td></tr>';

  // bottlenecks
  const seatsRows = f.bottlenecks.seats;
  document.getElementById('fc-bn-seats').innerHTML = seatsRows.length ? seatsRows.map(r => `
    <tr>
      <td class="p-3 font-bold text-navy-900">${esc(r.seat)} <span class="text-slate-400 font-normal">(${esc(r.zone)})</span></td>
      <td class="p-3">${r.projectedFullLabel ? esc(r.projectedFullLabel) : '<span class="text-slate-400">not full</span>'}</td>
      <td class="p-3">${Math.round(r.peakProbability)}% at ${esc(r.peakHourLabel)}</td>
      <td class="p-3">${Math.round(r.frequency)}%</td>
    </tr>`).join('') : '<tr><td colspan="4" class="p-4 text-center text-slate-400">Not enough booking history yet.</td></tr>';
  const zoneRows = f.bottlenecks.zones;
  document.getElementById('fc-bn-zones').innerHTML = zoneRows.length ? zoneRows.map(z => `
    <div class="space-y-1">
      <div class="flex justify-between text-xs"><span class="font-semibold text-slate-700">${esc(z.name)}</span>
        <span class="text-slate-500">peak ~${Math.round(z.peakPct)}% at ${esc(z.peakHourLabel)} &bull; ${z.projectedFullLabel ? `high demand from ${esc(z.projectedFullLabel)}` : 'no bottleneck'}</span></div>
      <div class="h-2.5 bg-slate-100 rounded-full overflow-hidden"><div class="h-full ${RISK_STYLE[z.risk][1]}" style="width: ${Math.min(100, z.peakPct)}%"></div></div>
    </div>`).join('') : '<p class="text-xs text-slate-400">Not enough booking history yet.</p>';

  // recommendations
  document.getElementById('fc-recs').innerHTML = f.recommendations.length ? f.recommendations.map(r => `
    <div class="p-3 rounded-lg border text-xs ${REC_STYLE[r.level] || REC_STYLE.info}"><span class="font-bold">${esc(r.title)}:</span> ${esc(r.text)}</div>`).join('')
    : '<p class="text-xs text-slate-500">No actions needed right now.</p>';
}

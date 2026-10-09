// Free space on the server's disk, shown in the header on every page except the reader
// (the header is hidden there). Refreshed every minute, when the tab comes back, after a
// job ends and after books are removed.
(function () {
  'use strict';
  var H = window.Hua, $ = H.$;

  var UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];
  var lastTask = '';

  function size(bytes) {
    var n = Math.max(0, Number(bytes) || 0);
    var u = 0;
    while (n >= 1024 && u < UNITS.length - 1) { n /= 1024; u += 1; }
    var text = u < 2 || n >= 100 ? String(Math.round(n)) : n.toFixed(1);
    return text + ' ' + UNITS[u];
  }

  function paint(id, s, detail) {
    var box = $(id);
    if (!box) return;
    if (!s) { box.hidden = true; return; }
    var usedPct = s.total > 0 ? Math.min(100, Math.max(0, (s.used / s.total) * 100)) : 0;
    box.hidden = false;
    box.setAttribute('data-level', s.level);
    $(id + '-fill').style.width = usedPct.toFixed(1) + '%';
    $(id + '-text').textContent = size(s.free) + ' free';
    box.title = detail;
    box.setAttribute('aria-label', detail);
  }

  function draw(s) {
    if (!s) { paint('storage', null, ''); paint('ram', null, ''); return; }
    var disk = size(s.free) + ' free of ' + size(s.total) + ' on the disk for the ' + s.where + '.';
    if (s.level !== 'ok') disk += ' Running low: downloads may fail.';
    paint('storage', s, 'Storage: ' + disk);
    var ram = s.ram;
    if (!ram) { paint('ram', null, ''); return; }
    var mem = size(ram.free) + ' free of ' + size(ram.total) + ' RAM.';
    if (ram.swap_used) mem += ' ' + size(ram.swap_used) + ' of swap in use.';
    if (ram.level !== 'ok') mem += ' Running low: translation may stall.';
    paint('ram', ram, 'Memory: ' + mem);
  }

  var inflight = false;
  function refresh() {
    if (inflight) return;
    inflight = true;
    H.api('GET', '/api/storage').then(function (res) {
      draw(res.ok ? res.data : null);
    }).catch(function () {}).then(function () { inflight = false; });
  }

  // A finished, failed or cancelled job has changed what is on disk.
  H.onState(function (s) {
    var t = s && s.task;
    var key = t && (t.state === 'done' || t.state === 'error' || t.state === 'cancelled') ? t.id + ':' + t.state : '';
    if (key && key !== lastTask) refresh();
    lastTask = key;
  });

  setInterval(function () { if (document.visibilityState === 'visible') refresh(); }, 60000);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') refresh();
  });
  refresh();
  window.HuaStorage = { refresh: refresh };
})();

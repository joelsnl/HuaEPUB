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

  function draw(s) {
    var box = $('storage');
    if (!s) { box.hidden = true; return; }
    var usedPct = s.total > 0 ? Math.min(100, Math.max(0, (s.used / s.total) * 100)) : 0;
    box.hidden = false;
    box.setAttribute('data-level', s.level);
    $('storage-fill').style.width = usedPct.toFixed(1) + '%';
    $('storage-text').textContent = size(s.free) + ' free';
    var full = size(s.free) + ' free of ' + size(s.total) + ' on the disk for the ' + s.where + '.';
    if (s.level !== 'ok') full += ' Running low: downloads may fail.';
    box.title = full;
    box.setAttribute('aria-label', 'Storage: ' + full);
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

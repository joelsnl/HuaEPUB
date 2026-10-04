// Settings: the browser-editable part of settings.json (shared with the desktop app).
(function () {
  'use strict';
  var H = window.Hua, $ = H.$, setText = H.setText;

  var current = null;

  function fill(s) {
    current = s;
    $('set-translate').checked = !!s.translate;
    $('set-clean').checked = !!s.clean;
    $('set-cache').checked = !!s.use_cache;
    $('set-workers').value = String(s.workers);
    $('set-workers').max = String(s.max_workers || 500);
    var backend = $('set-backend');
    backend.textContent = '';
    s.backends.forEach(function (b) {
      var o = document.createElement('option');
      o.value = b.value; o.textContent = b.label;
      backend.appendChild(o);
    });
    backend.value = s.backend;
    var gloss = $('set-glossary');
    gloss.textContent = '';
    s.glossaries.forEach(function (g) {
      var o = document.createElement('option');
      o.value = g.value; o.textContent = g.label;
      gloss.appendChild(o);
    });
    gloss.value = s.glossary;
    $('set-polish').checked = !!s.polish;
    $('set-polish').disabled = !s.polish_available || !s.translate;
    setText('set-polish-note', s.polish_available
      ? 'Copy-edits the machine English on the PC after translating. Slower.'
      : 'Polish needs its model on the PC first. Tick Polish English once in the desktop app to download it.');
    var label = (s.backends.filter(function (b) { return b.value === s.backend; })[0] || {}).label || s.backend;
    if (window.HuaSingle) window.HuaSingle.setTranslator(s.translate ? label : 'Off (Chinese EPUB)');
    if (window.HuaRead) window.HuaRead.setFont(s.reader_font_pt || 18, false);
  }

  function load() {
    return H.api('GET', '/api/settings').then(function (res) { if (res.ok) fill(res.data); });
  }

  function save(change) {
    setText('set-status', 'Saving…');
    H.api('PUT', '/api/settings', change).then(function (res) {
      if (!res.ok) { setText('set-status', H.errorText(res.data)); if (current) fill(current); return; }
      fill(res.data);
      setText('set-status', 'Saved.');
    }).catch(function () { setText('set-status', H.ERRORS.network); });
  }

  $('set-translate').addEventListener('change', function (e) { save({ translate: e.target.checked }); });
  $('set-clean').addEventListener('change', function (e) { save({ clean: e.target.checked }); });
  $('set-cache').addEventListener('change', function (e) { save({ use_cache: e.target.checked }); });
  $('set-polish').addEventListener('change', function (e) { save({ polish: e.target.checked }); });
  $('set-backend').addEventListener('change', function (e) { save({ backend: e.target.value }); });
  $('set-glossary').addEventListener('change', function (e) { save({ glossary: e.target.value }); });
  $('set-workers').addEventListener('change', function (e) {
    var v = parseInt(e.target.value, 10);
    if (!(v >= 1 && v <= (current ? current.max_workers : 500))) {
      setText('set-status', 'Workers must be between 1 and ' + (current ? current.max_workers : 500) + '.');
      if (current) e.target.value = String(current.workers);
      return;
    }
    save({ workers: v });
  });
  $('settings-form').addEventListener('submit', function (e) { e.preventDefault(); });
  $('btn-signout').addEventListener('click', function () {
    H.api('POST', '/api/logout').then(function () { window.location.href = '/login'; });
  });

  H.onView('settings', load);
  window.HuaSettings = { load: load };
})();

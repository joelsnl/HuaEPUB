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
      ? 'Copy-edits the machine English on this computer after translating. Slower.'
      : 'Install Polish below, then turn this on.');
    $('set-output').value = s.output_dir || '';
    var label = (s.backends.filter(function (b) { return b.value === s.backend; })[0] || {}).label || s.backend;
    if (window.HuaSingle) window.HuaSingle.setTranslator(s.translate ? label : 'Off (Chinese EPUB)');
    if (window.HuaRead && window.HuaRead.applyPrefs) window.HuaRead.applyPrefs(s);
    else if (window.HuaRead) window.HuaRead.setFont(s.reader_font_pt || 18, false);
  }

  function load() {
    return H.api('GET', '/api/settings').then(function (res) { if (res.ok) fill(res.data); });
  }

  var SETTING_ACTIONS = {
    translate: 'change translation',
    clean: 'change cleaning',
    use_cache: 'change the chapter cache',
    polish: 'change Polish',
    backend: 'change the translator',
    glossary: 'change the glossary',
    workers: 'change how many workers run',
    output_dir: 'change the books folder'
  };

  function save(change) {
    var key = Object.keys(change)[0];
    var action = SETTING_ACTIONS[key] || 'change settings';
    setText('set-status', 'Saving…');
    H.api('PUT', '/api/settings', change).then(function (res) {
      if (!res.ok) { setText('set-status', H.errorText(res.data, '', action)); if (current) fill(current); return; }
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
  $('set-output-save').addEventListener('click', function () {
    save({ output_dir: $('set-output').value });
  });

  function install(what, label) {
    setText('set-status', 'Starting…');
    H.api('POST', '/api/install', { what: what }).then(function (res) {
      setText('set-status', res.ok ? (label + ' started.') : H.errorText(res.data, '', 'start ' + label));
    }).catch(function () { setText('set-status', H.ERRORS.network); });
  }
  $('set-install-polish').addEventListener('click', function () { install('polish', 'Polish'); });
  $('set-install-nmt').addEventListener('click', function () { install('nmt', 'Offline NMT'); });
  $('set-install-ollama').addEventListener('click', function () { install('ollama', 'the Ollama model'); });
  $('set-install-service').addEventListener('click', function () {
    setText('set-status', 'Checking…');
    H.api('POST', '/api/service').then(function (res) {
      var text = (res.data && (res.data.message || res.data.detail)) || H.errorText(res.data, '', 'install the service');
      setText('set-status', text);
    }).catch(function () { setText('set-status', H.ERRORS.network); });
  });
  $('settings-form').addEventListener('submit', function (e) { e.preventDefault(); });
  $('btn-signout').addEventListener('click', function () {
    H.api('POST', '/api/logout').then(function () { window.location.href = '/login'; });
  });

  H.onView('settings', load);
  window.HuaSettings = { load: load };
})();

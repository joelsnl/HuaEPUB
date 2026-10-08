// HuaEPUB server mode: shared helpers, page switching, the job dock and the live state stream.
// No framework and no inline script (CSP: script-src 'self'). Each page lives in its own file.
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  var ERRORS = {
    blocked_url: 'Blocked link. Use a full http(s) link to a public novel page.',
    no_parser: 'No parser handles that site.',
    fetch_failed: 'Could not reach the site.',
    no_chapters: 'No chapters found at that link.',
    busy: "Can't do that yet. Another job is still running. Wait for it or cancel it first.",
    preview_expired: 'That chapter list expired. Read the link again.',
    bad_range: 'Check the chapter range.',
    sign_in_required: 'You were signed out. Reload the page to sign in again.',
    no_links: 'Paste at least one link.',
    too_many_links: 'Paste 50 links or fewer at a time.',
    unknown_book: 'That book is no longer in the Library.',
    nothing_to_update: 'No books have new chapters. Check for updates first.',
    empty_library: 'The Library is empty.',
    no_epub: 'No EPUB for this book on this PC.',
    nothing_to_read: 'Nothing to read yet.',
    cannot_resume: 'That download cannot be resumed.',
    bad_setting: 'That setting was not saved.',
    network: 'Lost contact with the PC. Is server mode still on?'
  };

  // ---------- small DOM helpers ----------
  function setText(id, text) { var el = typeof id === 'string' ? $(id) : id; if (el) el.textContent = text; }
  function show(id, on) { var el = typeof id === 'string' ? $(id) : id; if (el) el.hidden = !on; }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  // ---------- API ----------
  function api(method, path, body, signal) {
    var opts = { method: method, headers: { 'X-HuaEPUB': '1' }, signal: signal, credentials: 'same-origin' };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (r) {
      return r.text().then(function (t) {
        var data = null;
        try { data = t ? JSON.parse(t) : null; } catch (e) { data = null; }
        if (r.status === 401 && data && data.error === 'sign_in_required') {
          window.location.href = '/login';
        }
        return { ok: r.ok, status: r.status, data: data };
      });
    });
  }

  function errorText(data, fallback, action) {
    var key = data && data.error;
    var text = ERRORS[key] || fallback || 'Something went wrong.';
    if (key === 'busy') {
      var trying = action || 'do that';
      var running = (data && data.label) ? (' ' + data.label + ' is still running.') : ' Another job is still running.';
      text = "Can't " + trying + " yet." + running + ' Wait for it or cancel it first.';
    }
    if ((key === 'fetch_failed' || key === 'cannot_resume' ||
         key === 'bad_setting' || key === 'nothing_to_read') && data.detail) text += ' ' + data.detail;
    return text;
  }

  // ---------- downloads ----------
  function fileUrl(file) { return '/api/files/' + encodeURIComponent(file.token); }
  function triggerDownload(file) {
    var a = document.createElement('a');
    a.href = fileUrl(file);
    a.download = file.name || 'book.epub';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // ---------- slips ----------
  // Draw n strips into host: [0, done) translated, [done, fetched) fetched, cur outlined.
  function drawSlips(host, n, fetched, done, cur, flags) {
    if (host.childElementCount !== 1 || host.firstChild.childElementCount !== n) {
      host.textContent = '';
      var row = el('div', 'slips-row');
      for (var i = 0; i < n; i++) {
        var slip = el('div', 'slip');
        slip.appendChild(document.createElement('b'));
        slip.appendChild(document.createElement('i'));
        row.appendChild(slip);
      }
      host.appendChild(row);
    }
    var slips = host.firstChild.children;
    for (var j = 0; j < slips.length; j++) {
      var cls = 'slip';
      if (j < done) cls += ' is-done';
      else if (j < fetched) cls += ' is-fetched';
      if (j === cur) cls += ' is-cur';
      if (flags && flags[j]) cls += ' is-flag';
      slips[j].className = cls;
    }
  }

  // Strip counts for a task snapshot (current novel only).
  function slipCounts(task, n) {
    if (!task) return { fetched: 0, done: 0, cur: -1 };
    if (task.state === 'done') return { fetched: n, done: n, cur: -1 };
    var translating = task.result && task.result.translated;
    var fetched = Math.min(n, Math.floor((task.fetched || 0) * n));
    var built = Math.min(n, Math.floor((task.built || 0) * n));
    var done = translating || task.phase === 'writing' ? built : 0;
    if (!translating && task.built > 0) { fetched = n; done = built; }
    var cur = task.phase === 'fetching' ? fetched : (task.phase === 'translating' ? done : -1);
    if (cur >= n) cur = -1;
    return { fetched: fetched, done: done, cur: cur };
  }

  // ---------- shared state + subscribers ----------
  var listeners = [];
  var latest = { task: null, busy: false, resume: null };
  function onState(fn) { listeners.push(fn); fn(latest); }
  function publish(payload) {
    latest = payload || latest;
    listeners.forEach(function (fn) { try { fn(latest); } catch (e) { if (window.console) console.error(e); } });
  }

  var stream = null;
  var poller = null;
  function connect() {
    stream = new EventSource('/api/events');
    stream.onmessage = function (ev) { try { publish(JSON.parse(ev.data)); } catch (e) { /* ignore */ } };
    stream.onerror = function () {
      if (stream) { stream.close(); stream = null; }
      startPolling();
      setTimeout(function () { if (!stream) { stopPolling(); connect(); } }, 15000);
    };
  }
  function startPolling() {
    if (poller) return;
    poller = setInterval(refresh, 1500);
  }
  function stopPolling() { if (poller) { clearInterval(poller); poller = null; } }
  function refresh() {
    return api('GET', '/api/state').then(function (res) { if (res.ok) publish(res.data); }).catch(function () {});
  }

  // ---------- views ----------
  var VIEWS = ['single', 'multi', 'library', 'read', 'settings'];
  var current = null;
  var viewHooks = {};
  function onView(name, fn) { viewHooks[name] = fn; }
  function go(name, arg) {
    if (VIEWS.indexOf(name) < 0) name = 'single';
    current = name;
    $('shell').setAttribute('data-view', name);
    VIEWS.forEach(function (v) { show('view-' + v, v === name); });
    document.querySelectorAll('.nav a').forEach(function (a) {
      var on = a.getAttribute('data-view') === name;
      a.classList.toggle('is-on', on);
      if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    if (name !== 'read') document.body.classList.remove('is-reading');
    if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
    if (viewHooks[name]) viewHooks[name](arg);
    renderDock(latest);
  }
  function view() { return current; }

  // ---------- resume banner ----------
  function renderResume(s) {
    var r = s.resume;
    show('resume-banner', !!r && !s.busy);
    if (r) setText('resume-title', r.title + (r.status === 'paused' ? ' (paused)' : ''));
  }

  // ---------- job dock ----------
  var dismissed = null;
  try { dismissed = sessionStorage.getItem('huaepub-dock-dismissed'); } catch (e) { dismissed = null; }
  var KIND_ON_STAGE = { single: true };

  function renderDock(s) {
    var t = s && s.task;
    var onStage = t && current === 'single' && KIND_ON_STAGE[t.kind];
    var finished = t && (t.state === 'done' || t.state === 'error' || t.state === 'cancelled');
    var visible = !!t && !onStage && !(finished && dismissed === t.id);
    show('dock', visible);
    if (!visible) return;
    $('dock').setAttribute('data-state', t.state);
    var phase = t.state === 'done' ? (t.result && t.result.warnings ? 'Saved with warnings' : 'Finished')
      : t.state === 'error' ? 'Failed' : t.state === 'cancelled' ? 'Cancelled' : t.phase_label;
    setText('dock-phase', phase);
    setText('dock-title', t.label);
    var line = t.state === 'error' ? t.error : (finished ? firstLine(t.result && t.result.notes) : t.message);
    if (!finished && t.novels > 1) line = 'Book ' + (t.novel + 1) + ' of ' + t.novels + (line ? ' · ' + line : '');
    setText('dock-line', line || '');
    var n = window.matchMedia('(max-width: 719px)').matches ? 16 : 30;
    var c = slipCounts(t, n);
    drawSlips($('dock-slips'), n, c.fetched, c.done, c.cur);
    var running = !finished;
    show('dock-pause', running && t.kind !== 'lookup' && t.kind !== 'check' && t.kind !== 'install');
    setText('dock-pause', t.state === 'paused' ? 'Resume' : 'Pause');
    show('dock-cancel', running);
    show('dock-close', finished);
    var files = $('dock-files');
    files.textContent = '';
    var list = (finished && t.result && t.result.files) || [];
    list.slice(0, 12).forEach(function (f) {
      var b = el('button', 'btn-link small mono', f.name);
      b.type = 'button';
      b.addEventListener('click', function () { triggerDownload(f); });
      files.appendChild(b);
    });
  }
  function firstLine(text) { return (text || '').split('\n').filter(Boolean)[0] || ''; }

  function pauseTask() { return api('POST', '/api/task/pause').then(refresh); }
  function cancelTask() { return api('POST', '/api/task/cancel').then(refresh); }

  // ---------- look switch ----------
  function buildLookSelect() {
    var sel = $('look-select');
    Object.keys(window.HuaTheme.names).forEach(function (id) {
      var o = el('option', '', window.HuaTheme.names[id]);
      o.value = id;
      sel.appendChild(o);
    });
    sel.value = window.HuaTheme.get();
    sel.addEventListener('change', function () { window.HuaTheme.set(sel.value); });
  }

  function init() {
    buildLookSelect();
    document.querySelectorAll('.nav a').forEach(function (a) {
      a.addEventListener('click', function (ev) { ev.preventDefault(); go(a.getAttribute('data-view')); });
    });
    $('dock-pause').addEventListener('click', pauseTask);
    $('dock-cancel').addEventListener('click', cancelTask);
    $('dock-close').addEventListener('click', function () {
      var t = latest.task;
      dismissed = t ? t.id : null;
      try { sessionStorage.setItem('huaepub-dock-dismissed', dismissed || ''); } catch (e) { /* ignore */ }
      renderDock(latest);
    });
    $('btn-resume').addEventListener('click', function () {
      api('POST', '/api/resume').then(function (res) {
        if (!res.ok) { setText('resume-title', errorText(res.data, '', 'resume this download')); return; }
        refresh();
      });
    });
    $('btn-discard').addEventListener('click', function () {
      api('POST', '/api/resume/discard').then(refresh);
    });
    onState(renderResume);
    onState(renderDock);
  }

  window.Hua = {
    $: $, el: el, setText: setText, show: show, plural: plural,
    api: api, errorText: errorText, ERRORS: ERRORS,
    fileUrl: fileUrl, triggerDownload: triggerDownload,
    drawSlips: drawSlips, slipCounts: slipCounts,
    onState: onState, refresh: refresh, connect: connect, state: function () { return latest; },
    onView: onView, go: go, view: view, VIEWS: VIEWS,
    pauseTask: pauseTask, cancelTask: cancelTask, init: init
  };
})();

// HuaEPUB Simple: one-screen state machine. No framework, no inline script (CSP: script-src 'self').
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var app = $('app');

  var ERRORS = {
    blocked_url: 'Blocked URL. Use a full http(s) link to a public novel page.',
    no_parser: 'No parser handles that site.',
    fetch_failed: 'Could not reach the site.',
    no_chapters: 'No chapters found at that link.',
    busy: 'A book is already being built. Cancel it or wait for it to finish.',
    preview_expired: 'That chapter list expired. Press Look again.',
    bad_range: 'Check the chapter range.',
    access_code_required: 'Open the link printed in the terminal to get in.',
    network: 'Lost contact with HuaEPUB Simple. Is it still running?'
  };

  var preview = null;     // {preview_id, title, author, chapter_count, chapters}
  var jobId = null;
  var lookAbort = null;
  var stream = null;
  var poller = null;
  var fileHandle = null;  // set when the user chose "Choose…"
  var deliveryNote = '';
  var flagged = [];
  var frozen = { fetched: 0, done: 0, cur: -1, step: -1 };
  var state = 'idle';
  var compactMQ = window.matchMedia('(max-width: 719px)');
  var canPick = typeof window.showSaveFilePicker === 'function';

  // ---------- helpers ----------
  function setText(id, text) { $(id).textContent = text; }
  function show(id, on) { $(id).hidden = !on; }

  function api(method, path, body, signal) {
    var opts = { method: method, headers: {}, signal: signal };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (r) {
      return r.text().then(function (t) {
        var data = null;
        try { data = t ? JSON.parse(t) : null; } catch (e) { data = null; }
        return { ok: r.ok, status: r.status, data: data };
      });
    });
  }

  function errorText(data, fallback) {
    var key = data && data.error;
    var text = ERRORS[key] || fallback || 'Something went wrong.';
    if (key === 'fetch_failed' && data.detail) text += ' ' + data.detail;
    return text;
  }

  function range() {
    var n = preview ? preview.chapter_count : 0;
    var from = parseInt($('from').value, 10);
    var to = parseInt($('to').value, 10);
    var ok = n > 0 && from >= 1 && to <= n && from <= to;
    return { from: from, to: to, ok: ok, count: ok ? to - from + 1 : 0 };
  }

  // ---------- slips ----------
  var slipCount = 0;
  function maxSlips() { return compactMQ.matches ? 22 : 44; }

  function ensureSlips(n) {
    var host = $('slips');
    if (slipCount === n && host.firstChild) return;
    slipCount = n;
    host.textContent = '';
    var row = document.createElement('div');
    row.className = 'slips-row';
    for (var i = 0; i < n; i++) {
      var slip = document.createElement('div');
      slip.className = 'slip';
      slip.appendChild(document.createElement('b'));
      slip.appendChild(document.createElement('i'));
      row.appendChild(slip);
    }
    host.appendChild(row);
  }

  // total = chapters in the selected range; fetched/done are strip counts
  function drawSlips(total, fetched, done, cur) {
    var n = Math.min(maxSlips(), total || maxSlips());
    ensureSlips(n);
    var per = total ? Math.ceil(total / n) : 1;
    var flags = {};
    flagged.forEach(function (pos) { flags[Math.min(n - 1, Math.floor(pos / per))] = true; });
    var slips = $('slips').querySelectorAll('.slip');
    for (var i = 0; i < slips.length; i++) {
      var cls = 'slip';
      if (i < done) cls += ' is-done';
      else if (i < fetched) cls += ' is-fetched';
      if (i === cur) cls += ' is-cur';
      if (flags[i]) cls += ' is-flag';
      slips[i].className = cls;
    }
    frozen.fetched = fetched; frozen.done = done; frozen.cur = cur;
  }

  function slipsFor(snap) {
    var total = snap.total || (range().count) || 0;
    var n = Math.min(maxSlips(), total || maxSlips());
    var per = total ? Math.ceil(total / n) : 1;
    if (snap.state === 'fetching') {
      var f = Math.min(n, Math.floor(snap.current / per));
      drawSlips(total, f, 0, f < n ? f : -1);
    } else if (snap.state === 'translating') {
      var d = Math.min(n, Math.floor(snap.fraction * n));
      drawSlips(total, n, d, d < n ? d : -1);
    } else if (snap.state === 'writing' || snap.state === 'done') {
      drawSlips(total, n, n, -1);
    }
  }

  function setSteps(active, complete) {
    var items = $('steps').querySelectorAll('li');
    for (var i = 0; i < items.length; i++) {
      var cls = '';
      if (complete) cls = 'is-past';
      else if (i < active) cls = 'is-past';
      else if (i === active) cls = 'is-now';
      if (complete && i === 3) cls = 'is-now';
      items[i].className = cls;
    }
  }

  // ---------- render ----------
  function set(next, info) {
    info = info || {};
    state = next;
    app.setAttribute('data-state', next);
    var busy = next === 'resolving' || next === 'fetching' || next === 'translating' || next === 'writing';
    var terminal = next === 'done' || next === 'error' || next === 'cancelled';
    var titles = {
      idle: 'Ready when you are.', resolving: 'Reading the page', fetching: 'Fetching chapters',
      translating: 'Translating', writing: 'Writing the EPUB', done: 'Saved', error: 'Failed',
      cancelled: 'Cancelled'
    };
    var title = titles[next] || '';
    var line = info.line || '';
    var mono = false;

    if (next === 'idle') {
      line = 'Paste a novel link. Nothing is downloaded until you press Build.';
    } else if (next === 'resolving') {
      line = 'Loading the chapter list. Long books can take a minute.';
    } else if (next === 'preview') {
      var r = range();
      title = preview.chapter_count + ' chapter' + (preview.chapter_count === 1 ? '' : 's') + ' found.';
      var per = r.ok ? Math.ceil(r.count / Math.min(maxSlips(), r.count)) : 1;
      line = r.ok
        ? 'Each strip is about ' + per + (per === 1 ? ' chapter' : ' chapters') + '. Change the range on the left, or build the whole book.'
        : 'Check the chapter range on the left.';
    } else if (busy) {
      mono = true;
    } else if (next === 'done') {
      if (info.warnings) title = 'Saved with warnings';
      mono = true;
      line = info.summary || '';
    } else if (next === 'error') {
      line = info.line || ERRORS.network;
    } else if (next === 'cancelled') {
      line = info.line || 'Stopped before the EPUB was written. Nothing was saved. Fetched chapters stay cached.';
    }

    setText('title', title);
    setText('status', line);
    $('status').classList.toggle('mono', mono);
    show('seal', next === 'done');

    // rail
    var hasBook = !!preview && next !== 'idle' && next !== 'resolving';
    show('book-empty', !hasBook);
    show('book-full', hasBook);
    $('url').disabled = !(next === 'idle' || next === 'preview');
    $('btn-look').disabled = $('url').disabled;
    $('from').disabled = $('to').disabled = next !== 'preview';
    document.querySelectorAll('input[name="save"]').forEach(function (el) { el.disabled = busy || terminal; });
    $('sec-book').classList.toggle('is-dim', busy || terminal);
    $('sec-output').classList.toggle('is-dim', busy || terminal);
    show('sec-saved', next === 'done');
    show('btn-build', next === 'preview');
    show('btn-cancel', busy);
    show('btn-again', next === 'done');
    show('btn-restart', terminal);
    setText('btn-restart', next === 'error' ? 'Try again' : 'Start over');
    $('btn-build').disabled = next === 'preview' && !range().ok;

    // slips and steps
    if (next === 'idle') { flagged = []; drawSlips(0, 0, 0, -1); setSteps(-1, false); }
    else if (next === 'resolving') { drawSlips(0, 0, 0, -1); setSteps(-1, false); }
    else if (next === 'preview') { flagged = []; drawSlips(range().count, 0, 0, -1); setSteps(-1, false); }
    else if (next === 'fetching') setSteps(0, false);
    else if (next === 'translating') setSteps(1, false);
    else if (next === 'writing') setSteps(2, false);
    else if (next === 'done') setSteps(3, true);
    else if (next === 'error' || next === 'cancelled') {
      drawSlips(info.total || range().count, frozen.fetched, frozen.done, -1);
    }
    if (next === 'fetching') frozen.step = 0;
    if (next === 'translating') frozen.step = 1;
    if (next === 'writing') frozen.step = 2;
    if ((next === 'error' || next === 'cancelled') && frozen.step >= 0) setSteps(frozen.step, false);
    if ((next === 'error' || next === 'cancelled') && frozen.step < 0) setSteps(-1, false);
  }

  function fillBook() {
    setText('book-title', preview.title);
    setText('book-meta', (preview.author || 'Unknown author') + ' · ' + preview.chapter_count +
      ' chapter' + (preview.chapter_count === 1 ? '' : 's'));
    $('from').value = '1';
    $('to').value = String(preview.chapter_count);
    rangeChanged();
  }

  function rangeChanged() {
    if (!preview) return;
    var r = range();
    var fromT = r.ok || (r.from >= 1 && r.from <= preview.chapter_count) ? preview.chapters[r.from - 1] : null;
    var toT = r.to >= 1 && r.to <= preview.chapter_count ? preview.chapters[r.to - 1] : null;
    setText('from-title', fromT ? fromT.title : '');
    setText('to-title', toT ? toT.title : '');
    var bad = !r.ok;
    show('range-error', bad);
    if (bad) setText('range-error', 'Use numbers from 1 to ' + preview.chapter_count + ', with From not above To.');
    if (state === 'preview') set('preview');
  }

  // ---------- actions ----------
  function look(ev) {
    if (ev) ev.preventDefault();
    if (state !== 'idle' && state !== 'preview') return;
    var url = $('url').value.trim();
    if (!/^https?:\/\/[^\s\/]+\.[^\s\/]+/i.test(url)) {
      preview = null;
      set('error', { line: ERRORS.blocked_url });
      return;
    }
    preview = null;
    set('resolving');
    lookAbort = new AbortController();
    api('POST', '/api/preview', { url: url }, lookAbort.signal).then(function (res) {
      lookAbort = null;
      if (!res.ok) { set('error', { line: errorText(res.data, ERRORS.fetch_failed) }); return; }
      preview = res.data;
      fillBook();
      set('preview');
    }).catch(function (err) {
      lookAbort = null;
      if (err && err.name === 'AbortError') return;
      set('error', { line: ERRORS.network });
    });
  }

  function build() {
    var r = range();
    if (!preview || !r.ok) { rangeChanged(); return; }
    var wantsPicker = canPick && document.querySelector('input[name="save"]:checked').value === 'choose';
    var picked = wantsPicker
      ? window.showSaveFilePicker({
          suggestedName: suggestedName(r),
          types: [{ description: 'EPUB book', accept: { 'application/epub+zip': ['.epub'] } }]
        })
      : Promise.resolve(null);
    // The picker must start inside this click; everything else waits for it.
    picked.then(function (handle) {
      fileHandle = handle;
      return api('POST', '/api/jobs', {
        preview_id: preview.preview_id, chapter_from: r.from, chapter_to: r.to
      });
    }).then(function (res) {
      if (!res.ok) { set('error', { line: errorText(res.data) }); return; }
      jobId = res.data.job_id;
      flagged = [];
      frozen = { fetched: 0, done: 0, cur: -1, step: -1 };
      set('fetching', { total: r.count });
      setText('status', 'Starting…');
      listen(jobId);
    }).catch(function (err) {
      if (err && err.name === 'AbortError') {
        set('preview', {});
        setText('status', 'Save cancelled. Press Build EPUB to choose again.');
        return;
      }
      set('error', { line: ERRORS.network });
    });
  }

  function suggestedName(r) {
    var base = (preview.title || 'book').replace(/[\\/:*?"<>|]+/g, '').trim() || 'book';
    var part = (r.from === 1 && r.to === preview.chapter_count) ? '' : ' (' + r.from + '-' + r.to + ')';
    return base + part + '.epub';
  }

  function cancel() {
    if (state === 'resolving') {
      if (lookAbort) lookAbort.abort();
      lookAbort = null;
      set('cancelled', { line: 'Stopped. Nothing was downloaded.' });
      return;
    }
    if (!jobId) return;
    $('btn-cancel').disabled = true;
    api('POST', '/api/jobs/' + jobId + '/cancel').then(function (res) {
      $('btn-cancel').disabled = false;
      if (res.status === 409) pollOnce();
    }).catch(function () { $('btn-cancel').disabled = false; });
  }

  function restart() {
    stopListening();
    var old = jobId;
    jobId = null;
    fileHandle = null;
    if (old) api('DELETE', '/api/jobs/' + old).catch(function () {});
    if (preview) { set('preview'); } else { set('idle'); }
    if (state === 'idle') $('url').focus();
  }

  // ---------- job updates ----------
  function listen(id) {
    stopListening();
    if (window.EventSource) {
      stream = new EventSource('/api/jobs/' + id + '/events');
      stream.onmessage = function (ev) { try { onSnapshot(JSON.parse(ev.data)); } catch (e) { /* ignore */ } };
      stream.onerror = function () {
        // Fall back to polling; a finished job closes the stream on its own.
        if (stream) { stream.close(); stream = null; }
        startPolling(id);
      };
    } else {
      startPolling(id);
    }
  }

  function startPolling(id) {
    if (poller) return;
    poller = setInterval(function () { pollOnce(id); }, 1000);
  }

  function pollOnce(id) {
    id = id || jobId;
    if (!id) return;
    api('GET', '/api/jobs/' + id).then(function (res) {
      if (res.ok) onSnapshot(res.data);
    }).catch(function () {});
  }

  function stopListening() {
    if (stream) { stream.close(); stream = null; }
    if (poller) { clearInterval(poller); poller = null; }
  }

  function onSnapshot(s) {
    if (s.job_id !== jobId) return;
    flagged = s.flagged || [];
    var mid = s.state === 'fetching' || s.state === 'translating' || s.state === 'writing';
    if (mid) {
      if (state !== s.state) set(s.state, { total: s.total });
      slipsFor(s);
      setText('status', statusLine(s));
      return;
    }
    stopListening();
    if (s.state === 'done') {
      slipsFor(s);
      var summary = s.total + ' chapter' + (s.total === 1 ? '' : 's');
      set('done', { warnings: s.warnings, summary: summary });
      slipsFor(s);
      setText('file-name', s.filename || 'book.epub');
      setText('file-note', 'Sending the file to your browser…');
      setText('notes', s.notes || '');
      show('notes-wrap', !!s.notes);
      deliver(s);
    } else if (s.state === 'cancelled') {
      set('cancelled', { total: s.total });
    } else if (s.state === 'error') {
      set('error', { line: s.error ? 'Could not finish: ' + s.error : ERRORS.network, total: s.total });
    }
  }

  function statusLine(s) {
    var head = s.state === 'fetching'
      ? s.current + ' of ' + s.total + ' chapters'
      : Math.round(s.fraction * 100) + '%';
    var msg = (s.message || '').replace(/\s+/g, ' ').trim();
    return msg ? head + ' · ' + msg : head;
  }

  // ---------- delivery ----------
  function triggerDownload(s) {
    var a = document.createElement('a');
    a.href = '/api/jobs/' + s.job_id + '/epub';
    a.download = s.filename || 'book.epub';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function deliver(s) {
    if (fileHandle) {
      fetch('/api/jobs/' + s.job_id + '/epub')
        .then(function (r) { if (!r.ok) throw new Error('fetch'); return r.blob(); })
        .then(function (blob) {
          return fileHandle.createWritable().then(function (w) {
            return w.write(blob).then(function () { return w.close(); });
          });
        })
        .then(function () { setText('file-note', 'Written to the file you chose.'); })
        .catch(function () {
          setText('file-note', 'Could not write that file. Press Download EPUB again.');
        });
    } else {
      triggerDownload(s);
      setText('file-note', 'Sent to your Downloads folder. Your browser may have asked first.');
    }
  }

  function downloadAgain() {
    if (!jobId) return;
    triggerDownload({ job_id: jobId, filename: $('file-name').textContent });
  }

  // ---------- look switch ----------
  function buildLookMenu() {
    var menu = $('look-menu');
    var order = ['auto', 'dark', 'light', '-', 'indigo', 'gold', 'cinnabar', 'mist', '-', 'surprise'];
    var current = window.HuaTheme ? window.HuaTheme.get() : 'auto';
    menu.textContent = '';
    order.forEach(function (id) {
      var li = document.createElement('li');
      if (id === '-') { li.appendChild(document.createElement('hr')); menu.appendChild(li); return; }
      var b = document.createElement('button');
      b.type = 'button';
      b.setAttribute('role', 'menuitemradio');
      b.setAttribute('aria-checked', id === current ? 'true' : 'false');
      b.textContent = window.HuaTheme.names[id];
      b.addEventListener('click', function () {
        window.HuaTheme.set(id);
        setText('look-btn', 'Look: ' + window.HuaTheme.names[id]);
        closeMenu();
        buildLookMenu();
      });
      li.appendChild(b);
      menu.appendChild(li);
    });
    setText('look-btn', 'Look: ' + window.HuaTheme.names[current]);
  }
  function closeMenu() { show('look-menu', false); $('look-btn').setAttribute('aria-expanded', 'false'); }
  function toggleMenu() {
    var open = $('look-menu').hidden;
    show('look-menu', open);
    $('look-btn').setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) { var first = $('look-menu').querySelector('button'); if (first) first.focus(); }
  }

  // ---------- save choice ----------
  function initSave() {
    var note = $('save-note');
    if (!canPick) {
      show('save-choose-wrap', false);
      note.textContent = 'This browser saves to Downloads.';
      return;
    }
    note.textContent = 'Downloads is your browser’s own download folder.';
    var saved = null;
    try { saved = localStorage.getItem('huaepub-simple-save'); } catch (e) { saved = null; }
    if (saved === 'choose') document.querySelector('input[name="save"][value="choose"]').checked = true;
    document.querySelectorAll('input[name="save"]').forEach(function (el) {
      el.addEventListener('change', function () {
        var v = document.querySelector('input[name="save"]:checked').value;
        try { localStorage.setItem('huaepub-simple-save', v); } catch (e) { /* ignore */ }
        note.textContent = v === 'choose'
          ? 'Build EPUB asks where to save first.'
          : 'Downloads is your browser’s own download folder.';
      });
    });
  }

  // ---------- wiring ----------
  $('look-form').addEventListener('submit', look);
  $('btn-build').addEventListener('click', build);
  $('btn-cancel').addEventListener('click', cancel);
  $('btn-restart').addEventListener('click', restart);
  $('btn-again').addEventListener('click', downloadAgain);
  $('from').addEventListener('input', rangeChanged);
  $('to').addEventListener('input', rangeChanged);
  $('look-btn').addEventListener('click', toggleMenu);
  document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') { closeMenu(); $('look-btn').focus(); } });
  document.addEventListener('click', function (ev) {
    if (!ev.target.closest('.look')) closeMenu();
  });
  compactMQ.addEventListener('change', function () {
    slipCount = 0;
    if (state === 'idle' || state === 'resolving') drawSlips(0, 0, 0, -1);
    else if (state === 'preview') drawSlips(range().count, 0, 0, -1);
    else drawSlips(range().count, frozen.fetched, frozen.done, frozen.cur);
  });

  buildLookMenu();
  initSave();
  set('idle');
  fetch('/api/health').then(function (r) { return r.json(); }).then(function (h) {
    setText('version', h.version || '');
    if (h.name) document.title = h.name;
  }).catch(function () {});
})();

// Single: look up one novel, pick a range, build it into the Library, copy the EPUB to this device.
(function () {
  'use strict';
  var H = window.Hua, $ = H.$, setText = H.setText, show = H.show;
  var root = $('view-single');

  var preview = null;      // payload from POST /api/preview
  var lookAbort = null;
  var myTask = null;       // task id started from this tab
  var seenTask = null;     // last task id drawn on the stage
  var hiddenTask = null;   // a finished task the user cleared with Start over
  var delivered = {};      // task id -> true once the file went to this device
  var fileHandle = null;
  var lastFiles = [];
  var state = 'idle';
  var localLine = '';
  var compactMQ = window.matchMedia('(max-width: 719px)');
  var canPick = typeof window.showSaveFilePicker === 'function';

  function maxSlips() { return compactMQ.matches ? 22 : 44; }

  function range() {
    var n = preview ? preview.chapter_count : 0;
    var from = parseInt($('from').value, 10);
    var to = parseInt($('to').value, 10);
    var ok = n > 0 && from >= 1 && to <= n && from <= to;
    return { from: from, to: to, ok: ok, count: ok ? to - from + 1 : 0 };
  }

  function setSteps(active, complete) {
    var items = $('steps').querySelectorAll('li');
    for (var i = 0; i < items.length; i++) {
      var cls = '';
      if (complete) cls = i === 3 ? 'is-now' : 'is-past';
      else if (i < active) cls = 'is-past';
      else if (i === active) cls = 'is-now';
      items[i].className = cls;
    }
  }

  function flagsFor(total, n, positions) {
    var flags = {};
    var per = total ? Math.ceil(total / n) : 1;
    (positions || []).forEach(function (pos) { flags[Math.min(n - 1, Math.floor(pos / per))] = true; });
    return flags;
  }

  var STEP = { fetching: 0, translating: 1, polishing: 1, writing: 2 };
  var TITLES = {
    idle: 'Ready when you are.', resolving: 'Reading the page', fetching: 'Fetching chapters',
    translating: 'Translating', polishing: 'Polishing English', writing: 'Writing the EPUB',
    paused: 'Paused', starting: 'Starting', done: 'Saved', error: 'Failed', cancelled: 'Cancelled'
  };

  // Render the stage + rail. `task` is the live single task (or null).
  // "You shelved “Title” on 9 Oct 2026." (same_link false: matched by title on another site)
  function shelvedLine(note) {
    var when = '';
    try {
      if (note.at) when = new Date(note.at * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    } catch (err) { when = ''; }
    return 'You shelved “' + note.title + '”' + (when ? ' on ' + when : '') +
      (note.same_link ? '' : ', the same title on another site') + '.';
  }

  function render(task) {
    var s = state;
    var busy = s === 'resolving' || s === 'running';
    var terminal = s === 'done' || s === 'error' || s === 'cancelled';
    root.setAttribute('data-state', s === 'running' ? (task ? task.phase : 'fetching') : s);
    var title = TITLES[s] || '';
    var line = localLine;
    var mono = false;
    var n = maxSlips();

    if (s === 'idle') {
      line = 'Paste a novel link. Nothing is downloaded until you press Build.';
      H.drawSlips($('slips'), n, 0, 0, -1);
      setSteps(-1, false);
    } else if (s === 'resolving') {
      line = 'Loading the chapter list. Long books can take a minute.';
      H.drawSlips($('slips'), n, 0, 0, -1);
      setSteps(-1, false);
    } else if (s === 'preview') {
      var r = range();
      title = H.plural(preview.chapter_count, 'chapter') + ' found.';
      var count = Math.min(n, r.count || n);
      var per = r.ok ? Math.ceil(r.count / count) : 1;
      line = r.ok
        ? 'Each strip is about ' + H.plural(per, 'chapter') + '. Change the range, or build the whole book.'
        : 'Check the chapter range.';
      if (preview.in_library) line += ' This book is already in your Library; building replaces its EPUB.';
      if (preview.shelved) {
        title = 'You shelved this book.';
        line = shelvedLine(preview.shelved) + ' Build it only if you want to give it another go.';
      }
      H.drawSlips($('slips'), count, 0, 0, -1);
      setSteps(-1, false);
    } else if (task) {
      var total = task.chapters || 0;
      var count2 = Math.min(n, total || n);
      var c = H.slipCounts(task, count2);
      var flags = flagsFor(total, count2, task.result && task.result.flagged);
      if (s === 'running') {
        title = TITLES[task.phase] || task.phase_label || 'Working';
        mono = true;
        var head = task.phase === 'fetching'
          ? Math.round((task.fetched || 0) * total) + ' of ' + H.plural(total, 'chapter')
          : Math.round((task.fraction || 0) * 100) + '%';
        var msg = (task.message || '').replace(/\s+/g, ' ').trim();
        line = msg ? head + ' · ' + msg : head;
        setSteps(STEP[task.phase] !== undefined ? STEP[task.phase] : 0, false);
        H.drawSlips($('slips'), count2, c.fetched, c.done, c.cur, flags);
      } else if (s === 'done') {
        title = task.result && task.result.warnings ? 'Saved with warnings' : 'Saved';
        mono = true;
        line = H.plural(total, 'chapter') + ' · in your Library on the PC';
        setSteps(3, true);
        H.drawSlips($('slips'), count2, count2, count2, -1, flags);
      } else {
        line = s === 'error' ? 'Could not finish: ' + (task.error || 'unknown error')
          : 'Stopped before the EPUB was written. Fetched chapters stay cached.';
        H.drawSlips($('slips'), count2, c.fetched, c.done, -1, flags);
      }
    } else if (s === 'error' || s === 'cancelled') {
      H.drawSlips($('slips'), n, 0, 0, -1);
      setSteps(-1, false);
    }

    setText('title', title);
    setText('status', line);
    $('status').classList.toggle('mono', mono);
    show('seal', s === 'done' && !!(task && task.result && task.result.translated));

    var hasBook = !!preview && s !== 'idle' && s !== 'resolving';
    var foreign = !preview && !!task;
    show('book-empty', !hasBook && !foreign);
    show('book-full', hasBook || foreign);
    if (foreign) {
      setText('book-title', task.label);
      show('book-title-orig', false);
      setText('book-meta', 'Started from another device');
      show('book-cover', false);
    }
    show('btn-read-preview', hasBook);
    show($('book-full').querySelector('.range'), hasBook);
    $('url').disabled = !(s === 'idle' || s === 'preview' || s === 'error' || s === 'cancelled');
    $('btn-look').disabled = $('url').disabled;
    $('from').disabled = $('to').disabled = s !== 'preview';
    document.querySelectorAll('input[name="save"]').forEach(function (e) { e.disabled = busy; });
    $('sec-book').classList.toggle('is-dim', busy || terminal);
    show('sec-saved', s === 'done');
    show('btn-build', s === 'preview');
    show('run-actions', s === 'running');
    if (task && s === 'running') setText('btn-pause', task.state === 'paused' ? 'Resume' : 'Pause');
    show('btn-again', s === 'done' && lastFiles.length > 0);
    show('btn-read-done', s === 'done');
    show('btn-restart', terminal || (s === 'preview'));
    setText('btn-restart', s === 'preview' ? 'Clear' : (s === 'error' ? 'Try again' : 'Start over'));
    $('btn-build').disabled = s === 'preview' && (!range().ok || H.state().busy);
  }

  function fillBook() {
    var title = preview.title_en || preview.title;
    var author = preview.author_en || preview.author || 'Unknown author';
    setText('book-title', title);
    var showOrig = !!preview.title_en && preview.title_en !== preview.title;
    show('book-title-orig', showOrig);
    if (showOrig) setText('book-title-orig', preview.title);
    setText('book-meta', author + ' · ' + H.plural(preview.chapter_count, 'chapter'));
    var cover = $('book-cover');
    if (preview.has_cover) {
      cover.src = '/api/preview/' + preview.preview_id + '/cover';
      cover.hidden = false;
      cover.onerror = function () { cover.hidden = true; };
    } else {
      cover.hidden = true;
      cover.removeAttribute('src');
    }
    $('from').value = '1';
    $('to').value = String(preview.chapter_count);
    rangeChanged();
  }

  function chapterTitle(pos) {
    var ch = preview && pos >= 1 && pos <= preview.chapter_count ? preview.chapters[pos - 1] : null;
    return ch ? (ch.title_en || ch.title) : '';
  }

  function rangeChanged() {
    if (!preview) return;
    var r = range();
    setText('from-title', chapterTitle(r.from));
    setText('to-title', chapterTitle(r.to));
    show('range-error', !r.ok);
    if (!r.ok) setText('range-error', 'Use numbers from 1 to ' + preview.chapter_count + ', with From not above To.');
    if (state === 'preview') render(null);
  }

  // ---------- actions ----------
  function look(ev) {
    if (ev) ev.preventDefault();
    var url = $('url').value.trim();
    if (!/^https?:\/\/[^\s\/]+\.[^\s\/]+/i.test(url)) {
      preview = null; state = 'error'; localLine = H.ERRORS.blocked_url; render(null);
      return;
    }
    preview = null; state = 'resolving'; localLine = ''; render(null);
    lookAbort = new AbortController();
    H.api('POST', '/api/preview', { url: url }, lookAbort.signal).then(function (res) {
      lookAbort = null;
      if (!res.ok) { state = 'error'; localLine = H.errorText(res.data, H.ERRORS.fetch_failed, 'look up this novel'); render(null); return; }
      preview = res.data;
      state = 'preview';
      fillBook();
      render(null);
    }).catch(function (err) {
      lookAbort = null;
      if (err && err.name === 'AbortError') return;
      state = 'error'; localLine = H.ERRORS.network; render(null);
    });
  }

  function saveChoice() {
    var c = document.querySelector('input[name="save"]:checked');
    return c ? c.value : 'downloads';
  }

  function suggestedName() {
    var base = ((preview.title_en || preview.title) || 'book').replace(/[\\/:*?"<>|]+/g, '').trim() || 'book';
    return base + '.epub';
  }

  function build() {
    var r = range();
    if (!preview || !r.ok) { rangeChanged(); return; }
    var picked = canPick && saveChoice() === 'choose'
      ? window.showSaveFilePicker({
          suggestedName: suggestedName(),
          types: [{ description: 'EPUB book', accept: { 'application/epub+zip': ['.epub'] } }]
        })
      : Promise.resolve(null);
    // The picker must open inside this click; the request waits for it.
    picked.then(function (handle) {
      fileHandle = handle;
      return H.api('POST', '/api/single', {
        preview_id: preview.preview_id, chapter_from: r.from, chapter_to: r.to
      });
    }).then(function (res) {
      if (!res.ok) { state = 'error'; localLine = H.errorText(res.data, '', 'download this book'); render(null); return; }
      myTask = res.data.task_id;
      hiddenTask = null;
      state = 'running';
      render({ id: myTask, state: 'running', phase: 'starting', phase_label: 'Starting', chapters: r.count,
               fetched: 0, built: 0, fraction: 0, message: 'Starting…', result: {} });
      H.refresh();
    }).catch(function (err) {
      if (err && err.name === 'AbortError') {
        state = 'preview'; render(null);
        setText('status', 'Save cancelled. Press Build EPUB to choose again.');
        return;
      }
      state = 'error'; localLine = H.ERRORS.network; render(null);
    });
  }

  function restart() {
    var t = H.state().task;
    if (t) hiddenTask = t.id;
    fileHandle = null;
    lastFiles = [];
    if (state === 'preview') { preview = null; $('url').value = ''; }
    state = preview ? 'preview' : 'idle';
    localLine = '';
    render(null);
    if (state === 'idle') $('url').focus();
  }

  function deliver(task) {
    if (delivered[task.id]) return;
    delivered[task.id] = true;
    var files = (task.result && task.result.files) || [];
    lastFiles = files;
    var file = files[0];
    setText('file-name', file ? file.name : 'Saved on the PC');
    var notes = (task.result && task.result.notes) || '';
    setText('notes', notes);
    show('notes-wrap', !!notes);
    if (!file) { setText('file-note', 'Saved in the books folder on the PC.'); return; }
    var mine = task.id === myTask;
    var choice = saveChoice();
    if (!mine || choice === 'none') {
      setText('file-note', 'Saved in the books folder on the PC. Use Download EPUB to copy it here.');
      return;
    }
    if (fileHandle) {
      fetch(H.fileUrl(file), { credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('fetch'); return r.blob(); })
        .then(function (blob) {
          return fileHandle.createWritable().then(function (w) {
            return w.write(blob).then(function () { return w.close(); });
          });
        })
        .then(function () { setText('file-note', 'Saved on the PC and written to the file you chose.'); })
        .catch(function () { setText('file-note', 'Could not write that file. Press Download EPUB again.'); });
    } else {
      H.triggerDownload(file);
      setText('file-note', 'Saved on the PC and sent to this device’s Downloads folder.');
    }
  }

  // ---------- live state ----------
  function onState(s) {
    var t = s.task;
    var single = t && t.kind === 'single' ? t : null;
    if (single && single.id === hiddenTask) single = null;
    if (single) {
      var finished = single.state === 'done' || single.state === 'error' || single.state === 'cancelled';
      if (!finished) {
        state = 'running';
      } else if (seenTask === single.id || single.id === myTask) {
        state = single.state;
        if (single.state === 'done') deliver(single);
      } else {
        single = null;  // an old finished job from before this page loaded
      }
      if (single) seenTask = single.id;
    } else if (state === 'running') {
      state = preview ? 'preview' : 'idle';
    }
    if (single || state !== 'running') render(single);
  }

  function initSave() {
    var note = $('save-note');
    var saved = null;
    try { saved = localStorage.getItem('huaepub-save'); } catch (e) { saved = null; }
    if (!canPick) show('save-choose-wrap', false);
    if (saved === 'choose' && !canPick) saved = 'downloads';
    var input = document.querySelector('input[name="save"][value="' + (saved || 'downloads') + '"]');
    if (input) input.checked = true;
    function describe() {
      var v = saveChoice();
      note.textContent = v === 'choose' ? 'Build EPUB asks where to save on this device first.'
        : v === 'none' ? 'The EPUB stays on the PC. You can download it later from Library.'
        : 'A copy goes to this device’s Downloads folder.';
    }
    describe();
    document.querySelectorAll('input[name="save"]').forEach(function (e) {
      e.addEventListener('change', function () {
        try { localStorage.setItem('huaepub-save', saveChoice()); } catch (err) { /* ignore */ }
        describe();
      });
    });
  }

  // ---------- recent ----------
  function toggleRecent() {
    var menu = $('recent-menu');
    if (!menu.hidden) { menu.hidden = true; $('btn-recent').setAttribute('aria-expanded', 'false'); return; }
    H.api('GET', '/api/library/history').then(function (res) {
      menu.textContent = '';
      var items = (res.ok && res.data && res.data.items) || [];
      if (!items.length) { menu.appendChild(H.el('li', 'muted small', 'No downloads yet.')); }
      items.forEach(function (it) {
        var li = document.createElement('li');
        var b = H.el('button', '', it.title || it.url);
        b.type = 'button';
        b.addEventListener('click', function () {
          $('url').value = it.url;
          menu.hidden = true;
          $('btn-recent').setAttribute('aria-expanded', 'false');
          $('url').focus();
        });
        li.appendChild(b);
        menu.appendChild(li);
      });
      menu.hidden = false;
      $('btn-recent').setAttribute('aria-expanded', 'true');
    });
  }

  // ---------- wiring ----------
  $('look-form').addEventListener('submit', look);
  $('btn-build').addEventListener('click', build);
  $('btn-pause').addEventListener('click', H.pauseTask);
  $('btn-cancel').addEventListener('click', function () {
    if (state === 'resolving' && lookAbort) {
      lookAbort.abort(); lookAbort = null; state = 'cancelled'; localLine = ''; render(null); return;
    }
    H.cancelTask();
  });
  $('btn-restart').addEventListener('click', restart);
  $('btn-again').addEventListener('click', function () { if (lastFiles[0]) H.triggerDownload(lastFiles[0]); });
  $('btn-read-preview').addEventListener('click', function () { if (preview) H.go('read', { preview_id: preview.preview_id }); });
  $('btn-read-done').addEventListener('click', function () {
    if (preview) H.go('read', { preview_id: preview.preview_id }); else H.go('read');
  });
  $('btn-recent').addEventListener('click', toggleRecent);
  document.addEventListener('click', function (ev) {
    if (!ev.target.closest('#sec-source')) { show('recent-menu', false); $('btn-recent').setAttribute('aria-expanded', 'false'); }
  });
  $('from').addEventListener('input', rangeChanged);
  $('to').addEventListener('input', rangeChanged);
  compactMQ.addEventListener('change', function () { render(H.state().task && H.state().task.kind === 'single' ? H.state().task : null); });

  initSave();
  render(null);
  H.onState(onState);
  H.onView('single', function (arg) { if (arg && arg.url) { $('url').value = arg.url; $('url').focus(); } });
  window.HuaSingle = { setTranslator: function (label) { setText('out-translator', label); } };
})();

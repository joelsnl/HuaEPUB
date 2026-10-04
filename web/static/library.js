// Library: tracked books, new-chapter checks, updates, Download EPUB, Remove.
(function () {
  'use strict';
  var H = window.Hua, $ = H.$, setText = H.setText, show = H.show;

  var entries = [];
  var selected = {};      // url -> true
  var filter = 'all';
  var lastKey = '';
  var loading = false;
  var pendingRemove = null;

  function load() {
    if (loading) return;
    loading = true;
    H.api('GET', '/api/library').then(function (res) {
      loading = false;
      if (!res.ok) return;
      entries = res.data.entries || [];
      var known = {};
      entries.forEach(function (e) { known[e.url] = true; });
      Object.keys(selected).forEach(function (u) { if (!known[u]) delete selected[u]; });
      draw();
    }).catch(function () { loading = false; });
  }

  function visible() {
    return entries.filter(function (e) { return filter === 'all' || e.status === 'update'; });
  }

  function selection() { return entries.filter(function (e) { return selected[e.url]; }); }

  function badge(e) {
    if (e.status === 'update') return { text: '+' + e.new_count + ' new', cls: 'is-new' };
    if (e.status === 'checking') return { text: 'Checking…', cls: 'is-wait' };
    if (e.status === 'error') return { text: 'Check failed', cls: 'is-err' };
    if (e.status === 'current') return { text: 'Up to date', cls: 'is-ok' };
    return null;
  }

  function draw() {
    var shelf = $('shelf');
    shelf.textContent = '';
    var list = visible();
    show('shelf-empty', entries.length === 0);
    list.forEach(function (e) {
      var li = H.el('li', 'tile' + (selected[e.url] ? ' is-sel' : ''));
      var btn = H.el('button', 'tile-btn');
      btn.type = 'button';
      btn.setAttribute('aria-pressed', selected[e.url] ? 'true' : 'false');
      var cover = H.el('span', 'tile-cover');
      if (e.has_cover) {
        var img = document.createElement('img');
        img.alt = '';
        img.loading = 'lazy';
        img.src = '/api/library/cover?u=' + encodeURIComponent(e.url);
        img.onerror = function () { img.remove(); };
        cover.appendChild(img);
      }
      cover.appendChild(H.el('span', 'tile-glyph', (e.title || '?').trim().charAt(0)));
      btn.appendChild(cover);
      var text = H.el('span', 'tile-text');
      text.appendChild(H.el('span', 'tile-title', e.title));
      if (e.title_original) text.appendChild(H.el('span', 'muted small', e.title_original));
      var meta = [e.author, H.plural(e.chapters, 'chapter')].filter(Boolean).join(' · ');
      text.appendChild(H.el('span', 'muted small', meta));
      var b = badge(e);
      if (b) text.appendChild(H.el('span', 'tile-badge ' + b.cls, b.text));
      if (!e.has_epub) text.appendChild(H.el('span', 'muted small', e.on_drive ? 'EPUB on Drive' : 'No EPUB on the PC'));
      btn.appendChild(text);
      btn.addEventListener('click', function () {
        if (selected[e.url]) delete selected[e.url]; else selected[e.url] = true;
        draw();
      });
      btn.addEventListener('dblclick', function () { H.go('read', { url: e.url }); });
      li.appendChild(btn);
      shelf.appendChild(li);
    });
    var sel = selection();
    var n = sel.length;
    setText('lib-count', n + ' selected');
    var busy = H.state().busy;
    $('lib-read').disabled = n !== 1;
    $('lib-open').disabled = n !== 1;
    $('lib-update').disabled = busy || n === 0;
    setText('lib-update', n > 1 ? 'Update (' + n + ')' : 'Update');
    $('lib-epub').disabled = n === 0;
    $('lib-remove').disabled = busy || n === 0;
    setText('lib-remove', n > 1 ? 'Remove (' + n + ')' : 'Remove');
    var withUpdates = entries.filter(function (e) { return e.status === 'update'; }).length;
    $('lib-update-all').disabled = busy || !withUpdates;
    setText('lib-update-all', withUpdates ? 'Update all (' + withUpdates + ')' : 'Update all');
    $('lib-check').disabled = busy || !entries.length;
    setText('lib-lede', entries.length
      ? H.plural(entries.length, 'book') + (withUpdates ? ' · ' + withUpdates + ' with new chapters' : '')
      : 'Books you have downloaded on this PC.');
  }

  function post(path, body, after) {
    return H.api('POST', path, body).then(function (res) {
      if (!res.ok) { setText('lib-lede', H.errorText(res.data)); return null; }
      H.refresh();
      if (after) after(res.data);
      return res.data;
    }).catch(function () { setText('lib-lede', H.ERRORS.network); });
  }

  function askRemove() {
    var sel = selection();
    if (!sel.length) return;
    pendingRemove = sel.map(function (e) { return e.url; });
    var names = sel.slice(0, 6).map(function (e) { return '“' + e.title + '”'; }).join(', ');
    if (sel.length > 6) names += ' and ' + (sel.length - 6) + ' more';
    setText('lib-confirm-text', 'Remove ' + names + ' from your library? This deletes the EPUB in the books folder on the PC, ' +
      'the cached chapters and cover, and the reading position. If Google Drive sync is on, the Drive copy goes too.');
    setText('lib-confirm-yes', sel.length > 1 ? 'Remove ' + sel.length + ' books' : 'Remove');
    show('lib-confirm', true);
    $('lib-confirm-no').focus();
  }

  function askReset() {
    pendingRemove = 'reset';
    setText('lib-confirm-text', 'Clear every tracked book from the Library? EPUB files on the PC are kept, and the books will not come back on Drive sync.');
    setText('lib-confirm-yes', 'Reset library');
    show('lib-confirm', true);
    $('lib-confirm-no').focus();
  }

  function confirmYes() {
    var what = pendingRemove;
    pendingRemove = null;
    show('lib-confirm', false);
    if (what === 'reset') { post('/api/library/reset', {}, function () { selected = {}; load(); }); return; }
    if (what) post('/api/library/remove', { urls: what }, function () { selected = {}; load(); });
  }

  function downloadSelected() {
    var sel = selection();
    var missing = 0;
    var chain = Promise.resolve();
    sel.forEach(function (e) {
      chain = chain.then(function () {
        return H.api('POST', '/api/library/epub', { url: e.url }).then(function (res) {
          if (res.ok && res.data && res.data.file) H.triggerDownload(res.data.file);
          else missing += 1;
        });
      });
    });
    chain.then(function () {
      if (missing) setText('lib-lede', H.plural(missing, 'book') + ' had no EPUB on the PC or in Google Drive.');
    });
  }

  function onState(s) {
    var t = s.task;
    var key = t ? t.id + ':' + t.state + ':' + (t.kind === 'check' ? t.rev : '') : 'none';
    if (key !== lastKey) {
      lastKey = key;
      if (H.view() === 'library') load();
    } else if (H.view() === 'library') {
      draw();
    }
  }

  document.querySelectorAll('input[name="lib-filter"]').forEach(function (e) {
    e.addEventListener('change', function () { filter = e.value; draw(); });
  });
  $('lib-check').addEventListener('click', function () { post('/api/library/check', {}); });
  $('lib-update-all').addEventListener('click', function () { post('/api/library/update-all', {}); });
  $('lib-update').addEventListener('click', function () {
    post('/api/library/update', { urls: selection().map(function (e) { return e.url; }) });
  });
  $('lib-read').addEventListener('click', function () { var s = selection()[0]; if (s) H.go('read', { url: s.url }); });
  $('lib-open').addEventListener('click', function () { var s = selection()[0]; if (s) H.go('single', { url: s.url }); });
  $('lib-epub').addEventListener('click', downloadSelected);
  $('lib-remove').addEventListener('click', askRemove);
  $('lib-reset').addEventListener('click', askReset);
  $('lib-confirm-yes').addEventListener('click', confirmYes);
  $('lib-confirm-no').addEventListener('click', function () { pendingRemove = null; show('lib-confirm', false); });
  $('lib-all').addEventListener('click', function () { visible().forEach(function (e) { selected[e.url] = true; }); draw(); });
  $('lib-none').addEventListener('click', function () { selected = {}; draw(); });
  $('lib-invert').addEventListener('click', function () {
    visible().forEach(function (e) { if (selected[e.url]) delete selected[e.url]; else selected[e.url] = true; });
    draw();
  });

  H.onState(onState);
  H.onView('library', load);
  window.HuaLibrary = { load: load, entries: function () { return entries; } };
})();

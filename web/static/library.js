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
  var detailUrl = '';
  var tocUrl = '';
  var tocSeq = 0;

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
      btn.setAttribute('aria-haspopup', 'dialog');
      btn.setAttribute('aria-label', 'Details for ' + (e.title || 'book'));
      var cover = H.el('span', 'tile-cover');
      cover.appendChild(H.el('span', 'tile-glyph', (e.title || '?').trim().charAt(0)));
      if (e.has_cover) cover.appendChild(coverImage(e));
      var b = badge(e);
      if (b) cover.appendChild(H.el('span', 'tile-badge ' + b.cls, b.text));
      btn.appendChild(cover);
      var text = H.el('span', 'tile-text');
      text.appendChild(H.el('span', 'tile-title', e.title));
      text.appendChild(H.el('span', 'tile-sub muted small', metaLine(e)));
      btn.appendChild(text);
      btn.addEventListener('click', function () { openDetail(e); });
      var pick = H.el('button', 'tile-pick');
      pick.type = 'button';
      pick.setAttribute('aria-pressed', selected[e.url] ? 'true' : 'false');
      pick.setAttribute('aria-label', (selected[e.url] ? 'Deselect ' : 'Select ') + (e.title || 'book'));
      pick.addEventListener('click', function (ev) {
        ev.stopPropagation();
        toggleSelected(e.url);
      });
      li.appendChild(btn);
      li.appendChild(pick);
      shelf.appendChild(li);
    });
    if (detailUrl) {
      var open = entries.filter(function (e) { return e.url === detailUrl; })[0];
      if (open) fillDetail(open);
      else closeDetail();
    }
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

  function metaLine(e) {
    return [e.author, H.plural(e.chapters, 'chapter')].filter(Boolean).join(' · ');
  }

  function coverImage(e) {
    var img = document.createElement('img');
    img.alt = '';
    img.loading = 'lazy';
    img.src = '/api/library/cover?u=' + encodeURIComponent(e.url);
    img.onerror = function () { img.remove(); };
    return img;
  }

  function toggleSelected(url) {
    if (selected[url]) delete selected[url]; else selected[url] = true;
    draw();
  }

  function detailEntry() {
    return entries.filter(function (e) { return e.url === detailUrl; })[0] || null;
  }

  function savedOn(ts) {
    var n = Number(ts);
    if (!n) return '';
    try {
      return new Date(n * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    } catch (err) {
      return '';
    }
  }

  function fileLine(e) {
    if (e.has_epub) return 'On this PC';
    return 'Not on this PC';
  }

  function statusLine(e) {
    var b = badge(e);
    if (b && e.status === 'error' && e.status_error) return b.text + ' — ' + e.status_error;
    if (b) return b.text;
    return 'Not checked yet';
  }

  function fillDetail(e) {
    setText('lib-detail-title', e.title || 'Book');
    setText('lib-detail-original', e.title_original || '');
    show('lib-detail-original', !!e.title_original);
    setText('lib-detail-by', e.author || '');
    show('lib-detail-by', !!e.author);
    setText('lib-detail-chapters', H.plural(e.chapters, 'chapter'));
    setText('lib-detail-latest', e.last_chapter || '');
    show('lib-detail-latest-row', !!e.last_chapter);
    var saved = savedOn(e.updated_at);
    setText('lib-detail-saved', saved);
    show('lib-detail-saved-row', !!saved);
    setText('lib-detail-file', fileLine(e));
    setText('lib-detail-status', statusLine(e));
    setText('lib-detail-blurb', e.description || '');
    show('lib-detail-blurb', !!e.description);
    var cover = $('lib-detail-cover');
    cover.textContent = '';
    cover.appendChild(H.el('span', 'detail-glyph', (e.title || '?').trim().charAt(0)));
    if (e.has_cover) cover.appendChild(coverImage(e));
    var busy = H.state().busy;
    $('lib-detail-update').disabled = !!busy;
    $('lib-detail-remove').disabled = !!busy;
    setText('lib-detail-select', selected[e.url] ? 'Selected' : 'Select');
    loadToc(e);
  }

  function loadToc(e) {
    if (tocUrl === e.url) return;
    tocUrl = e.url;
    var seq = ++tocSeq;
    var list = $('lib-detail-toc');
    list.textContent = '';
    list.appendChild(H.el('li', 'muted small', 'Loading chapters…'));
    H.api('GET', '/api/library/chapters?u=' + encodeURIComponent(e.url)).then(function (res) {
      if (seq !== tocSeq || detailUrl !== e.url) return;
      list.textContent = '';
      var rows = (res.ok && res.data && res.data.chapters) || [];
      if (!rows.length) {
        list.appendChild(H.el('li', 'muted small', res.ok ? 'No chapter list on this PC yet.' : H.errorText(res.data)));
        return;
      }
      rows.forEach(function (ch, i) {
        var li = document.createElement('li');
        var b = H.el('button', 'toc-row');
        b.type = 'button';
        b.appendChild(H.el('span', 'mono toc-n', String(ch.n || i + 1)));
        b.appendChild(H.el('span', 'toc-name', ch.title || ('Chapter ' + (i + 1))));
        b.addEventListener('click', function () {
          var url = e.url;
          closeDetail();
          H.go('read', { url: url, index: i });
        });
        li.appendChild(b);
        list.appendChild(li);
      });
    }).catch(function () {
      if (seq !== tocSeq) return;
      list.textContent = '';
      list.appendChild(H.el('li', 'muted small', H.ERRORS.network));
    });
  }

  function openDetail(e) {
    detailUrl = e.url;
    fillDetail(e);
    var d = $('lib-detail');
    if (!d.open) d.showModal();
  }

  function closeDetail() {
    detailUrl = '';
    tocUrl = '';
    tocSeq += 1;
    var d = $('lib-detail');
    if (d && d.open) d.close();
  }

  function post(path, body, after, action) {
    return H.api('POST', path, body).then(function (res) {
      if (!res.ok) { setText('lib-lede', H.errorText(res.data, '', action)); return null; }
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
      'the cached chapters and cover, and the reading position.');
    setText('lib-confirm-yes', sel.length > 1 ? 'Remove ' + sel.length + ' books' : 'Remove');
    show('lib-confirm', true);
    $('lib-confirm-no').focus();
  }

  function askReset() {
    pendingRemove = 'reset';
    setText('lib-confirm-text', 'Clear every tracked book from the Library? EPUB files on this PC are kept.');
    setText('lib-confirm-yes', 'Reset library');
    show('lib-confirm', true);
    $('lib-confirm-no').focus();
  }

  function confirmYes() {
    var what = pendingRemove;
    pendingRemove = null;
    show('lib-confirm', false);
    if (what === 'reset') {
      post('/api/library/reset', {}, function () { selected = {}; load(); }, 'reset the library');
      return;
    }
    if (what) post('/api/library/remove', { urls: what }, function () { selected = {}; load(); }, 'remove these books');
  }

  function downloadSelected() {
    var sel = selection();
    var missing = 0;
    var chain = Promise.resolve();
    sel.forEach(function (e) {
      chain = chain.then(function () {
        return H.api('POST', '/api/library/epub', { url: e.url }).then(function (res) {
          if (res.ok && res.data && res.data.file) H.triggerDownload(res.data.file);
          else if (res.data && res.data.error === 'busy') {
            setText('lib-lede', H.errorText(res.data, '', 'download this EPUB'));
          } else missing += 1;
        });
      });
    });
    chain.then(function () {
      if (missing) setText('lib-lede', H.plural(missing, 'book') + ' had no EPUB on this PC.');
    });
  }

  function onState(s) {
    if (H.view() !== 'library') {
      closeDetail();
      return;
    }
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
  $('lib-check').addEventListener('click', function () { post('/api/library/check', {}, null, 'check for updates'); });
  $('lib-update-all').addEventListener('click', function () {
    post('/api/library/update-all', {}, null, 'update every book with new chapters');
  });
  $('lib-update').addEventListener('click', function () {
    post('/api/library/update', { urls: selection().map(function (e) { return e.url; }) }, null, 'update the selected books');
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
  $('lib-detail-close').addEventListener('click', closeDetail);
  $('lib-detail').addEventListener('click', function (ev) {
    if (ev.target === $('lib-detail')) closeDetail();
  });
  $('lib-detail').addEventListener('close', function () { detailUrl = ''; });
  $('lib-detail-read').addEventListener('click', function () {
    var e = detailEntry();
    if (!e) return;
    closeDetail();
    H.go('read', { url: e.url });
  });
  $('lib-detail-open').addEventListener('click', function () {
    var e = detailEntry();
    if (!e) return;
    closeDetail();
    H.go('single', { url: e.url });
  });
  $('lib-detail-update').addEventListener('click', function () {
    var e = detailEntry();
    if (!e) return;
    closeDetail();
    post('/api/library/update', { urls: [e.url] }, null, 'update this book');
  });
  $('lib-detail-epub').addEventListener('click', function () {
    var e = detailEntry();
    if (!e) return;
    H.api('POST', '/api/library/epub', { url: e.url }).then(function (res) {
      if (res.ok && res.data && res.data.file) H.triggerDownload(res.data.file);
      else if (res.data && res.data.error === 'busy') {
        setText('lib-lede', H.errorText(res.data, '', 'download this EPUB'));
      } else setText('lib-lede', 'No EPUB on this PC.');
    });
  });
  $('lib-detail-select').addEventListener('click', function () {
    var e = detailEntry();
    if (e) toggleSelected(e.url);
  });
  $('lib-detail-remove').addEventListener('click', function () {
    var e = detailEntry();
    if (!e) return;
    selected = {};
    selected[e.url] = true;
    closeDetail();
    askRemove();
    draw();
  });

  H.onState(onState);
  H.onView('library', load);
  window.HuaLibrary = { load: load, entries: function () { return entries; } };
})();

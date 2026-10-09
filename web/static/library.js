// Library: tracked books, new-chapter checks, updates, Download EPUB, Remove.
(function () {
  'use strict';
  var H = window.Hua, $ = H.$, setText = H.setText, show = H.show;

  var entries = [];
  var selected = {};      // url -> true
  var PREFS_KEY = 'huaepub-library';
  var prefs = {};
  try { prefs = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') || {}; } catch (err) { prefs = {}; }
  var filter = prefs.filter || 'all';
  var sort = prefs.sort || 'recent';
  var query = '';
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

  // Sort and filter are remembered in this browser; the search is not.
  function savePrefs() {
    // The shelved list is a place you visit, not a view to come back to.
    var keep = filter === 'shelved' ? 'all' : filter;
    try { localStorage.setItem(PREFS_KEY, JSON.stringify({ filter: keep, sort: sort })); } catch (err) { /* ignore */ }
  }

  var FILTERS = {
    all: function () { return true; },
    reading: function (e) { return e.read_chapter > 0; },
    unopened: function (e) { return !e.read_chapter; },
    updates: function (e) { return e.status === 'update'; },
    shelved: function (e) { return !!e.shelved_at; }
  };

  function active() { return entries.filter(function (e) { return !e.shelved_at; }); }

  function lastTouched(e) { return Math.max(e.read_at || 0, e.updated_at || 0); }
  function leftToRead(e) { return Math.max(0, (e.chapters || 0) - (e.read_chapter || 0)); }
  function byTitle(a, b) { return (a.title || '').localeCompare(b.title || '', undefined, { sensitivity: 'base' }); }

  // Array.sort is stable, so ties keep the Library's own order.
  var SORTS = {
    recent: function (a, b) { return lastTouched(b) - lastTouched(a); },
    title: byTitle,
    author: function (a, b) {
      if (!a.author !== !b.author) return a.author ? -1 : 1;  // books without an author go last
      return (a.author || '').localeCompare(b.author || '', undefined, { sensitivity: 'base' }) || byTitle(a, b);
    },
    left: function (a, b) { return leftToRead(b) - leftToRead(a); },
    chapters: function (a, b) { return (b.chapters || 0) - (a.chapters || 0); }
  };

  function searchText(e) {
    return [e.title, e.title_original, e.author, callNumber(e.url), e.url].join(' ').toLowerCase();
  }

  function visible() {
    var words = query.toLowerCase().split(/\s+/).filter(Boolean);
    var keep = FILTERS[filter] || FILTERS.all;
    return entries.filter(function (e) {
      // Shelved books only show on the shelved list.
      if (!!e.shelved_at !== (filter === 'shelved') || !keep(e)) return false;
      var hay = searchText(e);
      return words.every(function (w) { return hay.indexOf(w) >= 0; });
    }).sort(SORTS[sort] || SORTS.recent);
  }

  function selection() { return entries.filter(function (e) { return selected[e.url]; }); }

  // Call number from the source link: twkan.com/book/86838.html -> "TWKAN 86838".
  function callNumber(url) {
    try {
      var u = new URL(url);
      var host = u.hostname.replace(/^www\./, '').split('.')[0].toUpperCase();
      var ids = u.pathname.match(/\d+/g);
      return host + (ids ? ' ' + ids[ids.length - 1] : '');
    } catch (err) {
      return '';
    }
  }

  function readLine(e) {
    if (!e.read_chapter) return 'Not opened';
    if (e.chapters && e.read_chapter >= e.chapters) return 'Caught up';
    return 'On chapter ' + e.read_chapter;
  }

  // Where the reader left off, e.g. "Chapter 38 of 120, 82 left".
  function whereLine(e) {
    var line = 'Chapter ' + Math.min(e.read_chapter, e.chapters || e.read_chapter) +
      (e.chapters ? ' of ' + e.chapters : '');
    var left = leftToRead(e);
    if (!e.chapters) return line;
    return line + (left ? ', ' + left + ' left' : ', caught up');
  }

  function badge(e) {
    if (e.shelved_at) return { text: 'Shelved', cls: 'is-wait' };
    if (e.status === 'update') return { text: '+' + e.new_count + ' new', cls: 'is-new' };
    if (e.status === 'checking') return { text: 'Checking…', cls: 'is-wait' };
    if (e.status === 'error') return { text: 'Check failed', cls: 'is-err' };
    if (e.status === 'current') return { text: 'Up to date', cls: 'is-ok' };
    return null;
  }

  function draw() {
    drawShelf();
    drawControls();
  }

  // The cards. Rebuilding them reloads every cover, so only do it when the books,
  // the filter or the selection changed, not on every progress tick.
  function drawShelf() {
    var shelf = $('shelf');
    shelf.textContent = '';
    var list = visible();
    show('shelf-empty', entries.length === 0);
    show('lib-find', entries.length > 0);
    show('shelf-none', entries.length > 0 && list.length === 0);
    if (entries.length && !list.length) setText('shelf-none-text', noMatchText());
    drawContinue();
    list.forEach(function (e) {
      var li = H.el('li', 'tile' + (selected[e.url] ? ' is-sel' : '') + (e.shelved_at ? ' is-shelved' : ''));
      var btn = H.el('button', 'tile-btn');
      btn.type = 'button';
      btn.setAttribute('aria-haspopup', 'dialog');
      btn.setAttribute('aria-label', 'Details for ' + (e.title || 'book'));
      btn.appendChild(H.el('span', 'tile-call', callNumber(e.url)));
      var cover = H.el('span', 'tile-cover');
      cover.appendChild(H.el('span', 'tile-glyph', (e.title_original || e.title || '?').trim().charAt(0)));
      if (e.has_cover) cover.appendChild(shelfCover(e));
      btn.appendChild(cover);
      var text = H.el('span', 'tile-text');
      text.appendChild(H.el('span', 'tile-title', e.title));
      if (e.title_original) text.appendChild(H.el('span', 'tile-orig', e.title_original));
      if (e.author) text.appendChild(H.el('span', 'tile-sub', e.author));
      btn.appendChild(text);
      var foot = H.el('span', 'tile-foot');
      var count = H.el('span', '');
      count.appendChild(H.el('b', '', String(e.chapters || 0)));
      count.appendChild(document.createTextNode(e.chapters === 1 ? ' chapter' : ' chapters'));
      foot.appendChild(count);
      foot.appendChild(H.el('span', '', readLine(e)));
      var b = badge(e);
      if (b) foot.appendChild(H.el('span', 'tile-badge ' + b.cls, b.text));
      btn.appendChild(foot);
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
  }

  function noMatchText() {
    var q = query.trim();
    if (q) return 'No books match “' + q + '”' + (filter === 'all' ? '.' : ' in this list.');
    if (filter === 'reading') return 'You have not opened any of these books yet.';
    if (filter === 'unopened') return 'You have started every book.';
    if (filter === 'updates') return 'No new chapters. Check for updates to look again.';
    if (filter === 'shelved') return 'Nothing is shelved.';
    if (!active().length) return 'Every book is shelved.';
    return 'No books here.';
  }

  // The book read most recently, one tap from where it was left.
  function drawContinue() {
    var last = null;
    entries.forEach(function (e) {
      if (e.read_at && !e.shelved_at && (!last || e.read_at > last.read_at)) last = e;
    });
    show('lib-continue', !!last && filter !== 'shelved');
    if (!last) return;
    $('lib-continue').dataset.url = last.url;
    setText('lib-continue-title', last.title);
    setText('lib-continue-where', whereLine(last));
    var cover = $('lib-continue-cover');
    cover.textContent = '';
    cover.appendChild(H.el('span', 'tile-glyph', (last.title_original || last.title || '?').trim().charAt(0)));
    if (last.has_cover) cover.appendChild(coverImage(last));
  }

  // Messages from actions. Kept apart from the lede, which every state tick rewrites.
  // ok: a confirmation rather than a problem.
  function note(text, ok) {
    var where = detailUrl ? 'lib-detail-note' : 'lib-note';
    setText(where, text || '');
    $(where).classList.toggle('is-ok', !!ok);
    show(where, !!text);
  }

  // Buttons, counts and the summary line: cheap, safe on every state tick.
  function drawControls() {
    if (detailUrl) {
      $('lib-detail-update').disabled = !!H.state().busy;
      $('lib-detail-remove').disabled = !!H.state().busy;
    }
    var sel = selection();
    var n = sel.length;
    setText('lib-count', n + ' selected');
    document.querySelector('.toolbar-sel').classList.toggle('has-sel', n > 0);
    var busy = H.state().busy;
    $('lib-read').disabled = n !== 1;
    $('lib-open').disabled = n !== 1;
    $('lib-update').disabled = busy || n === 0;
    setText('lib-update', n > 1 ? 'Update (' + n + ')' : 'Update');
    $('lib-epub').disabled = !sel.some(function (e) { return e.has_epub; });
    $('lib-remove').disabled = busy || n === 0;
    setText('lib-remove', n > 1 ? 'Remove (' + n + ')' : 'Remove');
    var books = active();
    var shelvedCount = entries.length - books.length;
    var withUpdates = books.filter(function (e) { return e.status === 'update'; }).length;
    $('lib-update-all').disabled = busy || !withUpdates;
    setText('lib-update-all', withUpdates ? 'Update all (' + withUpdates + ')' : 'Update all');
    $('lib-check').disabled = busy || !books.length;
    var allShelved = n > 0 && sel.every(function (e) { return e.shelved_at; });
    $('lib-shelve').disabled = n === 0;
    setText('lib-shelve', (allShelved ? 'Put back' : 'Shelve') + (n > 1 ? ' (' + n + ')' : ''));
    show('lib-shelved', shelvedCount > 0 && filter !== 'shelved');
    setText('lib-shelved', 'Shelved books (' + shelvedCount + ')');
    show('lib-shelved-head', filter === 'shelved');
    document.querySelector('.lib-filter').classList.toggle('is-off', filter === 'shelved');
    setText('lib-lede', entries.length
      ? H.plural(books.length, 'book') + ' in the catalogue' +
        (shelvedCount ? ', ' + shelvedCount + ' shelved.' : '.') +
        (withUpdates ? ' ' + withUpdates + (withUpdates === 1 ? ' has' : ' have') + ' new chapters.' : '')
      : 'Books you have downloaded on this PC.');
    var shown = $('shelf').children.length;
    var pool = filter === 'shelved' ? shelvedCount : books.length;
    if (pool && shown !== pool) setText('lib-count', n + ' selected, ' + shown + ' of ' + pool + ' shown');
  }

  // Shelf covers are kept and moved into the rebuilt cards, so a redraw never reloads them.
  var shelfCovers = {};
  function shelfCover(e) {
    var img = shelfCovers[e.url];
    if (!img || !img.isConnected && img.dataset.failed) {
      img = coverImage(e);
      shelfCovers[e.url] = img;
    }
    return img;
  }

  function coverImage(e) {
    var img = document.createElement('img');
    img.alt = '';
    img.loading = 'lazy';
    img.src = '/api/library/cover?u=' + encodeURIComponent(e.url);
    img.onerror = function () { img.dataset.failed = '1'; img.remove(); };
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
    if (e.shelved_at) return 'Shelved on ' + savedOn(e.shelved_at);
    var b = badge(e);
    if (b && e.status === 'error' && e.status_error) return b.text + ' — ' + e.status_error;
    if (b) return b.text;
    return 'Not checked yet';
  }

  function fillDetail(e) {
    setText('lib-detail-call', callNumber(e.url));
    setText('lib-detail-title', e.title || 'Book');
    setText('lib-detail-original', e.title_original || '');
    show('lib-detail-original', !!e.title_original);
    setText('lib-detail-by', e.author || '');
    show('lib-detail-by', !!e.author);
    setText('lib-detail-chapters', H.plural(e.chapters, 'chapter'));
    setText('lib-detail-readpos', e.read_chapter ? String(e.read_chapter) : '');
    show('lib-detail-read-row', !!e.read_chapter);
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
    cover.appendChild(H.el('span', 'detail-glyph', (e.title_original || e.title || '?').trim().charAt(0)));
    if (e.has_cover) cover.appendChild(coverImage(e));
    var busy = H.state().busy;
    $('lib-detail-update').disabled = !!busy;
    $('lib-detail-remove').disabled = !!busy;
    $('lib-detail-epub').disabled = !e.has_epub;
    setText('lib-detail-select', selected[e.url] ? 'Selected' : 'Select');
    setText('lib-detail-read', e.read_chapter ? 'Continue reading' : 'Start reading');
    setText('lib-detail-shelve', e.shelved_at ? 'Put back' : 'Shelve');
    loadToc(e);
  }

  function loadToc(e) {
    if (tocUrl === e.url) return;
    tocUrl = e.url;
    var seq = ++tocSeq;
    var list = $('lib-detail-toc');
    list.textContent = '';
    $('lib-detail-find').value = '';
    show('lib-detail-find', false);
    list.appendChild(H.el('li', 'muted small', 'Loading chapters…'));
    H.api('GET', '/api/library/chapters?u=' + encodeURIComponent(e.url)).then(function (res) {
      if (seq !== tocSeq || detailUrl !== e.url) return;
      list.textContent = '';
      var rows = (res.ok && res.data && res.data.chapters) || [];
      if (!rows.length) {
        list.appendChild(H.el('li', 'muted small', res.ok ? 'No chapter list on this PC yet.' : H.errorText(res.data)));
        return;
      }
      var here = null;
      rows.forEach(function (ch, i) {
        var li = document.createElement('li');
        li.dataset.n = String(ch.n || i + 1);
        li.dataset.find = (li.dataset.n + ' ' + (ch.title || '')).toLowerCase();
        var b = H.el('button', 'toc-row');
        if (i + 1 === e.read_chapter) {
          b.classList.add('is-here');
          b.setAttribute('aria-current', 'true');
          here = li;
        }
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
      // A long book gets a finder, and its list opens at the chapter being read.
      show('lib-detail-find', rows.length > 30);
      if (here) list.scrollTop = Math.max(0, here.offsetTop - list.offsetTop - list.clientHeight / 3);
    }).catch(function () {
      if (seq !== tocSeq) return;
      list.textContent = '';
      list.appendChild(H.el('li', 'muted small', H.ERRORS.network));
    });
  }

  // A number finds that chapter; words find titles containing them.
  function findChapter() {
    var q = $('lib-detail-find').value.trim().toLowerCase();
    var num = /^\d+$/.test(q);
    Array.prototype.forEach.call($('lib-detail-toc').children, function (li) {
      if (!li.dataset.find) return;
      li.hidden = !!q && (num ? li.dataset.n !== q : li.dataset.find.indexOf(q) < 0);
    });
  }

  function openDetail(e) {
    note('');
    detailUrl = e.url;
    fillDetail(e);
    var d = $('lib-detail');
    if (!d.open) d.showModal();
  }

  function closeDetail() {
    show('lib-detail-note', false);
    detailUrl = '';
    tocUrl = '';
    tocSeq += 1;
    var d = $('lib-detail');
    if (d && d.open) d.close();
  }

  function post(path, body, after, action) {
    return H.api('POST', path, body).then(function (res) {
      if (!res.ok) { note(H.errorText(res.data, '', action)); return null; }
      note('');
      H.refresh();
      if (after) after(res.data);
      return res.data;
    }).catch(function () { note(H.ERRORS.network); });
  }

  // Shelve keeps the card (and its files) but takes it out of the way for good.
  function shelve(urls, on) {
    if (!urls.length) return;
    post('/api/library/shelve', { urls: urls, shelved: on }, function () {
      selected = {};
      load();
      note(on ? H.plural(urls.length, 'book') + ' shelved. Find ' + (urls.length === 1 ? 'it' : 'them') +
        ' under Shelved books.' : H.plural(urls.length, 'book') + ' back in the library.', true);
    }, on ? 'shelve these books' : 'put these books back');
  }

  function setFilter(value) {
    filter = value;
    note('');
    document.querySelectorAll('input[name="lib-filter"]').forEach(function (r) { r.checked = r.value === value; });
    selected = {};
    savePrefs();
    draw();
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
    note('');
    sel.forEach(function (e) {
      chain = chain.then(function () {
        return H.api('POST', '/api/library/epub', { url: e.url }).then(function (res) {
          if (res.ok && res.data && res.data.file) H.triggerDownload(res.data.file);
          else if (res.data && res.data.error === 'busy') {
            note(H.errorText(res.data, '', 'download this EPUB'));
          } else missing += 1;
        }).catch(function () { missing += 1; });
      });
    });
    chain.then(function () {
      if (missing) note(H.plural(missing, 'book') + ' had no EPUB on this PC.');
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
      drawControls();
    }
  }

  if (!FILTERS[filter]) filter = 'all';
  if (!SORTS[sort]) sort = 'recent';
  $('lib-sort').value = sort;
  document.querySelectorAll('input[name="lib-filter"]').forEach(function (e) {
    e.checked = e.value === filter;
    e.addEventListener('change', function () { setFilter(e.value); });
  });
  $('lib-sort').addEventListener('change', function () { sort = $('lib-sort').value; savePrefs(); draw(); });
  $('lib-search').addEventListener('input', function () { query = $('lib-search').value; draw(); });
  $('lib-show-all').addEventListener('click', function () {
    query = '';
    $('lib-search').value = '';
    setFilter('all');
  });
  $('lib-continue').addEventListener('click', function () {
    var url = $('lib-continue').dataset.url;
    if (url) H.go('read', { url: url });
  });
  $('lib-detail-find').addEventListener('input', findChapter);
  // "/" jumps to the search box, as on most sites that have one.
  document.addEventListener('keydown', function (ev) {
    if (ev.key !== '/' || H.view() !== 'library' || detailUrl || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    var t = ev.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    ev.preventDefault();
    $('lib-search').focus();
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
  $('lib-shelve').addEventListener('click', function () {
    var sel = selection();
    shelve(sel.map(function (e) { return e.url; }), !sel.every(function (e) { return e.shelved_at; }));
  });
  $('lib-shelved').addEventListener('click', function () { setFilter('shelved'); window.scrollTo(0, 0); });
  $('lib-shelved-back').addEventListener('click', function () { setFilter('all'); });
  $('lib-detail-shelve').addEventListener('click', function () {
    var e = detailEntry();
    if (!e) return;
    closeDetail();
    shelve([e.url], !e.shelved_at);
  });
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
        note(H.errorText(res.data, '', 'download this EPUB'));
      } else note('No EPUB on this PC.');
    }).catch(function () { note(H.ERRORS.network); });
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

// Read: a paged book in the browser. The place is the same reading.json as the desktop.
(function () {
  'use strict';
  var H = window.Hua, $ = H.$, setText = H.setText, show = H.show;

  var book = null;
  var index = 0;
  var logicalPage = 0;
  var fontPt = 18;
  var loadSeq = 0;
  var saveTimer = null;
  var chromeTimer = null;
  var prefs = { theme: 'paper', mode: 'pages', face: 'serif', leading: 'normal', align: 'justify' };
  var touchX = 0;

  function root() { return $('read-book'); }

  function paint() {
    var el = root();
    el.dataset.theme = prefs.theme;
    el.dataset.face = prefs.face;
    el.dataset.leading = prefs.leading;
    el.dataset.align = prefs.align;
    el.classList.toggle('is-pages', prefs.mode === 'pages');
    el.classList.toggle('is-scroll', prefs.mode !== 'pages');
    document.querySelectorAll('#read-display .chip').forEach(function (btn) {
      var key = btn.getAttribute('data-pref');
      var field = { reader_theme: 'theme', reader_mode: 'mode', reader_face: 'face',
        reader_leading: 'leading', reader_align: 'align' }[key];
      btn.classList.toggle('is-on', field && prefs[field] === btn.getAttribute('data-value'));
    });
    $('read-size').value = String(fontPt);
    setText('read-size-n', String(fontPt));
    document.documentElement.style.setProperty('--read-size', fontPt + 'px');
  }

  function layout() {
    var leaf = $('read-leaf');
    if (!leaf) return;
    leaf.style.setProperty('--page', leaf.clientWidth + 'px');
  }

  function pageCount() {
    var leaf = $('read-leaf');
    var spread = $('read-spread');
    var width = leaf.clientWidth || 1;
    if (prefs.mode !== 'pages') return 1;
    return Math.max(1, Math.round(spread.scrollWidth / width));
  }

  function pageIndex() {
    if (prefs.mode !== 'pages') {
      var width = $('read-leaf').clientWidth || 1;
      return Math.max(0, Math.round($('read-leaf').scrollLeft / width));
    }
    var pages = pageCount();
    if (logicalPage > pages - 1) logicalPage = Math.max(0, pages - 1);
    return logicalPage;
  }

  function chapterRatio() {
    if (prefs.mode === 'pages') {
      var pages = pageCount();
      if (pages <= 1) return book && index >= book.chapters.length - 1 ? 1 : 0;
      return pageIndex() / (pages - 1);
    }
    var leaf = $('read-leaf');
    var span = Math.max(1, leaf.scrollHeight - leaf.clientHeight);
    return Math.max(0, Math.min(1, leaf.scrollTop / span));
  }

  function fraction() {
    if (!book) return 0;
    var n = Math.max(1, book.chapters.length);
    return Math.max(0, Math.min(0.999, (index + chapterRatio()) / n));
  }

  function placeText(value, chapter) {
    if (!book) return '';
    var n = book.chapters.length || 1;
    var ch = chapter == null
      ? Math.min(n - 1, Math.floor(Math.min(0.999999, value) * n))
      : chapter;
    return (ch + 1) + ' of ' + n + '  ·  ' + Math.round(value * 100) + '%';
  }

  function showPlace() {
    var value = fraction();
    $('read-progress').value = String(Math.round(value * 1000));
    setText('read-place', placeText(value, index));
    var ch = book && book.chapters[index];
    setText('read-running', ch ? ((index + 1) + '  ·  ' + (ch.title || '')) : '');
    syncMark();
  }

  function reveal(scroll) {
    var leaf = $('read-leaf');
    var ratio = Math.max(0, Math.min(1, Number(scroll) || 0));
    leaf.style.scrollBehavior = 'auto';
    if (prefs.mode === 'pages') {
      var pages = pageCount();
      var page = pages <= 1 ? 0 : Math.round(ratio >= 0.999 ? pages - 1 : ratio * (pages - 1));
      logicalPage = page;
      leaf.scrollLeft = page * (leaf.clientWidth || 1);
    } else {
      var span = Math.max(0, leaf.scrollHeight - leaf.clientHeight);
      leaf.scrollTop = span * ratio;
    }
    leaf.style.scrollBehavior = '';
    showPlace();
  }

  function goToPage(page) {
    var leaf = $('read-leaf');
    logicalPage = page;
    leaf.style.scrollBehavior = 'auto';
    leaf.scrollLeft = page * (leaf.clientWidth || 1);
  }

  function afterMove() {
    showPlace();
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(savePosition, 600);
  }

  function hideSheets() {
    show('read-contents', false);
    show('read-display', false);
  }

  function showChrome() {
    root().classList.add('is-chrome');
    if (chromeTimer) clearTimeout(chromeTimer);
    chromeTimer = setTimeout(function () {
      if ($('read-contents').hidden && $('read-display').hidden) root().classList.remove('is-chrome');
    }, 3200);
  }

  function withinChapter() {
    return chapterRatio();
  }

  function apply(next, persistKey) {
    var kept = book ? withinChapter() : 0;
    if (next.theme) prefs.theme = next.theme;
    if (next.mode) prefs.mode = next.mode;
    if (next.face) prefs.face = next.face;
    if (next.leading) prefs.leading = next.leading;
    if (next.align) prefs.align = next.align;
    if (typeof next.font === 'number') fontPt = Math.max(12, Math.min(36, next.font));
    paint();
    layout();
    if (book) requestAnimationFrame(function () { layout(); reveal(kept); });
    if (persistKey) {
      var body = {};
      body[persistKey] = persistKey === 'reader_font_pt' ? fontPt : {
        reader_theme: prefs.theme, reader_mode: prefs.mode, reader_face: prefs.face,
        reader_leading: prefs.leading, reader_align: prefs.align
      }[persistKey];
      H.api('PUT', '/api/settings', body).catch(function () {});
    }
  }

  function setFont(pt, persist) {
    apply({ font: pt }, persist ? 'reader_font_pt' : '');
  }

  function pickList() {
    document.body.classList.remove('is-reading');
    show('read-pick', true);
    show('read-book', false);
    hideSheets();
    H.api('GET', '/api/library').then(function (res) {
      var list = $('read-list');
      list.textContent = '';
      var items = (res.ok && res.data && res.data.entries) || [];
      show('read-empty', !items.length);
      items.forEach(function (e) {
        var li = document.createElement('li');
        var b = H.el('button', 'pick');
        b.type = 'button';
        b.appendChild(H.el('span', 'pick-title', e.title));
        b.appendChild(H.el('span', 'muted small', H.plural(e.chapters, 'chapter') +
          (e.has_epub ? ' · EPUB' : ' · cached chapters')));
        b.addEventListener('click', function () { open({ url: e.url }); });
        li.appendChild(b);
        list.appendChild(li);
      });
    });
  }

  function open(arg) {
    document.body.classList.add('is-reading');
    show('read-pick', false);
    show('read-book', true);
    hideSheets();
    showChrome();
    setText('read-title', 'Opening…');
    setText('read-kind', '');
    setText('read-status', '');
    setText('read-running', '');
    $('read-text').textContent = '';
    var jump = arg && typeof arg.index === 'number' ? arg.index : null;
    H.api('POST', '/api/read/open', {
      url: (arg && arg.url) || '',
      preview_id: (arg && arg.preview_id) || ''
    }).then(function (res) {
      if (!res.ok) {
        setText('read-title', 'Could not open this book');
        setText('read-status', H.errorText(res.data, '', 'open this book'));
        return;
      }
      book = res.data;
      book.bookmarks = res.data.bookmarks || [];
      setText('read-title', book.title);
      setText('read-kind', book.kind === 'epub' ? 'EPUB' : 'Cached chapters');
      load(jump !== null ? jump : (book.index || 0), jump !== null ? 0 : (book.scroll || 0));
    }).catch(function () { setText('read-status', H.ERRORS.network); });
  }

  function load(i, scroll) {
    if (!book || !book.chapters.length) return;
    index = Math.max(0, Math.min(i, book.chapters.length - 1));
    var seq = ++loadSeq;
    var ready = book.chapters[index] && book.chapters[index].ready;
    setText('read-status', ready ? '' : 'Fetching this chapter from the site…');
    if (prefs.mode === 'pages') logicalPage = 0;
    var ch = book.chapters[index];
    setText('read-running', ch ? ((index + 1) + '  ·  ' + (ch.title || '')) : '');
    syncMark();
    H.api('GET', '/api/read/' + book.book_id + '/chapter/' + index).then(function (res) {
      if (seq !== loadSeq) return;
      if (!res.ok) {
        if (res.status === 404 && res.data && res.data.error === 'book_closed') { open({ url: book.url || '' }); return; }
        setText('read-status', H.errorText(res.data, '', 'load this chapter'));
        return;
      }
      book.chapters[index].ready = true;
      if (res.data.title) book.chapters[index].title = res.data.title;
      $('read-text').innerHTML = res.data.html;
      setText('read-status', res.data.note || '');
      layout();
      requestAnimationFrame(function () {
        layout();
        requestAnimationFrame(function () { reveal(scroll); savePosition(); });
      });
    }).catch(function () { if (seq === loadSeq) setText('read-status', H.ERRORS.network); });
  }

  function turn(dir) {
    hideSheets();
    root().classList.remove('is-chrome');
    if (!book) return;
    if (prefs.mode !== 'pages') {
      var leaf = $('read-leaf');
      var step = leaf.clientHeight * 0.86;
      if (dir < 0 && leaf.scrollTop <= 2) { load(index - 1, 1); return; }
      if (dir > 0 && leaf.scrollTop + step >= leaf.scrollHeight - leaf.clientHeight - 2) {
        load(index + 1, 0); return;
      }
      leaf.scrollTo({ top: leaf.scrollTop + dir * step, behavior: 'smooth' });
      afterMove();
      return;
    }
    var page = pageIndex() + dir;
    var pages = pageCount();
    if (page < 0) { load(index - 1, 1); return; }
    if (page >= pages) { load(index + 1, 0); return; }
    goToPage(page);
    afterMove();
  }

  function seek(value) {
    if (!book || !book.chapters.length) return;
    var n = book.chapters.length;
    var ch = Math.min(n - 1, Math.floor(value * n));
    var within = value * n - ch;
    if (ch === index) reveal(within);
    else load(ch, within);
  }

  function ratioNow() {
    if (!book) return 0;
    return chapterRatio();
  }

  function savePosition() {
    if (!book) return;
    H.api('POST', '/api/read/' + book.book_id + '/position', {
      index: index, scroll: ratioNow()
    }).catch(function () {});
  }

  function chapterMarked(i) {
    return (book.bookmarks || []).some(function (m) { return m.chapter_index === i; });
  }

  function thisPageMarked() {
    var scroll = Math.round(ratioNow() * 100) / 100;
    return (book.bookmarks || []).some(function (m) {
      return m.chapter_index === index && Math.round(Number(m.scroll) * 100) / 100 === scroll;
    });
  }

  function syncMark() {
    var on = thisPageMarked();
    $('read-mark').classList.toggle('is-on', on);
    $('read-mark').setAttribute('aria-pressed', on ? 'true' : 'false');
    $('read-mark').textContent = on ? 'Bookmarked' : 'Bookmark';
  }

  function fillContents() {
    var list = $('read-toc-list');
    list.textContent = '';
    book.chapters.forEach(function (c) {
      var li = document.createElement('li');
      var b = document.createElement('button');
      b.type = 'button';
      if (c.index === index) b.className = 'is-here';
      var star = document.createElement('span');
      star.className = 'mark';
      star.textContent = chapterMarked(c.index) ? '●' : '';
      var name = document.createElement('span');
      name.textContent = (c.index + 1) + '. ' + (c.title || 'Chapter');
      b.appendChild(star);
      b.appendChild(name);
      b.addEventListener('click', function () { hideSheets(); load(c.index, 0); });
      li.appendChild(b);
      list.appendChild(li);
    });
  }

  $('read-back').addEventListener('click', function () { savePosition(); book = null; pickList(); });
  $('read-contents-btn').addEventListener('click', function () {
    var openSheet = $('read-contents').hidden;
    hideSheets();
    if (openSheet && book) { fillContents(); show('read-contents', true); showChrome(); }
  });
  $('read-contents-close').addEventListener('click', function () { hideSheets(); showChrome(); });
  $('read-display-btn').addEventListener('click', function () {
    var openSheet = $('read-display').hidden;
    hideSheets();
    if (openSheet) { show('read-display', true); showChrome(); }
  });
  $('read-display-close').addEventListener('click', function () { hideSheets(); showChrome(); });
  $('read-mark').addEventListener('click', function () {
    if (!book) return;
    H.api('POST', '/api/read/' + book.book_id + '/bookmark', {
      index: index, scroll: ratioNow()
    }).then(function (res) {
      if (!res.ok) { setText('read-status', H.errorText(res.data, '', 'bookmark this page')); return; }
      book.bookmarks = res.data.bookmarks || [];
      syncMark();
    });
  });
  $('read-size').addEventListener('input', function () { setFont(parseInt(this.value, 10) || 18, false); });
  $('read-size').addEventListener('change', function () { setFont(parseInt(this.value, 10) || 18, true); });
  $('read-progress').addEventListener('input', function () {
    setText('read-place', placeText(Number(this.value) / 1000));
  });
  $('read-progress').addEventListener('change', function () { seek(Number(this.value) / 1000); });
  document.querySelectorAll('#read-display .chip').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var key = btn.getAttribute('data-pref');
      var field = { reader_theme: 'theme', reader_mode: 'mode', reader_face: 'face',
        reader_leading: 'leading', reader_align: 'align' }[key];
      var next = {};
      next[field] = btn.getAttribute('data-value');
      apply(next, key);
    });
  });

  function zone(ev) {
    var sel = window.getSelection && window.getSelection();
    if (sel && String(sel).length) return;
    if (ev.target.closest && ev.target.closest('a, button, input, label')) return;
    var rect = $('read-leaf').getBoundingClientRect();
    var x = (ev.clientX - rect.left) / Math.max(1, rect.width);
    if (x < 0.28) turn(-1);
    else if (x > 0.72) turn(1);
    else if (root().classList.contains('is-chrome')) root().classList.remove('is-chrome');
    else showChrome();
  }
  $('read-leaf').addEventListener('click', zone);
  $('read-leaf').addEventListener('touchstart', function (ev) {
    touchX = ev.changedTouches[0].clientX;
  }, { passive: true });
  $('read-leaf').addEventListener('touchend', function (ev) {
    var dx = ev.changedTouches[0].clientX - touchX;
    if (Math.abs(dx) > 48) { turn(dx < 0 ? 1 : -1); ev.preventDefault(); }
  });

  document.addEventListener('keydown', function (ev) {
    if (!book || H.view() !== 'read' || $('read-book').hidden) return;
    var tag = ev.target && ev.target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if (ev.key === 'ArrowLeft' || ev.key === 'PageUp') { turn(-1); ev.preventDefault(); }
    else if (ev.key === 'ArrowRight' || ev.key === 'PageDown' || ev.key === ' ') { turn(1); ev.preventDefault(); }
    else if (ev.key === 'Escape') { hideSheets(); root().classList.remove('is-chrome'); }
  });
  $('read-leaf').addEventListener('scroll', function () {
    if (!book) return;
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(function () { showPlace(); savePosition(); }, prefs.mode === 'pages' ? 60 : 700);
  }, { passive: true });
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden' && book && H.view() === 'read') savePosition();
  });
  if (window.ResizeObserver) {
    new ResizeObserver(function () {
      if (!book || $('read-book').hidden) return;
      var page = prefs.mode === 'pages' ? logicalPage : 0;
      layout();
      if (prefs.mode === 'pages') {
        logicalPage = Math.min(page, Math.max(0, pageCount() - 1));
        var leaf = $('read-leaf');
        leaf.style.scrollBehavior = 'auto';
        leaf.scrollLeft = logicalPage * (leaf.clientWidth || 1);
        leaf.style.scrollBehavior = '';
      }
    }).observe($('read-leaf'));
  }

  H.onView('read', function (arg) {
    if (arg && (arg.url || arg.preview_id)) { open(arg); return; }
    if (!book) pickList();
    else document.body.classList.add('is-reading');
  });
  paint();
  window.HuaRead = {
    setFont: setFont,
    applyPrefs: function (s) {
      if (!s) return;
      apply({
        theme: s.reader_theme, mode: s.reader_mode, face: s.reader_face,
        leading: s.reader_leading, align: s.reader_align, font: s.reader_font_pt || fontPt
      }, '');
    }
  };
})();

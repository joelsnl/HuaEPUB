// Read: a paged book in the browser. The place is the same reading.json as the desktop.
//
// Pages mode lays the chapter out in CSS columns (one or two per screen) and moves the column
// strip with a transform, so every page has its own margins and the last spread of a chapter
// never gets clamped half-way. Scroll mode is a single column that scrolls.
(function () {
  'use strict';
  var H = window.Hua, $ = H.$, setText = H.setText, show = H.show;

  var PREF_FIELDS = {
    reader_theme: 'theme', reader_mode: 'mode', reader_face: 'face', reader_leading: 'leading',
    reader_align: 'align', reader_spread: 'spread', reader_margin: 'margin'
  };

  var CHOICES = {
    theme: ['paper', 'sepia', 'night'], mode: ['pages', 'scroll'], face: ['serif', 'sans'],
    leading: ['tight', 'normal', 'loose'], align: ['justify', 'left'],
    spread: ['auto', 'one', 'two'], margin: ['narrow', 'normal', 'wide']
  };

  var book = null;
  var index = 0;
  var logicalPage = 0;
  var offset = 0;        // pages mode: how far the column strip is moved left, in px
  var cols = 1;          // pages mode: pages side by side (1, or 2 for a spread)
  var here = 0;          // where we are in this chapter (0..1), kept across resizes
  var fontPt = 18;
  var loadSeq = 0;
  var saveTimer = null;
  var chromeTimer = null;
  var prefs = {
    theme: 'paper', mode: 'pages', face: 'serif', leading: 'normal', align: 'justify',
    spread: 'auto', margin: 'normal'
  };
  var touchX = 0;
  var drag = null;       // a horizontal page drag in progress (pages mode)
  var slideFrame = 0;
  var wheel = { sum: 0, last: 0, turned: 0, spent: false };
  var jumpBack = null;   // { index, ratio } before a jump from Contents, a bookmark or the slider
  var texts = {};        // chapter index -> { html, title } already shown this visit
  var textOrder = [];
  var wake = null;
  var wakeTimer = null;
  var reducedMotion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  var finePointer = window.matchMedia ? window.matchMedia('(hover: hover) and (pointer: fine)') : null;

  function root() { return $('read-book'); }
  function leaf() { return $('read-leaf'); }
  function spread() { return $('read-spread'); }
  function paged() { return prefs.mode === 'pages'; }

  function paint() {
    var el = root();
    el.dataset.theme = prefs.theme;
    el.dataset.face = prefs.face;
    el.dataset.leading = prefs.leading;
    el.dataset.align = prefs.align;
    el.dataset.margin = prefs.margin;
    el.classList.toggle('is-pages', paged());
    el.classList.toggle('is-scroll', !paged());
    document.querySelectorAll('#read-display .chip').forEach(function (btn) {
      var field = PREF_FIELDS[btn.getAttribute('data-pref')];
      btn.classList.toggle('is-on', !!field && prefs[field] === btn.getAttribute('data-value'));
    });
    $('read-size').value = String(fontPt);
    setText('read-size-n', String(fontPt));
    document.documentElement.style.setProperty('--read-size', fontPt + 'px');
  }

  // Two pages side by side when the screen is landscape and each page still holds a
  // comfortable line (a tablet on its side, a laptop, a monitor). "Two" asks for it anyway,
  // except where a page would get too narrow to read (a phone held upright).
  function wantedCols(w, h) {
    if (!paged() || prefs.spread === 'one') return 1;
    var fontPx = fontPt;  // --read-size is fontPt px
    if (prefs.spread === 'two') return w / 2 >= fontPx * 14 ? 2 : 1;
    return (w >= 800 && w > h * 1.05 && w / 2 >= fontPx * 22) ? 2 : 1;
  }

  function layout() {
    var lf = leaf();
    if (!lf) return;
    cols = wantedCols(lf.clientWidth || 1, lf.clientHeight || 1);
    lf.style.setProperty('--cols', String(cols));
    root().classList.toggle('is-two', cols === 2);
  }

  // One screen: both pages of a spread, or the one page.
  function screenWidth() {
    return spread().getBoundingClientRect().width || leaf().clientWidth || 1;
  }

  function columnCount() {
    var colW = screenWidth() / cols;
    return Math.max(1, Math.round(spread().scrollWidth / colW));
  }

  function pageCount() {
    if (!paged()) return 1;
    return Math.max(1, Math.ceil(columnCount() / cols));
  }

  function pageIndex() {
    if (!paged()) return 0;
    var pages = pageCount();
    if (logicalPage > pages - 1) logicalPage = Math.max(0, pages - 1);
    return logicalPage;
  }

  function setOffset(x) {
    offset = x;
    spread().style.transform = x ? 'translate3d(' + (-x) + 'px,0,0)' : '';
  }

  function chapterRatio() {
    if (paged()) {
      var pages = pageCount();
      if (pages <= 1) return book && index >= book.chapters.length - 1 ? 1 : 0;
      return pageIndex() / (pages - 1);
    }
    var lf = leaf();
    var span = Math.max(1, lf.scrollHeight - lf.clientHeight);
    return Math.max(0, Math.min(1, lf.scrollTop / span));
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

  // Bottom margin, like Play Books: how much of this chapter is left.
  function folioText() {
    if (!book || !paged()) return '';
    var left = pageCount() - 1 - pageIndex();
    if (left <= 0) return index >= book.chapters.length - 1 ? 'End of book' : 'Last page in chapter';
    return left + (left === 1 ? ' page' : ' pages') + ' left in chapter';
  }

  function showPlace() {
    var value = fraction();
    here = chapterRatio();
    $('read-progress').value = String(Math.round(value * 1000));
    setText('read-place', placeText(value, index));
    var ch = book && book.chapters[index];
    setText('read-running', ch ? ((index + 1) + '  ·  ' + (ch.title || '')) : '');
    setText('read-folio', folioText());
    syncMark();
  }

  function reveal(scroll) {
    var lf = leaf();
    var ratio = Math.max(0, Math.min(1, Number(scroll) || 0));
    cancelAnimationFrame(slideFrame);
    if (paged()) {
      var pages = pageCount();
      var page = pages <= 1 ? 0 : Math.round(ratio >= 0.999 ? pages - 1 : ratio * (pages - 1));
      logicalPage = page;
      setOffset(page * screenWidth());
    } else {
      setOffset(0);
      lf.style.scrollBehavior = 'auto';
      var span = Math.max(0, lf.scrollHeight - lf.clientHeight);
      lf.scrollTop = span * ratio;
      lf.style.scrollBehavior = '';
    }
    showPlace();
  }

  // Ease the page sideways like Play Books' slide turn (ease-out, about a quarter second).
  function slideTo(left) {
    cancelAnimationFrame(slideFrame);
    var from = offset;
    var dist = left - from;
    if ((reducedMotion && reducedMotion.matches) || Math.abs(dist) < 1) { setOffset(left); return; }
    var ms = 140 + 140 * Math.min(1, Math.abs(dist) / screenWidth());
    var start = performance.now();
    function step(now) {
      var t = Math.min(1, (now - start) / ms);
      setOffset(from + dist * (1 - Math.pow(1 - t, 3)));
      if (t < 1) slideFrame = requestAnimationFrame(step);
    }
    slideFrame = requestAnimationFrame(step);
  }

  function goToPage(page, animate) {
    logicalPage = page;
    var left = page * screenWidth();
    if (animate) { slideTo(left); return; }
    cancelAnimationFrame(slideFrame);
    setOffset(left);
  }

  function afterMove() {
    showPlace();
    keepAwake();
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(savePosition, 600);
  }

  function hideSheets() {
    show('read-contents', false);
    show('read-display', false);
    root().classList.remove('has-sheet');
  }

  function openSheet(id) {
    hideSheets();
    show(id, true);
    root().classList.add('has-sheet');
    showChrome();
  }

  function sheetOpen() {
    return !$('read-contents').hidden || !$('read-display').hidden;
  }

  function showChrome() {
    root().classList.add('is-chrome');
    if (chromeTimer) clearTimeout(chromeTimer);
    chromeTimer = setTimeout(function hideLater() {
      // A mouse resting on the bar keeps it up.
      var over = document.querySelector('#read-book .chrome:hover');
      if (over) { chromeTimer = setTimeout(hideLater, 1200); return; }
      if (!sheetOpen()) root().classList.remove('is-chrome');
    }, 3200);
  }

  function apply(next, persistKey) {
    var kept = book ? chapterRatio() : 0;
    Object.keys(PREF_FIELDS).forEach(function (key) {
      var field = PREF_FIELDS[key];
      if (CHOICES[field].indexOf(next[field]) >= 0) prefs[field] = next[field];
    });
    if (typeof next.font === 'number') fontPt = Math.max(12, Math.min(36, next.font));
    paint();
    layout();
    if (book) requestAnimationFrame(function () { layout(); reveal(kept); });
    if (persistKey) saveLocal();
  }

  function setFont(pt, persist) {
    apply({ font: pt }, persist ? 'reader_font_pt' : '');
  }

  // Display settings belong to this device: a phone and a monitor want different text sizes
  // and margins. Until a device picks its own, it starts from the PC's reader settings.
  var LOCAL_KEY = 'huaepub-reader';

  function loadLocal() {
    try {
      var raw = JSON.parse(window.localStorage.getItem(LOCAL_KEY) || 'null');
      return raw && typeof raw === 'object' ? raw : null;
    } catch (e) { return null; }
  }

  function saveLocal() {
    var row = { font: fontPt };
    Object.keys(PREF_FIELDS).forEach(function (key) { row[PREF_FIELDS[key]] = prefs[PREF_FIELDS[key]]; });
    try { window.localStorage.setItem(LOCAL_KEY, JSON.stringify(row)); } catch (e) { /* private mode */ }
  }

  function pickList() {
    document.body.classList.remove('is-reading', 'dock-open');
    H.refresh();  // redraw the job panel for the page view (it had the reader's buttons)
    show('read-pick', true);
    show('read-book', false);
    hideSheets();
    letSleep();
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
    texts = {};
    textOrder = [];
    here = 0;
    setJumpBack(null);
    setText('read-title', 'Opening…');
    setText('read-kind', '');
    setText('read-status', '');
    setText('read-running', '');
    setText('read-folio', '');
    $('read-text').textContent = '';
    show('read-end', false);
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
      keepAwake();
      load(jump !== null ? jump : (book.index || 0), jump !== null ? 0 : (book.scroll || 0));
    }).catch(function () { setText('read-status', H.ERRORS.network); });
  }

  // Chapters already shown stay in memory, so turning back a chapter is instant.
  // An EPUB's next chapter is read ahead (it is on disk; cached books are fetched or
  // translated by the PC's own prefetch, so asking early could hold the reader slot).
  function remember(i, data) {
    texts[i] = data;
    textOrder = textOrder.filter(function (n) { return n !== i; });
    textOrder.push(i);
    while (textOrder.length > 8) delete texts[textOrder.shift()];
  }

  function readAhead(i) {
    if (!book || book.kind !== 'epub' || i >= book.chapters.length || texts[i]) return;
    var id = book.book_id;
    H.api('GET', '/api/read/' + id + '/chapter/' + i).then(function (res) {
      if (res.ok && book && book.book_id === id && !res.data.note) {
        remember(i, { html: res.data.html, title: res.data.title });
      }
    }).catch(function () {});
  }

  function langOf(html) {
    // Hyphenation needs a language; an untranslated chapter is mostly Han characters.
    var sample = String(html || '').replace(/<[^>]*>/g, '').slice(0, 1500);
    var han = (sample.match(/[㐀-鿿]/g) || []).length;
    return han > sample.length * 0.3 ? 'zh' : 'en';
  }

  function showText(data, scroll) {
    if (data.title) book.chapters[index].title = data.title;
    $('read-text').innerHTML = data.html;
    $('read-text').setAttribute('lang', langOf(data.html));
    var next = book.chapters[index + 1];
    show('read-end', !!next);
    setText('read-end-title', next ? (next.title || ('Chapter ' + (index + 2))) : '');
    layout();
    requestAnimationFrame(function () {
      layout();
      requestAnimationFrame(function () { reveal(scroll); savePosition(); });
    });
  }

  function load(i, scroll) {
    if (!book || !book.chapters.length) return;
    index = Math.max(0, Math.min(i, book.chapters.length - 1));
    var seq = ++loadSeq;
    var ready = book.chapters[index] && book.chapters[index].ready;
    if (paged()) { logicalPage = 0; setOffset(0); }
    var ch = book.chapters[index];
    setText('read-running', ch ? ((index + 1) + '  ·  ' + (ch.title || '')) : '');
    setText('read-folio', '');
    syncMark();
    keepAwake();
    if (texts[index]) {
      setText('read-status', '');
      showText(texts[index], scroll);
      readAhead(index + 1);
      return;
    }
    setText('read-status', ready ? '' : 'Fetching this chapter from the site…');
    H.api('GET', '/api/read/' + book.book_id + '/chapter/' + index).then(function (res) {
      if (seq !== loadSeq) return;
      if (!res.ok) {
        if (res.status === 404 && res.data && res.data.error === 'book_closed') { open({ url: book.url || '' }); return; }
        setText('read-status', H.errorText(res.data, '', 'load this chapter'));
        return;
      }
      book.chapters[index].ready = true;
      setText('read-status', res.data.note || '');
      var data = { html: res.data.html, title: res.data.title };
      if (!res.data.note) remember(index, data);
      showText(data, scroll);
      readAhead(index + 1);
    }).catch(function () { if (seq === loadSeq) setText('read-status', H.ERRORS.network); });
  }

  function turn(dir) {
    hideSheets();
    root().classList.remove('is-chrome');
    if (!book) return;
    if (!paged()) {
      var lf = leaf();
      var step = lf.clientHeight * 0.86;
      if (dir < 0 && lf.scrollTop <= 2) { load(index - 1, 1); return; }
      if (dir > 0 && lf.scrollTop >= lf.scrollHeight - lf.clientHeight - 2) {
        load(index + 1, 0); return;
      }
      lf.scrollTo({ top: lf.scrollTop + dir * step, behavior: reducedMotion && reducedMotion.matches ? 'auto' : 'smooth' });
      afterMove();
      return;
    }
    var page = pageIndex() + dir;
    var pages = pageCount();
    if (page < 0) { if (index > 0) load(index - 1, 1); return; }
    if (page >= pages) { if (index < book.chapters.length - 1) load(index + 1, 0); return; }
    goToPage(page, true);
    afterMove();
  }

  function chapterEdge(end) {
    if (!book) return;
    if (paged()) { goToPage(end ? pageCount() - 1 : 0, true); afterMove(); return; }
    leaf().scrollTo({ top: end ? leaf().scrollHeight : 0, behavior: 'auto' });
    afterMove();
  }

  // After a jump (Contents, a bookmark, the slider) offer the way back, like Kindle's
  // "Go back to page".
  function setJumpBack(place) {
    jumpBack = place;
    show('read-return', !!place);
    if (place) setText('read-return', 'Back to ' + (place.index + 1));
  }

  function jumpTo(i, ratio) {
    if (!book) return;
    // Exploring with several jumps still leads back to where reading stopped.
    if (!jumpBack) setJumpBack({ index: index, ratio: chapterRatio() });
    if (i === index) { reveal(ratio); afterMove(); } else load(i, ratio);
  }

  function seek(value) {
    if (!book || !book.chapters.length) return;
    var n = book.chapters.length;
    var ch = Math.min(n - 1, Math.floor(value * n));
    jumpTo(ch, value * n - ch);
  }

  function savePosition() {
    if (!book) return;
    H.api('POST', '/api/read/' + book.book_id + '/position', {
      index: index, scroll: chapterRatio()
    }).catch(function () {});
  }

  // Keep the screen on while reading, like an e-reader app, where the browser allows it
  // (HTTPS or this PC itself). Five quiet minutes let it sleep again.
  function keepAwake() {
    if (wakeTimer) clearTimeout(wakeTimer);
    wakeTimer = setTimeout(letSleep, 5 * 60 * 1000);
    if (wake || !navigator.wakeLock || document.visibilityState !== 'visible') return;
    navigator.wakeLock.request('screen').then(function (lock) {
      wake = lock;
      lock.addEventListener('release', function () { if (wake === lock) wake = null; });
    }).catch(function () {});
  }

  function letSleep() {
    if (wakeTimer) { clearTimeout(wakeTimer); wakeTimer = null; }
    if (wake) { var lock = wake; wake = null; lock.release().catch(function () {}); }
  }

  function chapterMarked(i) {
    return (book.bookmarks || []).some(function (m) { return m.chapter_index === i; });
  }

  function thisPageMarked() {
    var scroll = Math.round(chapterRatio() * 100) / 100;
    return (book.bookmarks || []).some(function (m) {
      return m.chapter_index === index && Math.round(Number(m.scroll) * 100) / 100 === scroll;
    });
  }

  function syncMark() {
    var on = !!book && thisPageMarked();
    $('read-mark').classList.toggle('is-on', on);
    $('read-mark').setAttribute('aria-pressed', on ? 'true' : 'false');
    $('read-mark').textContent = on ? 'Bookmarked' : 'Bookmark';
  }

  function fillMarks() {
    var list = $('read-marks-list');
    list.textContent = '';
    var marks = (book.bookmarks || []).slice().sort(function (a, b) {
      return a.chapter_index - b.chapter_index || Number(a.scroll) - Number(b.scroll);
    });
    show('read-marks', marks.length > 0);
    marks.forEach(function (m) {
      var ch = book.chapters[m.chapter_index];
      if (!ch) return;
      var li = document.createElement('li');
      var b = document.createElement('button');
      b.type = 'button';
      var dot = H.el('span', 'mark', '●');
      var name = H.el('span', 'toc-name', (m.chapter_index + 1) + '. ' + (ch.title || 'Chapter'));
      var at = H.el('span', 'toc-at', Math.round(Number(m.scroll || 0) * 100) + '%');
      b.appendChild(dot);
      b.appendChild(name);
      b.appendChild(at);
      b.addEventListener('click', function () { hideSheets(); jumpTo(m.chapter_index, Number(m.scroll) || 0); });
      li.appendChild(b);
      list.appendChild(li);
    });
  }

  function fillContents() {
    var list = $('read-toc-list');
    list.textContent = '';
    var hereLi = null;
    book.chapters.forEach(function (c) {
      var li = document.createElement('li');
      var b = document.createElement('button');
      b.type = 'button';
      if (c.index === index) { b.className = 'is-here'; hereLi = li; }
      var star = document.createElement('span');
      star.className = 'mark';
      star.textContent = chapterMarked(c.index) ? '●' : '';
      var name = H.el('span', 'toc-name', (c.index + 1) + '. ' + (c.title || 'Chapter'));
      b.appendChild(star);
      b.appendChild(name);
      b.addEventListener('click', function () { hideSheets(); jumpTo(c.index, 0); });
      li.dataset.n = String(c.index + 1);
      li.dataset.find = (c.title || '').toLowerCase();
      li.appendChild(b);
      list.appendChild(li);
    });
    fillMarks();
    $('read-toc-find').value = '';
    show('read-toc-find', book.chapters.length > 30);
    // Open the list at the chapter being read, not at chapter 1.
    requestAnimationFrame(function () {
      var sheet = $('read-contents');
      if (hereLi) sheet.scrollTop = Math.max(0, hereLi.offsetTop - sheet.clientHeight / 3);
    });
  }

  // A number finds that chapter; words find titles containing them.
  function findChapter() {
    var q = $('read-toc-find').value.trim().toLowerCase();
    var num = /^\d+$/.test(q);
    Array.prototype.forEach.call($('read-toc-list').children, function (li) {
      li.hidden = !!q && (num ? li.dataset.n !== q : li.dataset.find.indexOf(q) < 0);
    });
    show('read-marks', !q && (book.bookmarks || []).length > 0);
  }

  function fullscreenOn() {
    return !!(document.fullscreenElement || document.webkitFullscreenElement);
  }

  function syncFullscreen() {
    var btn = $('read-full');
    var can = document.fullscreenEnabled || document.webkitFullscreenEnabled;
    show(btn, !!can);
    btn.textContent = fullscreenOn() ? 'Exit full screen' : 'Full screen';
    btn.setAttribute('aria-pressed', fullscreenOn() ? 'true' : 'false');
  }

  function toggleFullscreen() {
    var doc = document.documentElement;
    if (fullscreenOn()) {
      (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      return;
    }
    var req = doc.requestFullscreen || doc.webkitRequestFullscreen;
    if (!req) return;
    var p = req.call(doc);
    if (p && p.catch) p.catch(function () {});
  }

  $('read-back').addEventListener('click', function () {
    savePosition();
    book = null;
    if (fullscreenOn()) toggleFullscreen();
    pickList();
  });
  $('read-contents-btn').addEventListener('click', function () {
    if (!$('read-contents').hidden) { hideSheets(); return; }
    if (book) { fillContents(); openSheet('read-contents'); }
  });
  $('read-contents-close').addEventListener('click', function () { hideSheets(); showChrome(); });
  $('read-display-btn').addEventListener('click', function () {
    if (!$('read-display').hidden) { hideSheets(); return; }
    openSheet('read-display');
  });
  $('read-display-close').addEventListener('click', function () { hideSheets(); showChrome(); });
  $('read-toc-find').addEventListener('input', findChapter);
  $('read-return').addEventListener('click', function () {
    var place = jumpBack;
    setJumpBack(null);
    if (!place || !book) return;
    if (place.index === index) { reveal(place.ratio); afterMove(); } else load(place.index, place.ratio);
  });
  $('read-full').addEventListener('click', toggleFullscreen);
  document.addEventListener('fullscreenchange', syncFullscreen);
  document.addEventListener('webkitfullscreenchange', syncFullscreen);
  $('read-prev').addEventListener('click', function () { turn(-1); });
  $('read-next').addEventListener('click', function () { turn(1); });
  $('read-end-btn').addEventListener('click', function () { if (book) load(index + 1, 0); });
  $('read-mark').addEventListener('click', function () {
    if (!book) return;
    H.api('POST', '/api/read/' + book.book_id + '/bookmark', {
      index: index, scroll: chapterRatio()
    }).then(function (res) {
      if (!res.ok) { setText('read-status', H.errorText(res.data, '', 'bookmark this page')); return; }
      book.bookmarks = res.data.bookmarks || [];
      syncMark();
    });
  });
  $('read-size').addEventListener('input', function () { setFont(parseInt(this.value, 10) || 18, false); });
  $('read-size').addEventListener('change', function () { setFont(parseInt(this.value, 10) || 18, true); });
  $('read-smaller').addEventListener('click', function () { setFont(fontPt - 1, true); });
  $('read-bigger').addEventListener('click', function () { setFont(fontPt + 1, true); });
  $('read-progress').addEventListener('input', function () {
    setText('read-place', placeText(Number(this.value) / 1000));
  });
  $('read-progress').addEventListener('change', function () { seek(Number(this.value) / 1000); });
  document.querySelectorAll('#read-display .chip').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var key = btn.getAttribute('data-pref');
      var next = {};
      next[PREF_FIELDS[key]] = btn.getAttribute('data-value');
      apply(next, key);
    });
  });

  function zone(ev) {
    var sel = window.getSelection && window.getSelection();
    if (sel && String(sel).length) return;
    if (ev.target.closest && ev.target.closest('a, button, input, label')) return;
    if (sheetOpen()) { hideSheets(); return; }
    var rect = leaf().getBoundingClientRect();
    var x = (ev.clientX - rect.left) / Math.max(1, rect.width);
    if (x < 0.28) turn(-1);
    else if (x > 0.72) turn(1);
    else if (root().classList.contains('is-chrome')) root().classList.remove('is-chrome');
    else showChrome();
  }
  leaf().addEventListener('click', zone);

  // Mouse: the bars come up when the pointer reaches the top or bottom edge, as in Play Books
  // on the web; the side arrows (CSS, fine pointers only) turn pages.
  root().addEventListener('mousemove', function (ev) {
    if (!book || (finePointer && !finePointer.matches)) return;
    var h = window.innerHeight || 1;
    if (ev.clientY < 72 || ev.clientY > h - 96) showChrome();
  });

  // Mouse wheel and touchpad in Pages mode: one notch, or one swipe, turns one page.
  // A touchpad flick keeps sending events for a while; that tail must not turn again.
  leaf().addEventListener('wheel', function (ev) {
    if (!book || !paged() || ev.ctrlKey) return;
    ev.preventDefault();
    var unit = ev.deltaMode === 1 ? 32 : (ev.deltaMode === 2 ? (leaf().clientHeight || 600) : 1);
    var dx = ev.deltaX * unit;
    var dy = ev.deltaY * unit;
    var d = Math.abs(dx) > Math.abs(dy) ? dx : dy;
    var now = ev.timeStamp || performance.now();
    var gap = now - wheel.last;
    wheel.last = now;
    if (gap > 220) { wheel.sum = 0; wheel.spent = false; }
    var notch = ev.deltaMode !== 0 || (Math.abs(d) >= 50 && gap > 40);
    if (notch) {
      if (now - wheel.turned < 160) return;
    } else if (wheel.spent) {
      return;
    }
    wheel.sum += d;
    if (notch || Math.abs(wheel.sum) >= 40) {
      var dir = wheel.sum > 0 ? 1 : -1;
      wheel.sum = 0;
      wheel.spent = true;
      wheel.turned = now;
      turn(dir);
    }
  }, { passive: false });

  // Pages mode: the page follows the finger, then slides on or springs back.
  // Scroll mode keeps the simple swipe-to-turn.
  leaf().addEventListener('touchstart', function (ev) {
    touchX = ev.changedTouches[0].clientX;
    drag = null;
    if (!book || !paged() || ev.touches.length > 1) return;
    cancelAnimationFrame(slideFrame);
    var t = ev.touches[0];
    drag = { x: t.clientX, y: t.clientY, at: ev.timeStamp, axis: '', left: logicalPage * screenWidth() };
  }, { passive: true });
  leaf().addEventListener('touchmove', function (ev) {
    if (!drag) return;
    var t = ev.touches[0];
    var dx = t.clientX - drag.x;
    var dy = t.clientY - drag.y;
    if (!drag.axis) {
      if (Math.max(Math.abs(dx), Math.abs(dy)) < 8) return;
      drag.axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
    }
    if (drag.axis !== 'x') return;
    ev.preventDefault();
    // Past the first or last page the strip gives a little (rubber band); release turns the chapter.
    var x = drag.left - dx;
    var max = (pageCount() - 1) * screenWidth();
    if (x < 0) x = x / 3;
    else if (x > max) x = max + (x - max) / 3;
    setOffset(x);
  }, { passive: false });
  leaf().addEventListener('touchend', function (ev) {
    var dx = ev.changedTouches[0].clientX - touchX;
    if (!drag) {
      if (!paged() && Math.abs(dx) > 48) { turn(dx < 0 ? 1 : -1); ev.preventDefault(); }
      return;
    }
    var d = drag;
    drag = null;
    if (d.axis !== 'x') return;
    ev.preventDefault();
    var width = screenWidth();
    var speed = Math.abs(dx) / Math.max(1, ev.timeStamp - d.at);
    var dir = dx < 0 ? 1 : -1;
    var page = logicalPage + dir;
    if (Math.abs(dx) > width * 0.2 || (Math.abs(dx) > 30 && speed > 0.35)) {
      if (page >= 0 && page < pageCount()) {
        hideSheets();
        root().classList.remove('is-chrome');
        goToPage(page, true);
        afterMove();
        return;
      }
      slideTo(d.left);
      turn(dir);
      return;
    }
    slideTo(d.left);
  });
  leaf().addEventListener('touchcancel', function () {
    if (drag && drag.axis === 'x') slideTo(drag.left);
    drag = null;
  });

  document.addEventListener('keydown', function (ev) {
    if (!book || H.view() !== 'read' || $('read-book').hidden) return;
    if (ev.altKey || ev.ctrlKey || ev.metaKey) return;
    var tag = ev.target && ev.target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
      if (ev.key === 'Escape') { ev.target.blur(); hideSheets(); }
      return;
    }
    if (document.body.classList.contains('dock-open')) return;
    var key = ev.key;
    if (key === 'Escape') { hideSheets(); root().classList.remove('is-chrome'); return; }
    if (sheetOpen()) return;  // arrows scroll the open list
    // Space must turn the page, not press the button that last had focus.
    if (key === ' ' && tag === 'BUTTON') ev.target.blur();
    var back = key === 'ArrowLeft' || key === 'PageUp' || (key === ' ' && ev.shiftKey);
    var fwd = key === 'ArrowRight' || key === 'PageDown' || (key === ' ' && !ev.shiftKey);
    if (paged()) {
      if (key === 'ArrowUp') back = true;
      if (key === 'ArrowDown') fwd = true;
    } else if (key === 'ArrowUp' || key === 'ArrowDown') {
      leaf().scrollBy({ top: key === 'ArrowUp' ? -64 : 64 });
      ev.preventDefault();
      return;
    }
    if (back) { turn(-1); ev.preventDefault(); }
    else if (fwd) { turn(1); ev.preventDefault(); }
    else if (key === 'Home') { chapterEdge(false); ev.preventDefault(); }
    else if (key === 'End') { chapterEdge(true); ev.preventDefault(); }
  });
  leaf().addEventListener('scroll', function () {
    if (!book || paged()) return;
    keepAwake();
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(function () { showPlace(); savePosition(); }, 700);
  }, { passive: true });
  document.addEventListener('visibilitychange', function () {
    if (!book || H.view() !== 'read') return;
    if (document.visibilityState === 'hidden') savePosition();
    else keepAwake();  // the browser drops the wake lock while the tab is hidden
  });
  setInterval(function () {
    if (!book || $('read-book').hidden) return;
    H.api('POST', '/api/read/' + book.book_id + '/touch', {}).catch(function () {});
  }, 45000);
  if (window.ResizeObserver) {
    // Rotating a tablet or resizing the window keeps the place in the chapter, not the page number.
    new ResizeObserver(function () {
      if (!book || $('read-book').hidden || drag) return;
      var kept = here;
      layout();
      reveal(kept);
    }).observe(leaf());
  }

  // Web fonts arriving after the first layout change how many pages a chapter has.
  if (document.fonts && document.fonts.addEventListener) {
    document.fonts.addEventListener('loadingdone', function () {
      if (!book || $('read-book').hidden || drag) return;
      var kept = here;
      layout();
      reveal(kept);
    });
  }

  H.onView('read', function (arg) {
    if (arg && (arg.url || arg.preview_id)) { open(arg); return; }
    if (!book) pickList();
    else { document.body.classList.add('is-reading'); keepAwake(); }
  });
  if (loadLocal()) apply(loadLocal(), '');
  else paint();
  syncFullscreen();
  window.HuaRead = {
    setFont: setFont,
    applyPrefs: function (s) {
      var mine = loadLocal();
      if (mine) { apply(mine, ''); return; }
      if (!s) return;
      apply({
        theme: s.reader_theme, mode: s.reader_mode, face: s.reader_face,
        leading: s.reader_leading, align: s.reader_align, font: s.reader_font_pt || fontPt
      }, '');
    }
  };
})();

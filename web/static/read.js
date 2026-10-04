// Read: the PC's EPUB (or cached chapters) in the browser, sharing reading.json with the desktop.
(function () {
  'use strict';
  var H = window.Hua, $ = H.$, setText = H.setText, show = H.show;

  var book = null;     // {book_id, title, kind, chapters[], index, scroll}
  var index = 0;
  var fontPt = 18;
  var loadSeq = 0;
  var saveTimer = null;

  function pickList() {
    show('read-pick', true);
    show('read-book', false);
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
          (e.has_epub ? ' · EPUB' : (e.on_drive ? ' · on Drive' : ' · cached chapters'))));
        b.addEventListener('click', function () { open({ url: e.url }); });
        li.appendChild(b);
        list.appendChild(li);
      });
    });
  }

  function open(arg) {
    show('read-pick', false);
    show('read-book', true);
    setText('read-title', 'Opening…');
    setText('read-kind', '');
    setText('read-status', '');
    $('read-text').textContent = '';
    H.api('POST', '/api/read/open', arg).then(function (res) {
      if (!res.ok) {
        setText('read-title', 'Could not open this book');
        setText('read-status', H.errorText(res.data));
        return;
      }
      book = res.data;
      setText('read-title', book.title);
      setText('read-kind', book.kind === 'epub' ? 'EPUB' : 'Cached chapters');
      var toc = $('read-toc');
      toc.textContent = '';
      book.chapters.forEach(function (c) {
        var o = document.createElement('option');
        o.value = String(c.index);
        o.textContent = (c.index + 1) + '. ' + c.title;
        toc.appendChild(o);
      });
      load(book.index || 0, book.scroll || 0);
    }).catch(function () { setText('read-status', H.ERRORS.network); });
  }

  function navState() {
    var n = book ? book.chapters.length : 0;
    ['read-prev', 'read-prev2'].forEach(function (id) { $(id).disabled = index <= 0; });
    ['read-next', 'read-next2'].forEach(function (id) { $(id).disabled = index >= n - 1; });
    $('read-toc').value = String(index);
  }

  function load(i, scroll) {
    if (!book) return;
    index = Math.max(0, Math.min(i, book.chapters.length - 1));
    navState();
    var seq = ++loadSeq;
    var ready = book.chapters[index] && book.chapters[index].ready;
    setText('read-status', ready ? '' : 'Fetching this chapter from the site…');
    H.api('GET', '/api/read/' + book.book_id + '/chapter/' + index).then(function (res) {
      if (seq !== loadSeq) return;
      if (!res.ok) {
        if (res.status === 404 && res.data && res.data.error === 'book_closed') { open({ url: book.url || '' }); return; }
        setText('read-status', H.errorText(res.data));
        return;
      }
      book.chapters[index].ready = true;
      var article = $('read-text');
      // Server-sanitised HTML (scripts, handlers and links removed); CSP blocks inline script anyway.
      article.innerHTML = res.data.html;
      setText('read-status', res.data.note || '');
      var top = article.getBoundingClientRect().top + window.scrollY - 12;
      if (scroll && scroll > 0) {
        window.scrollTo(0, top + scroll * Math.max(0, article.scrollHeight - window.innerHeight));
      } else {
        window.scrollTo(0, Math.max(0, top - 140));
      }
      savePosition();
    }).catch(function () { if (seq === loadSeq) setText('read-status', H.ERRORS.network); });
  }

  function ratio() {
    var article = $('read-text');
    var top = article.getBoundingClientRect().top + window.scrollY;
    var span = Math.max(1, article.scrollHeight - window.innerHeight);
    return Math.max(0, Math.min(1, (window.scrollY - top) / span));
  }

  function savePosition() {
    if (!book) return;
    H.api('POST', '/api/read/' + book.book_id + '/position', { index: index, scroll: ratio() }).catch(function () {});
  }

  function setFont(pt, persist) {
    fontPt = Math.max(12, Math.min(36, pt));
    document.documentElement.style.setProperty('--read-size', fontPt + 'px');
    if (persist) H.api('PUT', '/api/settings', { reader_font_pt: fontPt }).catch(function () {});
  }

  $('read-back').addEventListener('click', function () { savePosition(); book = null; pickList(); });
  $('read-prev').addEventListener('click', function () { load(index - 1, 0); });
  $('read-next').addEventListener('click', function () { load(index + 1, 0); });
  $('read-prev2').addEventListener('click', function () { load(index - 1, 0); });
  $('read-next2').addEventListener('click', function () { load(index + 1, 0); });
  $('read-toc').addEventListener('change', function () { load(parseInt($('read-toc').value, 10) || 0, 0); });
  $('read-smaller').addEventListener('click', function () { setFont(fontPt - 1, true); });
  $('read-larger').addEventListener('click', function () { setFont(fontPt + 1, true); });
  window.addEventListener('scroll', function () {
    if (!book || H.view() !== 'read') return;
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(savePosition, 1500);
  }, { passive: true });
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden' && book && H.view() === 'read') savePosition();
  });

  H.onView('read', function (arg) {
    if (arg && (arg.url || arg.preview_id)) { open(arg); return; }
    if (!book) pickList();
  });
  window.HuaRead = { setFont: setFont };
})();

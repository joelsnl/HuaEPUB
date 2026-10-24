// Stats: a "wrapped"-style summary built from /api/stats. Loaded once per visit to the page.
(function () {
  'use strict';
  var H = window.Hua, $ = H.$, setText = H.setText, show = H.show, el = H.el;

  function fmt(n) { return (n || 0).toLocaleString(); }
  function word(n, singular, plural) { return n === 1 ? singular : (plural || singular + 's'); }

  function dateText(ts) {
    if (!ts) return '';
    return new Date(ts * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }

  function tile(num, label, sub) {
    var card = el('div', 'stat-tile card');
    card.appendChild(el('span', 'stat-num', num));
    card.appendChild(el('span', 'stat-label', label));
    if (sub) card.appendChild(el('span', 'stat-sub', sub));
    return card;
  }

  function render(data) {
    var books = data.books, chapters = data.chapters, text = data.text, sites = data.sites;
    var empty = books.tracked === 0 && books.shelved === 0;
    show('stats-empty', empty);
    show('stats-full', !empty);
    if (empty) return;

    var heroNum = chapters.read > 0 ? chapters.read : chapters.in_library;
    setText('stats-hero-num', fmt(heroNum));
    setText('stats-hero-label', chapters.read > 0 ? 'chapters read, all time' : 'chapters in your library');
    var streakText = data.reading_streak_days >= 2
      ? 'Reading streak: ' + fmt(data.reading_streak_days) + ' days running'
      : (data.reading_streak_days === 1 ? 'You read today — keep it going.' : '');
    setText('stats-hero-sub', streakText);

    var grid = $('stats-grid');
    grid.textContent = '';
    grid.appendChild(tile(fmt(books.tracked), word(books.tracked, 'book') + ' in your library',
      fmt(books.finished) + ' finished · ' + fmt(books.reading) + ' in progress'));
    if (data.currently_reading) {
      grid.appendChild(tile(data.currently_reading.percent + '%', 'through your current read',
        data.currently_reading.title));
    }
    if (data.most_read_book) {
      grid.appendChild(tile(fmt(data.most_read_book.chapters), 'chapters in your most-read book',
        data.most_read_book.title));
    }
    if (data.favorite_author) {
      grid.appendChild(tile(fmt(data.favorite_author.chapters), 'chapters from your favorite author',
        data.favorite_author.name));
    }
    if (data.average_completion_percent > 0) {
      grid.appendChild(tile(data.average_completion_percent + '%',
        'average, how far into the books you\'ve started',
        fmt(books.reading + books.finished) + ' books started'));
    }
    if (text.characters_read > 0) {
      grid.appendChild(tile(fmt(text.characters_read), 'characters you\'ve actually read',
        'estimated from chapters marked read, not just cached'));
    }
    grid.appendChild(tile(fmt(chapters.cached), 'chapters downloaded', 'cached on this PC right now'));
    if (text.characters_cached > 0) {
      grid.appendChild(tile(fmt(text.characters_cached), 'characters downloaded',
        'estimated from what is cached now'));
    }
    if (text.translated_words > 0) {
      grid.appendChild(tile(fmt(text.translated_words), 'words translated', 'estimated from the translation cache'));
    }
    if (books.authors > 0) {
      grid.appendChild(tile(fmt(books.authors), word(books.authors, 'author') + ' in your library'));
    }
    if (sites.count > 0) {
      grid.appendChild(tile(fmt(sites.count), word(sites.count, 'site') + " you've used",
        sites.top ? 'Most from ' + sites.top + ' (' + fmt(sites.top_chapters) + ' chapters)' : ''));
    }
    if (data.longest_book) {
      grid.appendChild(tile(fmt(data.longest_book.chapters), 'chapters in your longest book',
        data.longest_book.title));
    }
    if (data.most_recent_book) {
      grid.appendChild(tile(dateText(data.most_recent_book.at) || '—', 'most recently downloaded',
        data.most_recent_book.title));
    }
    if (data.notes > 0) {
      grid.appendChild(tile(fmt(data.notes), word(data.notes, 'note') + ' saved'));
    }
    if (data.bookmarks > 0) {
      grid.appendChild(tile(fmt(data.bookmarks), word(data.bookmarks, 'bookmark') + ' saved'));
    }
    if (data.glossary_terms > 0) {
      grid.appendChild(tile(fmt(data.glossary_terms), word(data.glossary_terms, 'name', 'names') + ' & terms your translator has learned'));
    }
    if (books.untouched > 0) {
      grid.appendChild(tile(fmt(books.untouched), word(books.untouched, 'book') + " waiting for you to start"));
    }
    if (books.shelved > 0) {
      grid.appendChild(tile(fmt(books.shelved), word(books.shelved, 'book') + ' shelved'));
    }

    setText('stats-foot', 'Characters and translated words are estimates from what is cached right now, ' +
      'not a lifetime total — the cache can drop old chapters to stay under its size limit.');
  }

  function load() {
    show('stats-empty', false);
    show('stats-full', false);
    show('stats-error', false);
    show('stats-loading', true);
    H.api('GET', '/api/stats').then(function (res) {
      show('stats-loading', false);
      if (res.ok) {
        render(res.data);
      } else {
        setText('stats-error', "Couldn't load stats. Try again in a moment.");
        show('stats-error', true);
      }
    }).catch(function () {
      show('stats-loading', false);
      setText('stats-error', "Couldn't load stats. Try again in a moment.");
      show('stats-error', true);
    });
  }

  H.onView('stats', load);
})();

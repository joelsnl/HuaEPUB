// Multi: read many links, then build them one after another.
(function () {
  'use strict';
  var H = window.Hua, $ = H.$, setText = H.setText;

  var rows = [];          // last lookup rows: {url, preview_id, title, title_en, chapters, status, error}
  var lookupTask = null;  // id of the lookup this tab started

  function draw(list, kind) {
    var body = $('multi-rows');
    body.textContent = '';
    if (!list.length) {
      var tr = H.el('tr', 'empty');
      var td = H.el('td', '', 'Nothing read yet.');
      td.colSpan = 3;
      tr.appendChild(td);
      body.appendChild(tr);
      return;
    }
    list.forEach(function (r) {
      var tr = document.createElement('tr');
      var t = H.el('td', 'cell-title');
      var name = r.title_en || r.title || r.url;
      t.appendChild(H.el('span', '', name));
      if (r.title_en && r.title && r.title_en !== r.title) t.appendChild(H.el('span', 'muted small', r.title));
      if (!r.title) t.appendChild(H.el('span', 'muted small mono', r.url || ''));
      tr.appendChild(t);
      tr.appendChild(H.el('td', 'num mono', r.chapters ? String(r.chapters) : '—'));
      var st = H.el('td', 'cell-status', r.status || '');
      if (/fail/i.test(r.status || '')) st.classList.add('flag');
      if (/done|ready|finished/i.test(r.status || '')) st.classList.add('ok');
      if (r.error) st.appendChild(H.el('span', 'muted small', ' ' + r.error));
      tr.appendChild(st);
      body.appendChild(tr);
    });
    body.setAttribute('data-kind', kind || '');
  }

  function readyIds() {
    return rows.filter(function (r) { return r.status === 'Ready' && r.preview_id; })
      .map(function (r) { return r.preview_id; });
  }

  function onState(s) {
    var t = s.task;
    var busy = !!s.busy;
    if (t && t.kind === 'lookup') {
      if (t.id === lookupTask || !rows.length || busy) rows = t.rows || [];
      draw(rows, 'lookup');
    } else if (t && t.kind === 'multi') {
      draw(t.rows || [], 'multi');
    }
    var ready = readyIds().length;
    $('btn-multi-look').disabled = busy;
    $('btn-multi-build').disabled = busy || !ready;
    setText('btn-multi-build', ready ? 'Build ' + H.plural(ready, 'book') : 'Build all');
    if (busy && t && (t.kind === 'lookup' || t.kind === 'multi')) {
      setText('multi-note', t.message || '');
    } else if (t && t.kind === 'multi' && t.state === 'done') {
      setText('multi-note', 'Finished. The books are in your Library; download them from the bar above.');
    } else if (!busy) {
      setText('multi-note', ready ? 'Ready to build. Each book goes into your Library.' : '');
    }
  }

  function lookUp() {
    var text = $('multi-urls').value;
    var urls = text.split(/\s+/).map(function (u) { return u.trim(); }).filter(Boolean);
    if (!urls.length) { setText('multi-note', H.ERRORS.no_links); return; }
    try { localStorage.setItem('huaepub-multi', text); } catch (e) { /* ignore */ }
    H.api('POST', '/api/multi/lookup', { urls: urls }).then(function (res) {
      if (!res.ok) { setText('multi-note', H.errorText(res.data)); return; }
      lookupTask = res.data.task_id;
      rows = [];
      H.refresh();
    }).catch(function () { setText('multi-note', H.ERRORS.network); });
  }

  function buildAll() {
    var ids = readyIds();
    if (!ids.length) return;
    H.api('POST', '/api/multi/build', { preview_ids: ids }).then(function (res) {
      if (!res.ok) { setText('multi-note', H.errorText(res.data)); return; }
      H.refresh();
    }).catch(function () { setText('multi-note', H.ERRORS.network); });
  }

  $('btn-multi-look').addEventListener('click', lookUp);
  $('btn-multi-build').addEventListener('click', buildAll);
  try { var saved = localStorage.getItem('huaepub-multi'); if (saved) $('multi-urls').value = saved; } catch (e) { /* ignore */ }
  H.onState(onState);
})();

// Start-up: wire shared UI, open the page from the URL hash, then follow the server's live state.
(function () {
  'use strict';
  var H = window.Hua;
  H.init();
  var start = (location.hash || '').replace('#', '');
  H.go(H.VIEWS.indexOf(start) >= 0 ? start : 'library');
  window.addEventListener('hashchange', function () {
    var v = (location.hash || '').replace('#', '');
    if (H.VIEWS.indexOf(v) >= 0 && v !== H.view()) H.go(v);
  });
  if (window.HuaSettings) window.HuaSettings.load();
  H.api('GET', '/api/state').then(function (res) {
    if (res.ok) {
      H.setText('version', res.data.version ? 'v' + res.data.version : '');
      H.setText('brand-tag', res.data.mode === 'remote' ? 'Library server, over HTTPS' : 'Library server');
    }
  }).catch(function () {});
  H.refresh();
  H.connect();
})();

// HuaEPUB server mode: colour theme. Runs in <head> so the page never flashes the wrong palette.
// Auto follows the system (dark = night unit, light = field unit). The choice lives in this
// browser's localStorage only. Older saved looks map to the nearest of the two.
(function () {
  'use strict';
  var KEY = 'huaepub-look';
  // id -> data-theme value (null = follow the system)
  var THEMES = { auto: null, dark: 'dark', light: 'light' };
  var NAMES = { auto: 'Auto', light: 'Light', dark: 'Dark' };
  var LEGACY = { indigo: 'dark', gold: 'dark', cinnabar: 'dark', mist: 'light', surprise: 'auto' };

  function read() {
    try {
      var v = localStorage.getItem(KEY);
      if (v && (v in NAMES)) return v;
      if (v && (v in LEGACY)) return LEGACY[v];
    } catch (e) {}
    return 'auto';
  }
  function write(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }

  function apply(id) {
    var root = document.documentElement;
    var theme = THEMES[id];
    if (theme) root.setAttribute('data-theme', theme); else root.removeAttribute('data-theme');
  }

  window.HuaTheme = {
    names: NAMES,
    get: read,
    set: function (id) { write(id); apply(id); }
  };
  apply(read());
})();

// HuaEPUB server mode: colour theme. Runs in <head> so the page never flashes the wrong palette.
// Auto follows the system (dark = Graphite & Cyan, light = Celadon Day).
// The choice lives in this browser's localStorage only.
(function () {
  'use strict';
  var KEY = 'huaepub-look';
  var OLD_KEY = 'huaepub-simple-look';
  // id -> data-theme value (null = follow the system)
  var THEMES = {
    auto: null, dark: 'graphite', light: 'celadon',
    indigo: 'indigo', gold: 'gold', cinnabar: 'cinnabar', mist: 'mist'
  };
  var NAMES = {
    auto: 'Auto', dark: 'Dark', light: 'Light', indigo: 'Indigo & Jade',
    gold: 'Ink & Gold', cinnabar: 'Cinnabar Night', mist: 'Blue Mist', surprise: 'Surprise me'
  };
  var SURPRISE = ['graphite', 'celadon', 'indigo', 'gold', 'cinnabar', 'mist'];

  function read() {
    try {
      var v = localStorage.getItem(KEY) || localStorage.getItem(OLD_KEY);
      if (v && (v in NAMES)) return v;
    } catch (e) {}
    return 'auto';
  }
  function write(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }

  function apply(id) {
    var root = document.documentElement, theme = null;
    if (id === 'surprise') theme = SURPRISE[Math.floor(Math.random() * SURPRISE.length)];
    else theme = THEMES[id] || null;
    if (theme) root.setAttribute('data-theme', theme); else root.removeAttribute('data-theme');
  }

  window.HuaTheme = {
    names: NAMES,
    get: read,
    set: function (id) { write(id); if (id !== 'surprise') apply(id); else apply('surprise'); }
  };
  apply(read());
})();

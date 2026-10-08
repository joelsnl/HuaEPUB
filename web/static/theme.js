// HuaEPUB server mode: colour theme. Runs in <head> so the page never flashes the wrong palette.
// Auto follows the system (dark = Catalogue at night, light = Catalogue).
// The choice lives in this browser's localStorage only.
(function () {
  'use strict';
  var KEY = 'huaepub-look';
  // id -> data-theme value (null = follow the system)
  var THEMES = {
    auto: null, dark: 'graphite', light: 'celadon',
    indigo: 'indigo', gold: 'gold', cinnabar: 'cinnabar', mist: 'mist'
  };
  var NAMES = {
    auto: 'Auto', dark: 'Catalogue at night', light: 'Catalogue', indigo: 'Indigo & Jade',
    gold: 'Ink & Gold', cinnabar: 'Cinnabar Night', mist: 'Blue Mist', surprise: 'Surprise me'
  };
  var SURPRISE = ['graphite', 'celadon', 'indigo', 'gold', 'cinnabar', 'mist'];

  function read() {
    try {
      var v = localStorage.getItem(KEY);
      if (v && (v in NAMES)) return v;
    } catch (e) {}
    return 'auto';
  }
  function write(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }

  function apply(id) {
    var root = document.documentElement;
    var theme = id === 'surprise' ? SURPRISE[Math.floor(Math.random() * SURPRISE.length)] : THEMES[id];
    if (theme) root.setAttribute('data-theme', theme); else root.removeAttribute('data-theme');
  }

  window.HuaTheme = {
    names: NAMES,
    get: read,
    set: function (id) { write(id); apply(id); }
  };
  apply(read());
})();

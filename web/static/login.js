// Sign-in page: access code (your network) or password (remote mode).
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var mode = 'lan';

  function showError(text) {
    $('gate-error').textContent = text;
    $('gate-error').hidden = !text;
  }

  fetch('/api/session', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (s) {
    if (s.signed_in) { window.location.replace('/'); return; }
    mode = s.mode;
    if (mode === 'remote') {
      $('secret-label').textContent = 'Password';
      $('secret').type = 'password';
      $('secret').autocomplete = 'current-password';
      $('gate-hint').textContent = 'The password you set in HuaEPUB on your PC (Server mode).';
    }
    $('secret').focus();
  }).catch(function () { showError('The PC did not answer. Is server mode still on?'); });

  $('gate-form').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var secret = $('secret').value;
    if (!secret) { showError(mode === 'remote' ? 'Type the password.' : 'Type the access code.'); return; }
    $('gate-go').disabled = true;
    showError('');
    fetch('/api/login', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-HuaEPUB': '1' },
      body: JSON.stringify({ secret: secret })
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) { return { status: r.status, data: data }; });
    }).then(function (res) {
      $('gate-go').disabled = false;
      if (res.status === 200) { window.location.replace('/'); return; }
      if (res.status === 429) {
        var mins = Math.max(1, Math.ceil((res.data.retry_after || 60) / 60));
        showError('Too many wrong tries. Wait about ' + mins + ' minute' + (mins === 1 ? '' : 's') + ', then try again.');
        return;
      }
      showError(mode === 'remote' ? 'That password is not right.' : 'That code is not right.');
      $('secret').select();
    }).catch(function () {
      $('gate-go').disabled = false;
      showError('The PC did not answer. Is server mode still on?');
    });
  });
})();

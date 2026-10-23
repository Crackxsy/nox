'use strict';
(function () {
  var T = __STRINGS__;
  var DEVICE_KEY = 'nox.device.v1';
  var lang = (navigator.language || 'en').slice(0, 2).toLowerCase() === 'de' ? 'de' : 'en';
  var info = null;
  var $ = function (id) { return document.getElementById(id); };
  function t(key) { return (T[lang] && T[lang][key]) || T.en[key] || key; }
  function apply() {
    document.documentElement.lang = lang;
    document.title = t('title');
    var nodes = document.querySelectorAll('[data-t]');
    for (var i = 0; i < nodes.length; i++) nodes[i].textContent = t(nodes[i].getAttribute('data-t'));
    $('lang').textContent = t('language');
    $('lang').setAttribute('lang', lang === 'de' ? 'en' : 'de');
    if (info) show(info);
  }
  function state(text) { $('state').textContent = text; }
  var ticket = '';
  // The claim code and the setup portal's one-time ticket arrive in the fragment; both leave the
  // address bar at once, so they never land in history or a bookmark.
  function codeFromHash() {
    var parts = location.hash.replace(/^#/, '').split('&');
    var rest = [], code = '', taken = false;
    for (var i = 0; i < parts.length; i++) {
      var key = parts[i].split('=')[0], value = '';
      try { value = decodeURIComponent(parts[i].slice(key.length + 1)); } catch (e) { value = ''; }
      if (key === 'code') { code = value; taken = true; }
      else if (key === 'ticket') { ticket = value.replace(/[^0-9A-Za-z_-]/g, '').slice(0, 64); taken = true; }
      else if (parts[i]) { rest.push(parts[i]); }
    }
    if (taken) history.replaceState(null, '', location.pathname + (rest.length ? '#' + rest.join('&') : ''));
    return code.replace(/[^0-9A-Za-z-]/g, '').slice(0, 40);
  }
  function show(data) {
    if (data.claimed) { location.replace('/dashboard/'); return; }
    if (data.available === false) { state(t('unavailable')); return; }
    if (!data.window_open) { state(t('closed')); $('form').hidden = true; return; }
    state(t('minutes').replace('{minutes}', String(Math.max(1, Math.round((data.seconds_left || 60) / 60)))));
    $('code-hint').textContent = t(data.code_on_card ? 'code_hint_card' : 'code_hint_device');
    // Kept by the setup portal: shown as such, and the claimant may set others (optional then).
    $('pin-fields').hidden = !(data.needs_pin || data.staged_pin);
    $('passphrase-fields').hidden = !(data.needs_passphrase || data.staged_passphrase);
    $('pin-staged').hidden = !data.staged_pin;
    $('passphrase-staged').hidden = !data.staged_passphrase;
    $('security').hidden = $('pin-fields').hidden && $('passphrase-fields').hidden;
    if (data.ca_fingerprint) { $('fp').textContent = data.ca_fingerprint; $('fp-box').hidden = false; }
    $('form').hidden = false;
  }
  function errors(list) {
    var box = $('errors');
    var inputs = document.querySelectorAll('input');
    for (var i = 0; i < inputs.length; i++) inputs[i].removeAttribute('aria-invalid');
    box.textContent = '';
    if (!list.length) { box.hidden = true; return; }
    var title = document.createElement('p');
    var strong = document.createElement('strong');
    strong.textContent = t('errors');
    title.appendChild(strong);
    box.appendChild(title);
    var ul = document.createElement('ul');
    list.forEach(function (item) {
      var li = document.createElement('li');
      if (item[0]) {
        var a = document.createElement('a');
        a.href = '#' + item[0];
        a.textContent = t('err_' + item[1]);
        li.appendChild(a);
        var field = $(item[0]);
        if (field) field.setAttribute('aria-invalid', 'true');
      } else { li.textContent = t('err_' + item[1]); }
      ul.appendChild(li);
    });
    box.appendChild(ul);
    box.hidden = false;
    box.focus();
  }
  function guessName() {
    var ua = navigator.userAgent || '';
    if (/iPhone/.test(ua)) return 'iPhone';
    if (/iPad/.test(ua)) return 'iPad';
    if (/Android/.test(ua)) return 'Android';
    return '';
  }
  function submit(event) {
    event.preventDefault();
    var problems = [];
    var code = $('code').value.trim();
    if (code.replace(/[\s-]/g, '').length < 8) problems.push(['code', 'code']);
    var body = { code: code, name: $('name').value.trim(), default_name: guessName() };
    if (ticket.length >= 16) body.ticket = ticket;
    if (info && (info.needs_pin || $('pin').value)) {
      if ($('pin').value.length < (info.min_pin || 6)) problems.push(['pin', 'pin_short']);
      else if ($('pin').value !== $('pin2').value) problems.push(['pin2', 'pin_mismatch']);
      body.pin = $('pin').value;
    }
    if (info && (info.needs_passphrase || $('passphrase').value)) {
      if ($('passphrase').value.length < (info.min_passphrase || 12)) problems.push(['passphrase', 'passphrase_short']);
      else if ($('passphrase').value !== $('passphrase2').value) problems.push(['passphrase2', 'passphrase_mismatch']);
      body.passphrase = $('passphrase').value;
    }
    errors(problems);
    if (problems.length) return;
    $('submit').disabled = true;
    state(t('working'));
    fetch('/api/claim', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body), credentials: 'omit', cache: 'no-store'
    }).then(function (response) {
      return response.json().then(function (data) { return [response.status, data]; });
    }).then(function (result) {
      var data = result[1];
      if (result[0] === 201 && data.token) {
        var saved = true;
        try {
          localStorage.setItem(DEVICE_KEY, JSON.stringify({
            token: data.token, clientId: data.client_id, deviceId: data.device_id, name: data.name
          }));
        } catch (e) { saved = false; }
        state(t('done') + (saved ? '' : ' ' + t('not_saved')));
        location.replace(data.next || '/dashboard/');
        return;
      }
      $('submit').disabled = false;
      var code = String(data.error || 'unavailable');
      var field = code === 'wrong_code' ? 'code' : code === 'pin_required' ? 'pin'
        : code === 'passphrase_required' || code === 'passphrase_short' ? 'passphrase' : '';
      errors([[field, T.en['err_' + code] ? code : 'unavailable']]);
      state('');
      if (code === 'claimed') location.replace('/dashboard/');
    }).catch(function () {
      $('submit').disabled = false;
      errors([['', 'network']]);
    });
  }
  function load() {
    state(t('loading'));
    fetch('/api/claim', { credentials: 'omit', cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (data) { info = data; show(data); })
      .catch(function () { state(t('unavailable')); });
  }
  document.addEventListener('DOMContentLoaded', function () {
    $('code').value = codeFromHash();
    $('lang').addEventListener('click', function () { lang = lang === 'de' ? 'en' : 'de'; apply(); });
    $('form').addEventListener('submit', submit);
    apply();
    load();
  });
})();

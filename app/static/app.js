// Небольшие улучшения поверх серверного рендера. Всё работает и без JS (кроме автосохранения и живого статуса).
(function () {
  var csrf = document.querySelector('meta[name="csrf-token"]');
  document.addEventListener('htmx:configRequest', function (e) {
    if (csrf) e.detail.headers['X-CSRF-Token'] = csrf.content;
  });

  // Долгая отправка формы (разбор текста моделью): блокируем кнопку и показываем, что идёт работа
  // Подтверждение необратимых действий (вместо inline onsubmit — их запрещает CSP)
  document.addEventListener('submit', function (e) {
    var msg = e.target.getAttribute('data-confirm');
    if (msg && !window.confirm(msg)) e.preventDefault();
  }, true);
  // Выбор в списке сразу применяет фильтр
  document.addEventListener('change', function (e) {
    if (e.target.matches('select[data-autosubmit]')) e.target.form.submit();
  });
  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (e.defaultPrevented || !f.matches('[data-busy]')) return;
    var b = f.querySelector('button[type=submit]');
    if (b) { b.disabled = true; b.textContent = f.getAttribute('data-busy'); }
  });

  // Переключатель компании в шапке
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-toggle="co-menu"]');
    document.querySelectorAll('.co-switch.open').forEach(function (el) {
      if (!btn || el !== btn.parentElement) el.classList.remove('open');
    });
    if (btn) {
      var wrap = btn.parentElement;
      var open = wrap.classList.toggle('open');
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') document.querySelectorAll('.co-switch.open').forEach(function (el) { el.classList.remove('open'); });
  });

  // Суммы: группировка разрядов при вводе
  document.addEventListener('input', function (e) {
    var el = e.target;
    if (!el.matches('input[data-money]')) return;
    var neg = el.value.trim().startsWith('-') && el.hasAttribute('data-signed');
    var digits = el.value.replace(/[^\d]/g, '');
    var pos = el.value.length - el.selectionStart;
    el.value = (neg ? '-' : '') + digits.replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
    var p = Math.max(0, el.value.length - pos);
    try { el.setSelectionRange(p, p); } catch (_) {}
  });

  // Действующие обязательства: добавить и удалить строку
  document.addEventListener('click', function (e) {
    var add = e.target.closest('[data-add-liability]');
    if (add) {
      var tpl = document.getElementById('liability-row');
      var body = document.getElementById('liabilities-body');
      if (tpl && body) {
        body.appendChild(tpl.content.cloneNode(true));
        var empty = document.getElementById('liabilities-empty');
        if (empty) empty.remove();
        var last = body.lastElementChild.querySelector('input');
        if (last) last.focus();
        body.closest('form').dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
    var del = e.target.closest('[data-del-row]');
    if (del) {
      var form = del.closest('form');
      del.closest('tr').remove();
      if (form) form.dispatchEvent(new Event('change', { bubbles: true }));
    }
  });

  // Загрузка файлов: перетаскивание и список выбранных
  document.querySelectorAll('[data-drop]').forEach(function (zone) {
    var input = zone.querySelector('input[type=file]');
    var list = document.getElementById(zone.getAttribute('data-drop'));
    function render() {
      if (!list) return;
      list.innerHTML = '';
      Array.prototype.forEach.call(input.files, function (f) {
        var row = document.createElement('div');
        row.className = 'file-row';
        var ext = (f.name.split('.').pop() || '').slice(0, 4);
        row.innerHTML = '<span class="file-ic" aria-hidden="true"></span><div style="flex:1;min-width:0;display:flex;flex-direction:column"><span class="m"></span><span class="small muted"></span></div>';
        row.querySelector('.file-ic').textContent = ext;
        row.querySelector('.m').textContent = f.name;
        row.querySelector('.small').textContent = Math.max(1, Math.round(f.size / 1024)) + ' КБ · будет отправлен';
        list.appendChild(row);
      });
    }
    zone.addEventListener('dragover', function (e) { e.preventDefault(); zone.classList.add('over'); });
    zone.addEventListener('dragleave', function () { zone.classList.remove('over'); });
    zone.addEventListener('drop', function (e) {
      e.preventDefault(); zone.classList.remove('over');
      input.files = e.dataTransfer.files; render();
    });
    input.addEventListener('change', render);
  });
})();

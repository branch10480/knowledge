/* Knowledge v2 クライアント側 JS。
   innerHTML は使わず textContent と DOM API で描画。タグ数は Map.get で取得。
   データは inline JSON ではなく data-* 属性と独立 tag-index を参照する。 */
(() => {
  const root = document.documentElement;
  const btn = document.getElementById('themeToggle');
  const label = document.getElementById('themeLabel');
  const SEQ = ['auto', 'light', 'dark'];
  const NAMES = { auto: '自動', light: 'ライト', dark: 'ダーク' };
  let mode = localStorage.getItem('tds-theme') || 'auto';
  const browserTheme = () => window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  const applyResolved = theme => {
    root.dataset.theme = theme === 'dark' ? 'dark' : 'light';
    root.dataset.themeMode = 'auto';
  };
  function resolveSystemTheme() { applyResolved(browserTheme()); }
  function apply() {
    if (mode === 'auto') resolveSystemTheme();
    else { root.dataset.theme = mode; root.dataset.themeMode = mode; }
    label.textContent = NAMES[mode];
  }
  btn && btn.addEventListener('click', () => {
    mode = SEQ[(SEQ.indexOf(mode) + 1) % SEQ.length];
    localStorage.setItem('tds-theme', mode);
    apply();
  });
  apply();

  // --- index ページの検索・タグ絞り込み ---
  const S = document.getElementById('search-input');
  const TC = document.getElementById('entries-container');
  const TF = document.getElementById('tag-filters');
  const AL = document.getElementById('archive-links');
  const RC = document.getElementById('result-count');
  if (S && TC && TF && AL) {
    // データは script 型の JSON 要素から textContent で読み取る（実行しない）
    const dataEl = document.getElementById('knowledge-data');
    let entries = [];
    if (dataEl) {
      try { entries = JSON.parse(dataEl.textContent); } catch (_) { entries = []; }
    }
    // タグ集計（Map.get 使用）
    const counts = new Map();
    entries.forEach(e => (e.tags || []).forEach(t => counts.set(t, (counts.get(t) || 0) + 1)));
    const archives = {};
    entries.forEach(e => {
      const ym = (e.published_at || '').slice(0, 7);
      if (ym) archives[ym] = (archives[ym] || 0) + 1;
    });

    Object.keys(archives).sort().reverse().forEach(ym => {
      const a = document.createElement('a');
      a.className = 'archive-link';
      a.href = 'archive/' + ym + '.html';
      a.textContent = ym + ' (' + archives[ym] + ')';
      AL.appendChild(a);
    });

    [...counts.keys()].sort().forEach(tag => {
      const b = document.createElement('button');
      b.className = 'filter-btn';
      b.setAttribute('aria-pressed', 'false');
      b.textContent = tag + ' (' + counts.get(tag) + ')';
      b.addEventListener('click', () => {
        const pressed = b.getAttribute('aria-pressed') === 'true';
        b.setAttribute('aria-pressed', String(!pressed));
        render();
      });
      TF.appendChild(b);
    });

    function render() {
      const q = (S.value || '').toLowerCase().trim();
      const activeTags = [...TF.querySelectorAll('.filter-btn[aria-pressed="true"]')].map(b => {
        const m = b.textContent.match(/^(.+?)\s*\(\d+\)/);
        return m ? m[1] : '';
      }).filter(Boolean);
      const filtered = entries.filter(e => {
        const hay = (e.title + ' ' + e.summary + ' ' + (e.tags || []).join(' ')).toLowerCase();
        if (q && !hay.includes(q)) return false;
        if (activeTags.length && !activeTags.some(t => (e.tags || []).includes(t))) return false;
        return true;
      });
      TC.textContent = '';
      filtered.forEach(e => {
        const card = document.createElement('article');
        card.className = 'entry-card';
        const h = document.createElement('h2');
        h.className = 'entry-header';
        const a = document.createElement('a');
        a.href = 'entry/' + e.id + '.html';
        a.textContent = e.title;
        h.appendChild(a);
        card.appendChild(h);
        const meta = document.createElement('div');
        meta.className = 'entry-meta';
        const time = document.createElement('time');
        time.datetime = e.published_at;
        time.textContent = (e.published_at || '').slice(0, 10);
        meta.appendChild(time);
        (e.tags || []).forEach(t => {
          const pill = document.createElement('span');
          pill.className = 'tag-pill';
          pill.textContent = t;
          meta.appendChild(pill);
        });
        card.appendChild(meta);
        const body = document.createElement('div');
        body.className = 'entry-body';
        const p = document.createElement('p');
        p.textContent = e.summary;
        body.appendChild(p);
        card.appendChild(body);
        TC.appendChild(card);
      });
      if (RC) RC.textContent = filtered.length + ' / ' + entries.length + ' 件';
    }
    S.addEventListener('input', render);
    render();
  }
})();

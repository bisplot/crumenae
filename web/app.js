'use strict';
/* Crumenae UI. Plain JavaScript, no build step, no external requests.
   Text from the blockchain (token names, symbols) is untrusted: it is always
   inserted with textContent through el(), never as HTML. */
(() => {
  const $ = (sel) => document.querySelector(sel);
  const SVGNS = 'http://www.w3.org/2000/svg';
  const COLORS = ['--c1', '--c2', '--c3', '--c4', '--c5', '--c6', '--c7', '--c8'];
  const state = { portfolio: '', status: null, jobTimer: null, page: 'dashboard' };

  // ---------------------------------------------------------------- DOM helpers
  function build(node, attrs, children) {
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') node.setAttribute('class', v);
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else if (k === 'value') node.value = v;
      else if (k === 'checked' || k === 'selected' || k === 'disabled') node[k] = Boolean(v);
      else node.setAttribute(k, v === true ? '' : String(v));
    }
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      node.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }
  const el = (tag, attrs, ...children) => build(document.createElement(tag), attrs, children);
  // Replace a node's children. Unlike the DOM's replaceChildren(), it skips null/false and flattens
  // arrays (replaceChildren would print them as the text "null" or "[object HTMLOptionElement]").
  const fill = (node, ...children) => { node.replaceChildren(); return build(node, null, children); };
  const svg = (tag, attrs, ...children) => build(document.createElementNS(SVGNS, tag), attrs, children);
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // ---------------------------------------------------------------- API
  async function request(method, path, body) {
    const opts = { method, headers: {} };
    if (method !== 'GET') opts.headers['X-Crumenae'] = '1';
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    const isJson = (res.headers.get('content-type') || '').includes('json');
    const data = isJson ? await res.json() : await res.text();
    if (!res.ok) throw new Error((data && data.error) || res.statusText);
    return data;
  }
  const api = {
    get: (p) => request('GET', p),
    post: (p, b) => request('POST', p, b === undefined ? {} : b),
    put: (p, b) => request('PUT', p, b),
    del: (p) => request('DELETE', p),
  };
  const pf = (sep = '?') => (state.portfolio ? `${sep}portfolio=${encodeURIComponent(state.portfolio)}` : '');

  // ---------------------------------------------------------------- formatting
  const locale = navigator.language || 'en-GB';
  const eurFmt = new Intl.NumberFormat(locale, { style: 'currency', currency: 'EUR' });
  const eur = (v) => (v === null || v === undefined || v === '' ? '—' : eurFmt.format(Number(v)));
  function qty(v) {
    if (v === null || v === undefined || v === '') return '—';
    const n = Number(v);
    const a = Math.abs(n);
    const digits = a >= 1000 ? 2 : a >= 1 ? 4 : a >= 0.0001 ? 6 : 10;
    return new Intl.NumberFormat(locale, { maximumFractionDigits: digits }).format(n);
  }
  const pct = (v) => new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: 1 }).format(v);
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString(locale, { dateStyle: 'medium', timeStyle: 'short' }) : 'never');
  const isoLocal = (s) => (s ? s.replace('T', ' ').slice(0, 19) : '');
  const short = (s, a = 6, b = 4) => (s && s.length > a + b + 1 ? `${s.slice(0, a)}…${s.slice(-b)}` : s || '');
  const flagLevel = (f) => (f.startsWith('⛔') ? 'error' : f.startsWith('🟡') ? 'warn' : 'info');

  // ---------------------------------------------------------------- UI bits
  function toast(msg, isError) {
    const t = $('#toast');
    t.textContent = msg;
    t.className = 'toast' + (isError ? ' error' : '');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.add('hidden'), isError ? 6000 : 2600);
  }
  function openModal(...children) {
    const card = $('#modal-card');
    fill(card, ...children);
    $('#modal').classList.remove('hidden');
    const first = card.querySelector('input, select, textarea');
    if (first) first.focus();
  }
  function closeModal() { $('#modal').classList.add('hidden'); }
  $('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

  const card = (title, right, ...body) =>
    el('div', { class: 'card' }, title || right ? el('div', { class: 'card-head' }, title ? el('h2', { text: title }) : el('span'), right || null) : null, ...body);
  const field = (label, input, help) => el('label', { class: 'field' }, el('span', { text: label }), input, help ? el('span', { class: 'help', text: help }) : null);
  const badge = (text, kind) => el('span', { class: 'badge' + (kind ? ' ' + kind : ''), text });
  const flagList = (flags) => (flags && flags.length
    ? el('ul', { class: 'flag-list' }, flags.map((f) => el('li', { class: flagLevel(f) === 'error' ? 'neg' : flagLevel(f) === 'warn' ? '' : 'muted', text: f })))
    : null);
  const table = (head, rows, cls) => el('div', { class: 'table-wrap' },
    el('table', { class: cls || '' },
      el('thead', {}, el('tr', {}, head.map((h) => (h instanceof Node ? h : el('th', { class: h.startsWith('#') ? 'num' : '', text: h.replace(/^#/, '') }))))),
      el('tbody', {}, rows)));
  function sourceBadge(src, label) {
    const kind = { manual: 'ok', snapshot: 'ok', chain_state: 'ok', post_state: 'ok', reconstructed: '', exchange_daily: 'warn', missing: 'err' }[src];
    return badge(label || src, kind);
  }

  // ---------------------------------------------------------------- charts
  function donut(items) {
    const size = 180, r = 70, stroke = 26, c = size / 2;
    const total = items.reduce((s, i) => s + i.value, 0);
    const g = svg('svg', { viewBox: `0 0 ${size} ${size}`, role: 'img', 'aria-label': 'Allocation by asset' });
    g.appendChild(svg('circle', { cx: c, cy: c, r, fill: 'none', stroke: cssVar('--surface-2'), 'stroke-width': stroke }));
    let angle = -Math.PI / 2;
    if (total > 0) {
      items.forEach((it) => {
        const frac = it.value / total;
        if (frac <= 0) return;
        const a2 = angle + frac * Math.PI * 2 - (frac < 1 ? 0.012 : 0);
        const large = a2 - angle > Math.PI ? 1 : 0;
        const p = (a) => `${c + r * Math.cos(a)} ${c + r * Math.sin(a)}`;
        const path = frac >= 0.9999
          ? svg('circle', { cx: c, cy: c, r, fill: 'none', stroke: it.color, 'stroke-width': stroke })
          : svg('path', { d: `M ${p(angle)} A ${r} ${r} 0 ${large} 1 ${p(a2)}`, fill: 'none', stroke: it.color, 'stroke-width': stroke },
            svg('title', {}, `${it.label}: ${pct(frac)}`));
        g.appendChild(path);
        angle += frac * Math.PI * 2;
      });
    }
    g.appendChild(svg('text', { x: c, y: c - 4, 'text-anchor': 'middle', class: 'value-label', style: 'font-size:13px' }, `${items.length}`));
    g.appendChild(svg('text', { x: c, y: c + 13, 'text-anchor': 'middle' }, items.length === 1 ? 'asset' : 'assets'));
    return el('div', { class: 'chart', style: 'width:180px' }, g);
  }

  function bars(series) {
    const W = 560, H = 230, padL = 8, padR = 8, padT = 26, padB = 26;
    const vals = series.map((s) => Number(s.value) || 0);
    const max = Math.max(...vals, 1);
    const n = series.length;
    const slot = (W - padL - padR) / Math.max(n, 1);
    const bw = Math.min(64, slot * 0.6);
    const g = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Value at each fiscal-year end' });
    g.appendChild(svg('line', { x1: padL, x2: W - padR, y1: H - padB, y2: H - padB, stroke: cssVar('--border') }));
    series.forEach((s, i) => {
      const v = vals[i];
      const h = (v / max) * (H - padT - padB);
      const x = padL + slot * i + (slot - bw) / 2;
      const y = H - padB - h;
      const complete = s.complete !== false;
      g.appendChild(svg('rect', { x, y, width: bw, height: Math.max(h, 1), rx: 4, fill: cssVar('--c1'), opacity: complete ? 1 : 0.45 },
        svg('title', {}, `${s.year}: ${eur(s.value)}${complete ? '' : ' (incomplete)'}`)));
      g.appendChild(svg('text', { x: x + bw / 2, y: y - 7, 'text-anchor': 'middle', class: 'value-label' },
        compactEur(v) + (complete ? '' : ' ⚠')));
      g.appendChild(svg('text', { x: x + bw / 2, y: H - 8, 'text-anchor': 'middle' }, `31/12/${s.year}`));
    });
    return el('div', { class: 'chart' }, g);
  }
  const compactEur = (v) => new Intl.NumberFormat(locale, { style: 'currency', currency: 'EUR', notation: 'compact', maximumFractionDigits: 1 }).format(v);

  // ---------------------------------------------------------------- pages
  async function pageDashboard(root) {
    const d = await api.get('/api/dashboard' + pf());
    fillPortfolios(d.portfolios);
    if (!d.wallets.length && !d.lost_hidden) {
      fill(root, card(null, null, el('div', { class: 'empty' },
        el('h2', { text: 'No wallets yet' }),
        el('p', { text: 'Add your addresses and accounts, choose their network, then press Sync. Balances and transactions are read from public APIs and converted to EUR.' }),
        el('a', { class: 'btn primary', href: '#/wallets', text: 'Add a wallet' }),
        el('p', { class: 'small', text: 'Just looking around? Stop the app and run: python3 -m crumenae demo' }))));
      return;
    }
    const lastSeries = d.series.length ? d.series[d.series.length - 1] : null;
    const now = Number(d.total_now);
    const last = lastSeries ? Number(lastSeries.value) : null;
    const change = last ? (now - last) / last : null;
    const kpis = el('div', { class: 'grid kpis' },
      kpi('Total value now', eur(d.total_now), d.price_time ? `prices as of ${when(d.price_time)}` : 'no prices yet: run a sync'),
      kpi(d.last_year ? `At 31/12/${d.last_year} 23:59:59` : 'Last year-end', lastSeries ? eur(lastSeries.value) : '—',
        lastSeries ? (lastSeries.complete ? 'all wallets valued' : '⚠ incomplete: see Year-end') : 'not computed yet'),
      kpi('Change since year-end', change === null ? '—' : (change >= 0 ? '+' : '') + pct(change), last ? `${eur(now - last)}` : '', change === null ? '' : change >= 0 ? 'pos' : 'neg'),
      kpi('Wallets', String(d.wallets.length), `last sync ${when(d.last_sync)}` + (d.lost_hidden ? ` · ${d.lost_hidden} lost not shown` : '')));

    const items = d.holdings.filter((h) => Number(h.value) > 0);
    const top = items.slice(0, 7).map((h, i) => ({ label: h.symbol, value: Number(h.value), color: cssVar(COLORS[i]) }));
    const rest = items.slice(7).reduce((s, h) => s + Number(h.value), 0);
    if (rest > 0) top.push({ label: 'Other', value: rest, color: cssVar('--c8') });
    const allocation = card('Allocation', null, top.length ? el('div', { class: 'donut-wrap' }, donut(top),
      el('div', { class: 'legend' }, top.map((t) => el('div', { class: 'legend-row' },
        el('span', {}, el('span', { class: 'dot', style: `background:${t.color}` }), t.label),
        el('span', { class: 'num muted' }, pct(t.value / now)))))) : el('p', { class: 'muted', text: 'No priced holdings yet.' }));

    const colorOf = new Map(top.map((t) => [t.label, t.color]));
    const holdingsNote = el('span', { class: 'muted small' }, 'same coin on several chains is grouped',
      d.ghost_hidden ? el('span', {}, ' · ', el('a', { href: '#/assets?filter=ghost', text: `${d.ghost_hidden} ghost coin${d.ghost_hidden > 1 ? 's' : ''} hidden` })) : null,
      d.spam_hidden ? el('span', {}, ' · ', el('a', { href: '#/assets', text: `${d.spam_hidden} likely spam hidden` })) : null,
      d.lost_hidden ? el('span', {}, ' · ', el('a', { href: '#/assets?filter=lost', text: `${d.lost_hidden} lost wallet${d.lost_hidden > 1 ? 's' : ''} hidden` })) : null);
    const holdings = card('Holdings', holdingsNote,
      table(['Asset', '#Quantity', '#Price', '#Value', '#Share'], d.holdings.map((h) => el('tr', {},
        el('td', {}, el('span', { class: 'dot', style: `background:${colorOf.get(h.symbol) || cssVar('--c8')}` }), el('strong', { text: h.symbol }),
          ' ', el('span', { class: 'muted small', text: h.name !== h.symbol ? h.name : '' })),
        el('td', { class: 'num', text: qty(h.amount) }),
        el('td', { class: 'num', text: h.priced ? eur(h.price) : '—' }),
        el('td', { class: 'num' }, h.priced ? eur(h.value) : badge('no price', 'warn')),
        el('td', { class: 'num' }, el('div', { class: 'share' }, el('span', { text: pct(h.share) }),
          el('span', { class: 'share-bar' }, el('span', { style: `width:${Math.round(h.share * 100)}%` }))))))));

    const yearsCard = card('Value at each fiscal-year end', el('a', { href: '#/year-end', text: 'Details →' }),
      d.series.length ? bars(d.series) : el('p', { class: 'muted', text: 'Run a sync to compute year-end balances.' }));
    const walletsCard = card('Wallets', el('a', { href: '#/wallets', text: 'Manage →' }),
      table(['Wallet', 'Portfolio', '#Now', `#31/12/${d.last_year || ''}`, 'Status'], d.wallets.map((w) => el('tr', {},
        el('td', {}, el('div', { text: w.label }), el('div', { class: 'muted small', text: `${w.network_name}${w.address ? ' · ' + short(w.address) : ''}` })),
        el('td', { text: w.portfolio }),
        el('td', { class: 'num' }, eur(w.value_now), w.unpriced ? el('div', { class: 'small muted', text: `+${w.unpriced} unpriced` }) : null),
        el('td', { class: 'num' }, w.value_last_year_end === null ? '—' : eur(w.value_last_year_end),
          w.last_year_complete === false ? el('div', { class: 'small neg', text: 'incomplete' }) : null),
        el('td', {}, statusBadge(w))))));

    const flags = d.flags.length ? card('Needs attention', null, el('div', { class: 'flags' },
      d.flags.map((f) => el('div', { class: 'flag' + (f.level === 'error' ? ' error' : ''), text: f.text })))) : null;

    fill(root, kpis, flags || '', el('div', { class: 'grid two' }, allocation, holdings),
      el('div', { class: 'grid two-even' }, yearsCard, walletsCard));
  }

  function kpi(label, value, sub, cls) {
    return el('div', { class: 'card kpi' }, el('div', { class: 'label', text: label }),
      el('div', { class: 'value ' + (cls || ''), text: value }), el('div', { class: 'sub', text: sub || '' }));
  }

  async function pageYearEnd(root, params) {
    const st = state.status;
    const year = Number(params.year) || st.last_completed_year;
    const [detail, rep] = await Promise.all([api.get(`/api/years/${year}` + pf()), api.get(`/api/report/${year}` + pf())]);
    const years = [...new Set([...detail.years.filter((y) => y > detail.years[0]), st.last_completed_year])].sort();
    const tabs = el('div', { class: 'tabs' }, years.map((y) => el('a', { class: 'tab' + (y === year ? ' active' : ''), href: `#/year-end?year=${y}`, text: `TY${y}` })));
    const total = detail.wallets.reduce((s, w) => s + Number(w.value || 0), 0);
    const incomplete = detail.wallets.filter((w) => !w.complete).length;
    const head = card(null, null,
      el('div', { class: 'card-head' },
        el('div', {}, el('h2', { text: `Balances at ${isoLocal(detail.cutoff_local)}` }),
          el('div', { class: 'muted small', text: `${detail.timezone} · fiscal year ${year} · opening balances are the ${year - 1} year-end` })),
        el('div', { class: 'actions' }, tabs,
          el('button', { class: 'btn' + (st.yearend_stale ? ' attention' : ''), type: 'button', text: 'Recompute year-end', onclick: () => startJob('recompute') }))),
      st.yearend_stale ? el('div', { class: 'flag', style: 'margin-bottom:12px',
        text: `Out of date: ${st.yearend_stale}. Values below were computed ${st.yearend_computed_at ? 'on ' + when(st.yearend_computed_at) : 'earlier'}; press "Recompute year-end" to update them.` }) : null,
      el('div', { class: 'grid kpis' },
        kpi('Total at year-end', eur(total), incomplete ? `⚠ ${incomplete} wallet(s) incomplete` : 'all wallets valued'),
        kpi('Initial values (RW col. 7)', eur(rep.totals.initial_value_eur), 'start of year or first day held'),
        kpi('Final values (RW col. 8)', eur(rep.totals.final_value_eur), 'end of year or last day held'),
        kpi('IC estimate', eur(rep.totals.ic_eur), `${Number(rep.ic_rate) * 100}% × days held, where no bollo applies`)));

    const dl = (name, label) => el('a', { class: 'btn small', href: `/api/report/${year}/${name}` + pf(), download: '', text: label });
    const exp = (name) => el('a', { class: 'btn small highlight', href: `/api/exports/${name}` + pf(), download: '', text: name });
    const exportsCard = card('Exports', el('span', { class: 'muted small', text: `also saved automatically in ${st.exports_dir}` }),
      el('div', { class: 'actions' }, exp(`balances_${detail.cutoff_local.slice(0, 10)}.csv`), exp(`transactions_${year}.csv`), exp('wallets.csv')),
      el('p', { class: 'help', text: 'Balances: one row per wallet and asset at the cutoff. Transactions: one row per transaction of the year, valued in EUR at its own time. Rewritten after every sync and at each year-end.' }));
    const rwCard = card(`Report TY${year}`, el('div', { class: 'actions' },
      dl('rw_crypto.csv', 'rw_crypto.csv'), dl('year_end_balances.csv', 'Detailed balances'), dl('transactions.csv', 'Detailed movements'),
      dl('evidence.md', 'evidence.md'),
      el('button', { class: 'btn small primary', type: 'button', text: 'Write report folder', onclick: async () => {
        try { const r = await api.post(`/api/report/${year}/write` + pf()); toast('Written to ' + r.folder); } catch (e) { toast(e.message, true); }
      } })),
    table(['Slot', 'Wallet', '#Days', '#Initial €', '#Final €', '#IC €', 'Notes'],
      rep.wallets.filter((w) => w.held).sort((a, b) => (a.rw_slot ?? 999) - (b.rw_slot ?? 999)).map((w) => el('tr', {},
        el('td', { text: w.rw_slot ?? '—' }),
        el('td', {}, el('div', { text: w.label }), el('div', { class: 'muted small', text: w.network + (w.bollo_applied ? ' · bollo applied' : '') })),
        el('td', { class: 'num', text: w.days_held ?? '?' }),
        el('td', { class: 'num', text: eur(w.initial_value_eur) }),
        el('td', { class: 'num', text: eur(w.final_value_eur) }),
        el('td', { class: 'num', text: w.ic_eur === null ? '—' : eur(w.ic_eur) }),
        el('td', { class: 'wrap small' }, flagList(w.notes) || el('span', { class: 'muted', text: 'OK' }))))));

    const walletCards = detail.wallets.map((w) => {
      const canManual = true;
      return el('div', { class: 'card wallet-card' },
        el('div', { class: 'wallet-head' },
          el('div', {}, el('h3', {}, w.wallet.label, w.wallet.lost ? [' ', badge('lost', 'lost')] : null),
            el('div', { class: 'muted small' }, `${w.wallet.network_name}${w.wallet.address ? ' · ' + short(w.wallet.address, 8, 6) : ''} · `, sourceBadge(w.source, w.source_label)),
            flagList(w.flags)),
          el('div', { style: 'text-align:right' }, el('div', { class: 'wallet-value', text: eur(w.value) }),
            !w.complete ? el('div', { class: 'small neg', text: 'incomplete' }) : null,
            canManual ? el('button', { class: 'btn small', type: 'button', style: 'margin-top:6px', text: w.source === 'manual' ? 'Edit manual entry' : 'Enter manually',
              onclick: () => manualModal(w, year) }) : null)),
        w.assets.length ? el('div', { style: 'margin-top:10px' }, table(['Asset', '#Quantity', '#Price €', '#Value €', 'Price source'],
          w.assets.map((a) => el('tr', {},
            el('td', {}, el('strong', { text: a.symbol || a.asset_key }), flagList(a.flags)),
            el('td', { class: 'num', text: qty(a.amount) }),
            el('td', { class: 'num', text: a.price_eur === null ? '—' : eur(a.price_eur) }),
            el('td', { class: 'num', text: eur(a.value_eur) }),
            el('td', { class: 'small muted wrap' }, a.price_source || '—', a.price_ts ? el('div', { text: new Date(a.price_ts * 1000).toISOString().replace('.000', '') }) : null)))))
          : el('p', { class: 'muted small', text: 'No balance at this cutoff.' }));
    });
    fill(root, head, exportsCard, rwCard, ...walletCards);
  }

  function manualModal(w, year) {
    const rows = el('tbody');
    const addRow = (a) => {
      const sym = el('input', { type: 'text', value: a ? a.symbol || a.asset_key : '', placeholder: 'BTC', disabled: Boolean(a && a.asset_key && w.wallet.network !== 'manual'), 'aria-label': 'Asset' });
      const amt = el('input', { type: 'text', inputmode: 'decimal', value: a ? a.amount : '', placeholder: '0.0', 'aria-label': 'Quantity' });
      const tr = el('tr', {}, el('td', {}, sym), el('td', {}, amt), el('td', {}, el('button', { class: 'btn ghost small', type: 'button', text: '✕', onclick: () => tr.remove() })));
      tr.dataset.key = a && a.asset_key && w.wallet.network !== 'manual' ? a.asset_key : '';
      tr._get = () => ({ asset_key: tr.dataset.key || undefined, symbol: tr.dataset.key ? undefined : sym.value.trim(), amount: amt.value.trim().replace(',', '.') });
      rows.appendChild(tr);
    };
    w.assets.forEach(addRow);
    if (!w.assets.length) addRow();
    const save = async (entries) => {
      try {
        await api.put(`/api/manual/${w.wallet.id}`, { year, entries });
        closeModal(); toast('Saved. Press "Recompute year-end" to update the values'); await refreshStatus(); route();
      } catch (e) { toast(e.message, true); }
    };
    openModal(
      el('h2', { text: `${w.wallet.label} — balances at 31/12/${year}` }),
      el('p', { class: 'help', text: 'A manual entry overrides every other source for this cutoff. Use tickers (BTC, ETH, EUR…) for exchange and manual accounts.' }),
      el('table', {}, el('thead', {}, el('tr', {}, el('th', { text: 'Asset' }), el('th', { text: 'Quantity' }), el('th'))), rows),
      el('div', { class: 'actions' },
        el('button', { class: 'btn', type: 'button', text: '+ Add asset', onclick: () => addRow() }),
        el('span', { style: 'flex:1' }),
        w.source === 'manual' ? el('button', { class: 'btn danger', type: 'button', text: 'Remove manual entry', onclick: () => save([]) }) : null,
        el('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: closeModal }),
        el('button', { class: 'btn primary', type: 'button', text: 'Save', onclick: () => save([...rows.children].map((tr) => tr._get()).filter((e) => (e.asset_key || e.symbol) && e.amount !== '')) })));
  }

  async function pageTransactions(root, params) {
    const [wallets, years] = await Promise.all([api.get('/api/wallets'), api.get(`/api/years/${state.status.last_completed_year}`)]);
    const yearOpts = [...new Set([...years.years, state.status.last_completed_year, state.status.last_completed_year + 1])].sort().reverse();
    const f = { wallet: params.wallet || '', year: params.year || '', category: params.category || '', q: params.q || '' };
    const anyLost = wallets.some((w) => w.lost);
    const wSel = el('select', {}, el('option', { value: '', text: anyLost ? 'All wallets except lost' : 'All wallets' }),
      anyLost ? el('option', { value: 'lost', text: 'Lost wallets', selected: f.wallet === 'lost' }) : null,
      wallets.map((w) => el('option', { value: w.id, text: w.lost ? `${w.label} (lost)` : w.label, selected: String(w.id) === f.wallet })));
    const ySel = el('select', {}, el('option', { value: '', text: 'All years' }), yearOpts.map((y) => el('option', { value: y, text: `TY${y}`, selected: String(y) === f.year })));
    const cSel = el('select', {}, el('option', { value: '', text: 'All except spam' }), el('option', { value: 'all', text: 'All, spam included', selected: f.category === 'all' }));
    const qIn = el('input', { type: 'search', placeholder: 'Hash, address, asset, note', value: f.q });
    const body = el('tbody');
    const info = el('span', { class: 'muted small' });
    const more = el('button', { class: 'btn', type: 'button', text: 'Load more' });
    const exportLink = el('a', { class: 'btn small highlight', download: '' });
    const refreshExport = () => {
      const y = ySel.value || String(state.status.last_completed_year);
      exportLink.href = `/api/exports/transactions_${y}.csv` + pf();
      exportLink.textContent = `transactions_${y}.csv`;
    };
    ySel.addEventListener('change', refreshExport);
    refreshExport();
    let offset = 0;
    let categories = {};

    async function load(reset) {
      if (reset) { offset = 0; body.replaceChildren(); }
      const qs = new URLSearchParams({ wallet: wSel.value === 'lost' ? '' : wSel.value, lost: wSel.value === 'lost' ? '1' : '', year: ySel.value, category: cSel.options.length === 2 ? f.category : cSel.value, q: qIn.value, offset, limit: 200 });
      if (state.portfolio) qs.set('portfolio', state.portfolio);
      const data = await api.get('/api/transactions?' + qs.toString());
      categories = data.categories;
      if (cSel.options.length === 2) Object.keys(categories).forEach((c) => cSel.appendChild(el('option', { value: c, text: c, selected: c === f.category })));
      data.rows.forEach((m) => body.appendChild(txRow(m, categories)));
      offset += data.rows.length;
      info.textContent = `${offset} of ${data.total} movements` + (cSel.value === '' ? ' · spam hidden' : '');
      more.classList.toggle('hidden', offset >= data.total);
    }
    [wSel, ySel, cSel].forEach((s) => s.addEventListener('change', () => load(true)));
    let tq;
    qIn.addEventListener('input', () => { clearTimeout(tq); tq = setTimeout(() => load(true), 300); });
    more.addEventListener('click', () => load(false));
    fill(root, card(null, null,
      el('div', { class: 'actions', style: 'margin-bottom:12px' }, wSel, ySel, cSel, qIn, el('span', { style: 'flex:1' }), info, exportLink),
      el('div', { class: 'table-wrap' }, el('table', { class: 'compact' },
        el('thead', {}, el('tr', {}, ['Time (local)', 'Wallet', 'Asset', '#Amount', '#Value €', 'Category', 'Tx'].map((h) =>
          el('th', { class: h.startsWith('#') ? 'num' : '', text: h.replace('#', '') })))), body)),
      el('div', { class: 'actions', style: 'margin-top:12px' }, more)));
    await load(true);
  }

  function txRow(m, categories) {
    const sel = el('select', { class: 'mini', 'aria-label': 'Category', title: categories[m.category] || '' },
      Object.keys(categories).map((c) => el('option', { value: c, text: c + (m.category_source === 'user' && c === m.category ? ' ✎' : ''), selected: c === m.category })),
      el('option', { value: 'auto', text: '↺ automatic' }));
    sel.addEventListener('change', async () => {
      try { await api.put(`/api/transactions/${m.id}`, { category: sel.value }); toast('Category saved'); if (sel.value === 'auto') route(); }
      catch (e) { toast(e.message, true); }
    });
    const amount = Number(m.amount);
    // Compact row: one line per movement; details (seconds, network, counterparty, full hash) in tooltips.
    const time = isoLocal(m.time_local);
    const txTitle = [m.tx_hash, m.counterparty ? `counterparty: ${m.counterparty}` : ''].filter(Boolean).join('\n');
    return el('tr', { class: m.failed ? 'muted' : '' },
      el('td', { class: 'mono', text: time.slice(0, 16), title: time }),
      el('td', {}, el('span', { class: 'clip', text: m.wallet_label, title: `${m.wallet_label} · ${m.network}` })),
      el('td', {}, el('strong', { text: m.symbol || m.asset_key }), m.kind === 'fee' ? el('span', { class: 'muted', text: ' fee' }) : null,
        m.failed ? badge('failed', 'err') : null),
      el('td', { class: 'num ' + (amount >= 0 ? 'pos' : 'neg'), text: (amount > 0 ? '+' : '') + qty(m.amount) }),
      el('td', { class: 'num', text: eur(m.value_eur), title: m.price_source || '' }),
      el('td', {}, sel),
      el('td', { class: 'mono' }, m.tx_url
        ? el('a', { href: m.tx_url, target: '_blank', rel: 'noopener noreferrer', text: short(m.tx_hash, 6, 4), title: txTitle })
        : el('span', { class: 'muted', text: short(m.tx_hash, 6, 4), title: txTitle })));
  }

  // --- wallet check: live balance vs the balance rebuilt from the transactions -------------------
  function statusBadge(w) {
    if (w.last_error) return badge('error', 'err');
    if (!w.active) return badge('paused');
    if (!w.last_sync_at && w.network !== 'manual') return badge('not synced', 'warn');
    const c = w.check || {};
    if (c.status === 'diff') return badge('check', 'warn');
    if (c.status === 'partial') return badge('incomplete', 'warn');
    return badge('ok', 'ok');
  }

  function checkLine(w) {
    const c = w.check || {};
    if (c.status === 'ok') return el('div', { class: 'small pos', title: 'Each asset\'s live balance equals the balance rebuilt from the synced transactions.', text: '✓ matches the transactions' });
    if (c.status === 'diff') return el('a', { href: '#', class: 'small warn-text', text: `⚠ ${c.count} asset(s) differ from the transactions`, onclick: (e) => { e.preventDefault(); showCheck(w); } });
    if (c.status === 'partial') return el('a', { href: '#', class: 'small warn-text', text: '⚠ live balance incomplete', onclick: (e) => { e.preventDefault(); showCheck(w); } });
    return null;
  }

  function showCheck(w) {
    const c = w.check || {};
    const num = (v) => (v === null || v === undefined ? 'not read' : String(v));
    openModal(el('h2', { text: `Check · ${w.label}` }),
      el('p', { class: 'help', text: 'After each sync the live balance is compared with the balance rebuilt from the synced transactions. A difference means the history is incomplete or wrong (a transaction the provider did not return, a fee not recorded, a token without decimals…): a year-end value built from that history would be wrong too.' }),
      c.not_read && c.not_read.length ? el('div', { class: 'flags' }, c.not_read.map((t) => el('div', { class: 'flag', text: 'Not read: ' + t }))) : null,
      c.assets && c.assets.length ? table(['Asset', '#Live', '#Transactions', '#Difference'], c.assets.map((a) => el('tr', {},
        el('td', { text: a.symbol, title: a.asset_key }), el('td', { class: 'num', text: num(a.live) }),
        el('td', { class: 'num', text: num(a.history) }), el('td', { class: 'num', text: a.difference === null ? '—' : String(a.difference) })))) : null,
      c.count > (c.assets || []).length ? el('p', { class: 'muted small', text: `…and ${c.count - c.assets.length} more` }) : null,
      el('p', { class: 'muted small', text: `Checked ${when(c.at)}. Sync again after a few minutes: a transaction made during the sync can show as a difference once.` }),
      el('div', { class: 'actions' }, el('button', { class: 'btn', type: 'button', text: 'Close', onclick: closeModal })));
  }

  async function pageWallets(root) {
    const [nets, wallets, cfg] = await Promise.all([api.get('/api/networks'), api.get('/api/wallets'), api.get('/api/settings')]);
    const byId = Object.fromEntries(nets.map((n) => [n.id, n]));
    // Platforms you typed before (saved by the server when a wallet is saved): no built-in list.
    const platformMap = new Map();
    [...(cfg.settings.platforms || []), ...wallets.map((w) => w.platform)].forEach((p) => {
      const v = (p || '').trim();
      if (v && !platformMap.has(v.toLowerCase())) platformMap.set(v.toLowerCase(), v);
    });
    const platforms = [...platformMap.values()].sort((a, b) => a.localeCompare(b));
    const group = (kind, label) => el('optgroup', { label }, nets.filter((n) => n.kind === kind && !n.hidden).map((n) => el('option', { value: n.id, text: n.name })));
    const netSel = el('select', { required: true }, el('option', { value: '', text: 'Choose a network…' }), group('chain', 'Blockchains'), group('exchange', 'Exchanges'), group('manual', 'Other'));
    const addr = el('input', { type: 'text', placeholder: '0x… / ronin:… / T… / bc1… / G… / terra1… / Solana address', autocomplete: 'off', spellcheck: 'false' });
    const label = el('input', { type: 'text', placeholder: 'e.g. Main wallet, Savings' });
    const platform = el('input', { type: 'text', placeholder: platforms.length ? 'choose or type a new one' : 'e.g. MetaMask, Ledger, Binance', list: 'platform-list', autocomplete: 'off' });
    const portfolios = [...new Set(wallets.map((w) => w.portfolio))];
    const portfolio = el('input', { type: 'text', value: state.portfolio || 'Personal', list: 'portfolio-list' });
    const slot = el('input', { type: 'number', min: 1, placeholder: 'optional' });
    const bollo = el('input', { type: 'checkbox' });
    const evmBox = el('div', { class: 'chips' });
    const evmField = field('Also track this address on', evmBox, 'The same EVM address can hold funds on several chains: one wallet per chain is created.');
    const keyIn = el('input', { type: 'password', autocomplete: 'off' });
    const secretIn = el('input', { type: 'password', autocomplete: 'off' });
    const credField = el('div', { class: 'form-row' }, field('API key (read-only)', keyIn), field('API secret', secretIn,
      'Create the key with “Enable Reading” only. It is stored in your OS keychain (or data/secrets.json), never in the browser.'));
    const addrField = field('Address', addr);

    function refreshForm() {
      const n = byId[netSel.value];
      addrField.classList.toggle('hidden', !n || !n.needs_address);
      credField.classList.toggle('hidden', !n || !(n.credentials || []).length);
      const isEvm = n && n.family === 'evm';
      evmField.classList.toggle('hidden', !isEvm);
      if (isEvm) {
        fill(evmBox, ...nets.filter((x) => x.family === 'evm' && !x.hidden && x.id !== n.id).map((x) =>
          el('label', { class: 'chip' }, el('input', { type: 'checkbox', value: x.id }), x.name)));
      }
    }
    netSel.addEventListener('change', refreshForm);
    refreshForm();

    const form = el('form', { class: 'form', onsubmit: async (e) => {
      e.preventDefault();
      const n = byId[netSel.value];
      if (!n) { toast('Choose a network', true); return; }
      const extra = [...evmBox.querySelectorAll('input:checked')].map((i) => i.value);
      const body = { networks: [n.id, ...(n.family === 'evm' ? extra : [])], address: n.needs_address ? addr.value.trim() : '', label: label.value.trim(),
        portfolio: portfolio.value.trim(), platform: platform.value.trim(), rw_slot: slot.value, bollo_applied: bollo.checked };
      if ((n.credentials || []).length) { body.api_key = keyIn.value.trim(); body.api_secret = secretIn.value.trim(); }
      try {
        const created = await api.post('/api/wallets', body);
        toast(`${created.length} wallet(s) added`);
        route();
        askSync(created);
      } catch (err) { toast(err.message, true); }
    } },
    el('div', { class: 'form-row' }, field('Network', netSel), field('Label', label),
      field('Platform', platform, 'Where it is held: the wallet app, device or exchange. A new name is added to the list.')),
    addrField, evmField, credField,
    el('div', { class: 'form-row' },
      field('Portfolio / owner', portfolio, 'Group wallets per person or client.'),
      field('RW slot', slot, 'Fixed row order in the tax form (optional).'),
      el('label', { class: 'field' }, el('span', { text: 'Stamp duty' }), el('span', { class: 'check' }, bollo, 'Italian bollo already applied'),
        el('span', { class: 'help', text: 'e.g. an Italian-resident exchange: then no IC is estimated.' }))),
    el('datalist', { id: 'portfolio-list' }, portfolios.map((p) => el('option', { value: p }))),
    el('datalist', { id: 'platform-list' }, platforms.map((p) => el('option', { value: p }))),
    el('div', { class: 'actions' }, el('button', { class: 'btn primary', type: 'submit', text: 'Add wallet' })));

    const list = card(`Your wallets (${wallets.length})`, el('div', { class: 'actions' },
      el('a', { class: 'btn highlight', href: '/api/exports/wallets.csv' + pf(), download: '', text: 'wallets.csv' }),
      el('button', { class: 'btn', type: 'button', text: 'Sync all', onclick: () => startJob('sync') })),
      wallets.length ? table(['Wallet', 'Address', 'Portfolio', '#Slot', 'Last sync', ''], wallets.map((w) => el('tr', {},
        el('td', {}, el('div', {}, el('strong', { text: w.label }), w.lost ? badge('lost', 'lost') : null, !w.active ? badge('paused') : null), el('div', { class: 'muted small', text: `${w.network_name} · ${w.platform_display}` })),
        el('td', { class: 'mono small' }, w.address ? (w.address_url ? el('a', { href: w.address_url, target: '_blank', rel: 'noopener noreferrer', text: short(w.address, 8, 6), title: w.address }) : short(w.address, 8, 6)) : '—'),
        el('td', { text: w.portfolio }),
        el('td', { class: 'num', text: w.rw_slot ?? '—' }),
        el('td', { class: 'small' }, w.last_error ? el('div', {}, el('span', { class: 'neg', text: w.last_error }), el('div', { class: 'muted small', text: `last successful sync: ${when(w.last_sync_at)}` })) : w.network === 'manual' ? el('span', { class: 'muted', text: 'manual entries' }) : el('div', {}, when(w.last_sync_at)),
          !w.last_error ? checkLine(w) : null,
          w.last_warning ? el('div', { class: 'muted small', title: w.last_warning, text: '⚠ ' + (w.last_warning.length > 60 ? w.last_warning.slice(0, 57) + '…' : w.last_warning) }) : null),
        el('td', {}, el('div', { class: 'actions', style: 'justify-content:flex-end' },
          w.network !== 'manual' ? el('button', { class: 'btn small', type: 'button', text: 'Sync', title: w.active ? '' : 'Paused: syncs this wallet once, "Sync all" still skips it', onclick: () => startJob('sync', { wallet_ids: [w.id] }) }) : null,
          el('button', { class: 'btn small', type: 'button', text: 'Edit', onclick: () => editWallet(w, byId[w.network]) }),
          el('button', { class: 'btn small' + (w.lost ? '' : ' ghost'), type: 'button', text: w.lost ? 'Found' : 'Lost',
            title: w.lost ? 'You have access again: show it on the Dashboard' : 'Keys or access lost: hide it and its assets from the Dashboard, Assets and Transactions. Year-end values and the RW report still include it.',
            onclick: () => setLost(w, !w.lost) }),
          el('button', { class: 'btn small danger', type: 'button', text: 'Delete', onclick: async () => {
            if (!confirm(`Delete “${w.label}” and all its transactions from this computer?`)) return;
            try { await api.del(`/api/wallets/${w.id}`); toast('Deleted'); route(); } catch (e) { toast(e.message, true); }
          } }))))))
        : el('p', { class: 'muted', text: 'No wallets yet.' }));
    fill(root, card('Add a wallet or account', null, form), list);
  }

  // Lost wallet: keys or access lost. Off the Dashboard, Assets and Transactions; year-end values
  // and the RW report keep it (the coins still exist on the chain).
  async function setLost(w, lost) {
    if (lost && !confirm(`Mark “${w.label}” as lost?\n\nIts assets leave the Dashboard and move to Assets → Lost; its transactions are hidden unless you choose "Lost wallets". Year-end values and the RW report still include it. You can undo this with "Found".`)) return;
    try { await api.put(`/api/wallets/${w.id}`, { lost }); toast(lost ? 'Marked as lost' : 'Back in your wallets'); route(); }
    catch (e) { toast(e.message, true); }
  }

  // After adding wallets: sync now, or later with "Sync now".
  function askSync(created) {
    const ids = created.map((w) => w.id);
    openModal(el('h2', { text: created.length > 1 ? `Sync the ${created.length} new wallets now?` : 'Sync the new wallet now?' }),
      el('p', { class: 'help', text: 'Syncing reads the history and balances through the APIs (it uses calls within your limits). You can also do it later with "Sync now" or the Sync button of the wallet.' }),
      el('div', { class: 'actions', style: 'justify-content:flex-end' },
        el('button', { class: 'btn', type: 'button', text: 'No, later', onclick: closeModal }),
        el('button', { class: 'btn primary', type: 'button', text: 'Yes', onclick: () => { closeModal(); startJob('sync', { wallet_ids: ids }); } })));
  }

  function editWallet(w, net) {
    const label = el('input', { type: 'text', value: w.label });
    const portfolio = el('input', { type: 'text', value: w.portfolio });
    const platform = el('input', { type: 'text', value: w.platform || '', placeholder: w.platform_display, list: 'platform-list', autocomplete: 'off' });
    const slot = el('input', { type: 'number', min: 1, value: w.rw_slot ?? '' });
    const bollo = el('input', { type: 'checkbox', checked: w.bollo_applied });
    const active = el('input', { type: 'checkbox', checked: w.active });
    const notes = el('textarea', { rows: 2 }, w.notes || '');
    const creds = {};
    const credFields = ((net && net.credentials) || []).map((c) => {
      creds[c] = el('input', { type: 'password', autocomplete: 'off', placeholder: w.credentials && w.credentials[c] ? 'saved — leave empty to keep' : 'not set' });
      return field(c.replace('_', ' '), creds[c]);
    });
    openModal(el('h2', { text: 'Edit wallet' }),
      el('div', { class: 'muted small mono', text: w.address || w.network_name }),
      el('div', { class: 'form' }, el('div', { class: 'form-row' }, field('Label', label), field('Platform', platform)),
        el('div', { class: 'form-row' }, field('Portfolio / owner', portfolio), field('RW slot', slot)),
        el('label', { class: 'check' }, bollo, 'Italian bollo already applied (no IC)'),
        el('label', { class: 'check' }, active, 'Active (included in syncs)'),
        credFields.length ? el('div', { class: 'form-row' }, credFields) : null,
        field('Notes', notes)),
      el('div', { class: 'actions', style: 'justify-content:flex-end' },
        el('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: closeModal }),
        el('button', { class: 'btn primary', type: 'button', text: 'Save', onclick: async () => {
          const body = { label: label.value, portfolio: portfolio.value, platform: platform.value, rw_slot: slot.value, bollo_applied: bollo.checked, active: active.checked, notes: notes.value };
          Object.entries(creds).forEach(([k, i]) => { if (i.value.trim()) body[k] = i.value.trim(); });
          try { await api.put(`/api/wallets/${w.id}`, body); closeModal(); toast('Saved'); route(); } catch (e) { toast(e.message, true); }
        } })));
  }

  async function pageAssets(root, params) {
    const rows = await api.get('/api/assets');
    const filter = params.filter || 'attention';
    // Lost: held by (or linked only to) wallets marked lost on the Wallets page.
    const isLost = (a) => Number(a.held_lost) !== 0 || a.lost_only;
    const shown = rows.filter((a) => filter === 'all' ? true : filter === 'hidden' ? ['spam', 'ignored'].includes(a.status)
      : filter === 'ghost' ? a.status === 'ghost'
        : filter === 'lost' ? isLost(a)
          : !a.lost_only && (a.status === 'unmapped' || (!['spam', 'ignored', 'ghost'].includes(a.status) && Number(a.held) !== 0)));
    const nGhost = rows.filter((a) => a.status === 'ghost').length;
    const nLost = rows.filter(isLost).length;
    const tabs = el('div', { class: 'tabs' }, [['attention', 'Held & to map'], ['all', 'All'], ['ghost', `Ghost${nGhost ? ' (' + nGhost + ')' : ''}`], ['lost', `Lost${nLost ? ' (' + nLost + ')' : ''}`], ['hidden', 'Spam / ignored']].map(([k, l]) =>
      el('a', { class: 'tab' + (k === filter ? ' active' : ''), href: `#/assets?filter=${k}`, text: l })));
    const statusKind = { known: 'ok', mapped: 'ok', unmapped: 'warn', spam: 'err', ignored: '', ghost: 'ghost' };
    const loadAll = el('button', { class: 'btn primary', type: 'button', text: 'Load all', title: 'Read the tokens each wallet holds now (no transactions: few API calls)',
      onclick: () => startJob('assets') });
    const clearAll = el('button', { class: 'btn danger', type: 'button', text: 'Clear all', title: 'Empty the asset list',
      onclick: async () => {
        if (!confirm(`Clear all ${rows.length} assets?\n\nThe list is emptied, including the CoinGecko/Binance mappings and spam marks you set. Your wallets, transactions and year-end values stay. Press "Load all" to rebuild the list from your wallets.`)) return;
        try { const r = await api.post('/api/assets/clear'); toast(`${r.cleared} assets cleared. Press "Load all" to rebuild them from your wallets.`); await refreshStatus(); route(); }
        catch (e) { toast(e.message, true); }
      } });
    fill(root, card(null, el('div', { class: 'actions', style: 'width:100%' }, tabs, el('span', { style: 'flex:1' }), clearAll, loadAll),
      el('p', { class: 'help', text: 'Each asset needs a price source to be valued in EUR. Known tokens are mapped automatically; for the others enter the CoinGecko id (from the coin page URL) or the Binance ticker. Mark scam airdrops as spam (ignored). Mark real tokens that can no longer be traded (delisted, dead projects) as ghost: they stay in your balances and year-end values (valued at their live price if a market still exists, else €0) but are left off the Dashboard. Assets of wallets you marked lost (Wallets page) are in the Lost tab: off the Dashboard, still in year-end values. "Load all" reads the tokens each wallet holds now, without the transactions (a few API calls per wallet); "Clear all" empties it.' }),
      rows.length ? null : el('div', { class: 'empty' }, el('h2', { text: 'No assets' }), el('p', { text: 'Press "Load all" to read every asset held by or moved through your wallets.' })),
      table(['Asset', 'Network', 'Wallets', '#Held', 'Status', 'CoinGecko id', 'Binance ticker', ''], shown.map((a) => {
        const cg = el('input', { type: 'text', value: a.coingecko_id || '', placeholder: 'e.g. ethereum', style: 'width:150px' });
        const bn = el('input', { type: 'text', value: a.binance_symbol || '', placeholder: 'e.g. ETH', style: 'width:90px' });
        const upd = async (body) => {
          try {
            await api.put(`/api/assets/${encodeURIComponent(a.key)}`, body);
            toast('Saved. Press "Recompute year-end" to update year-end values');
            await refreshStatus(); route();
          } catch (e) { toast(e.message, true); }
        };
        return el('tr', {},
          el('td', {}, el('strong', { text: a.symbol }), el('div', { class: 'muted small', text: a.name || '' }),
            a.contract ? el('div', { class: 'mono small muted', text: short(a.contract, 8, 6), title: a.contract }) : null),
          el('td', { class: 'small', text: a.network }),
          el('td', { class: 'small wrap', style: 'max-width:220px' }, (a.wallets || []).length
            ? (a.wallets || []).map((w, i) => [i ? ', ' : '', el('span', { class: w.held && !w.lost ? '' : 'muted', text: w.lost ? `${w.label} (lost)` : w.label,
              title: (w.held ? 'Holds it now' : 'Moved it in the past (or a snapshot / manual entry), not held now') + (w.lost ? ' · wallet marked lost' : '') })])
            : el('span', { class: 'muted', text: '—' })),
          el('td', { class: 'num' }, filter === 'lost' ? qty(a.held_lost) : qty(a.held),
            filter !== 'lost' && Number(a.held_lost) !== 0 ? el('div', { class: 'small muted', text: `+${qty(a.held_lost)} lost` }) : null),
          el('td', {}, badge(a.status === 'ghost' ? '👻 ghost' : a.status, statusKind[a.status])),
          el('td', {}, a.fiat ? el('span', { class: 'muted', text: 'fiat (ECB)' }) : cg),
          el('td', {}, a.fiat ? null : bn),
          el('td', {}, el('div', { class: 'actions', style: 'justify-content:flex-end' },
            a.fiat ? null : el('button', { class: 'btn small primary', type: 'button', text: 'Save', onclick: () => upd({ coingecko_id: cg.value, binance_symbol: bn.value, status: a.status === 'spam' || a.status === 'ignored' ? 'unmapped' : a.status }) }),
            ['spam', 'ignored', 'ghost'].includes(a.status)
              ? el('button', { class: 'btn small', type: 'button', text: 'Restore', onclick: () => upd({ status: a.coingecko_id || a.binance_symbol ? 'mapped' : 'unmapped' }) })
              : [a.fiat ? null : el('button', { class: 'btn small', type: 'button', text: 'Ghost', title: 'A real token that can no longer be traded: kept in balances, €0 when there is no market', onclick: () => upd({ status: 'ghost' }) }),
                el('button', { class: 'btn small danger', type: 'button', text: 'Spam', onclick: () => upd({ status: 'spam' }) })])));
      }))));
  }

  async function pageSettings(root) {
    const [cfg, nets, limits, provs] = await Promise.all([api.get('/api/settings'), api.get('/api/networks'), api.get('/api/limits'), api.get('/api/providers')]);
    const s = cfg.settings;
    const saveSettings = async (body) => { try { await api.put('/api/settings', body); toast('Settings saved'); await refreshStatus(); route(); } catch (e) { toast(e.message, true); } };

    // Service rows (key + limits + Test), shared by "Blockchain APIs" and "Price sources"
    const secretInputs = {};
    const limitInputs = {};
    const fmtInt = (n) => new Intl.NumberFormat(locale).format(n);
    const byId = Object.fromEntries(limits.map((l) => [l.id, l]));
    const keyLinks = {
      etherscan_api_key: 'https://etherscan.io/myapikey', coingecko_api_key: 'https://www.coingecko.com/en/api/pricing',
      coinmarketcap_api_key: 'https://coinmarketcap.com/api/', trongrid_api_key: 'https://www.trongrid.io/',
      alchemy_api_key: 'https://dashboard.alchemy.com/signup', helius_api_key: 'https://dashboard.helius.dev/',
    };
    const cgPlan = el('select', { 'aria-label': 'CoinGecko plan' }, ['demo', 'pro'].map((p) => el('option', { value: p, text: p === 'demo' ? 'Demo plan (free)' : 'Pro plan (paid)', selected: s.coingecko_plan === p })));
    const keyCell = (name, extra, link) => {
      const info = cfg.secrets[name] || {};
      secretInputs[name] = el('input', { type: 'password', autocomplete: 'off', placeholder: info.source ? `saved (${info.source}) — type to replace` : 'paste key', style: 'width:100%;min-width:180px' });
      return el('div', { style: 'display:grid;gap:4px' },
        el('div', { class: 'actions' }, info.source ? badge(info.source === 'keychain' ? 'saved · keychain' : `saved · ${info.source}`, 'ok') : badge('not set'),
          (link || keyLinks[name]) ? el('a', { class: 'small', href: link || keyLinks[name], target: '_blank', rel: 'noopener noreferrer', text: 'get one →' }) : null,
          info.source && info.source !== 'env' ? el('button', { class: 'btn small ghost danger', type: 'button', text: 'Remove', onclick: async () => {
            try { await api.put('/api/secrets', { [name]: '' }); toast('Removed'); route(); } catch (e) { toast(e.message, true); } } }) : null),
        secretInputs[name], extra || null);
    };
    const serviceRow = ({ limitId, label, hint, secret, testId, keyNote, extra, link, actions }) => {
      const l = byId[limitId];
      const ps = el('input', { type: 'text', inputmode: 'decimal', value: l.rate ?? '', placeholder: 'no limit', style: 'width:80px', 'aria-label': 'Calls' });
      const unit = el('select', { 'aria-label': 'Per' },
        el('option', { value: 'second', text: 'seconds', selected: l.unit === 'second' }),
        el('option', { value: 'minute', text: 'minutes', selected: l.unit === 'minute' }));
      const pd = el('input', { type: 'text', inputmode: 'numeric', value: l.per_day ?? '', placeholder: 'no limit', style: 'width:100px', 'aria-label': 'Calls per day' });
      limitInputs[l.id] = { ps, unit, pd };
      const full = l.per_day && l.used_today >= l.per_day;
      const result = el('div', { class: 'small', style: 'max-width:260px;white-space:normal' });
      const testBtn = el('button', { class: 'btn small', type: 'button', text: 'Test', title: 'One call to check the service and your key', onclick: async () => {
        testBtn.disabled = true; testBtn.textContent = 'Testing…';
        try {
          const r = await api.post(`/api/test/${testId}`);
          result.className = 'small ' + (r.ok ? 'pos' : 'neg');
          result.textContent = `${r.ok ? '✓' : '✖'} ${r.message.replace(/\*\*/g, '')} (cost: ${r.cost})`;
        } catch (e) { result.className = 'small neg'; result.textContent = '✖ ' + e.message; }
        testBtn.disabled = false; testBtn.textContent = 'Test';
      } });
      return el('tr', {},
        el('td', {}, el('strong', { text: label || l.label }), el('div', { class: 'help', text: hint || l.hint })),
        el('td', { style: 'min-width:220px' }, secret ? keyCell(secret, extra, link) : el('span', { class: 'muted small', text: keyNote || 'no key needed' })),
        el('td', {}, el('div', { class: 'actions', style: 'flex-wrap:nowrap' }, ps, unit)),
        el('td', {}, pd),
        el('td', { class: 'num' }, full ? badge(`${fmtInt(l.used_today)} · cap reached`, 'err')
          : `${fmtInt(l.used_today)}${l.per_day ? ' / ' + fmtInt(l.per_day) : ''}`),
        el('td', {}, el('div', { class: 'actions', style: 'justify-content:flex-end;flex-wrap:nowrap' }, testBtn, actions || null,
          el('button', { class: 'btn small ghost', type: 'button', text: 'Default', title: `Limits: ${l.default_rate ?? '—'} per ${l.default_unit}, ${l.default_per_day ?? '—'}/day`,
            onclick: () => { ps.value = l.default_rate ?? ''; unit.value = l.default_unit; pd.value = l.default_per_day ?? ''; } })), result));
    };
    const serviceHead = ['Service', 'API key', 'Speed limit', 'Calls / day', '#Used today', ''];
    const saveKeysAndLimits = async (extraSettings) => {
      const secrets = {};
      Object.entries(secretInputs).forEach(([k, i]) => { if (i.value.trim()) secrets[k] = i.value.trim(); });
      const rate_limits = {};
      Object.entries(s.rate_limits || {}).forEach(([id, v]) => { if (byId[id]) rate_limits[id] = v; });   // services not shown keep their limits
      Object.entries(limitInputs).forEach(([id, i]) => { rate_limits[id] = { rate: i.ps.value.trim(), unit: i.unit.value, per_day: i.pd.value.trim() }; });
      try {
        if (Object.keys(secrets).length) await api.put('/api/secrets', secrets);
        await api.put('/api/settings', Object.assign({ rate_limits }, extraSettings || {}));
        toast('Saved'); await refreshStatus(); route();
      } catch (e) { toast(e.message, true); }
    };

    // API providers: the services that read blockchains. Built-in ones plus the ones you add.
    const provTypes = {
      etherscan: 'Etherscan-compatible explorer API', alchemy: 'Alchemy-compatible (JSON-RPC + Transfers API)',
      cosmos: 'Cosmos SDK LCD (REST)', esplora: 'Esplora (Bitcoin)', horizon: 'Stellar Horizon',
    };
    const provDefaults = {
      etherscan: 'https://api.etherscan.io/v2/api', alchemy: 'https://{network}.g.alchemy.com/v2',
      cosmos: 'https://terra-lcd.publicnode.com', esplora: 'https://mempool.space/api', horizon: 'https://horizon.stellar.org',
    };
    const provNeedsWallets = { solar: true };   // shown only once a wallet uses them
    const providerModal = (p) => {
      const name = el('input', { type: 'text', value: p ? p.name : '', placeholder: 'e.g. Lineascan, Blockscout Gnosis, My Alchemy' });
      const type = el('select', { disabled: Boolean(p && p.networks.length) }, Object.entries(provTypes).map(([k, l]) => el('option', { value: k, text: l, selected: p ? p.type === k : k === 'etherscan' })));
      const url = el('input', { type: 'text', value: p ? p.base_url : provDefaults.etherscan, style: 'width:100%' });
      const key = el('input', { type: 'password', autocomplete: 'off', placeholder: p && p.secret_source ? 'saved — type to replace' : 'paste the key (if the service needs one)' });
      const needsKey = el('input', { type: 'checkbox', checked: p ? p.needs_key : true });
      const chainid = el('input', { type: 'checkbox', checked: p ? p.chainid_param : true });
      const chainidRow = el('label', { class: 'check' }, chainid, 'Send the chain id with each call (Etherscan V2 multichain)');
      const link = el('input', { type: 'text', value: p ? p.link || '' : '', placeholder: 'https://… (where to get a key, optional)' });
      const typeHelp = el('div', { class: 'help' });
      const refresh = () => {
        chainidRow.classList.toggle('hidden', type.value !== 'etherscan');
        typeHelp.textContent = {
          etherscan: 'Full history: transactions, internal transfers, tokens. Etherscan V2, Blockscout (…/api), Routescan, Lineascan, Monadscan…',
          alchemy: 'The URL contains {network}: each network gives its Alchemy name (e.g. bnb-mainnet, monad-mainnet). Native and token transfers, every token held now.',
          cosmos: 'Any Cosmos SDK chain (Terra, Cosmos Hub, Osmosis…): its LCD/REST URL. Each network gives its address prefix and native denom.',
          esplora: 'Bitcoin: mempool.space, Blockstream or your own node.',
          horizon: 'Stellar: the public server or any Horizon.',
        }[type.value];
      };
      type.addEventListener('change', () => { if (!p) url.value = provDefaults[type.value]; refresh(); });
      refresh();
      openModal(el('h2', { text: p ? `Edit ${p.name}` : 'Add an API provider' }),
        el('p', { class: 'help', text: 'Any service that speaks one of these types can be added without code. Then point networks to it in the Networks table.' }),
        el('div', { class: 'form' },
          el('div', { class: 'form-row' }, field('Name', name), field('Type', type)), typeHelp,
          field('Base URL', url),
          el('div', { class: 'form-row' }, field('API key', key, 'Stored in your OS keychain or data/secrets.json, never in the browser.'), field('Where to get a key', link)),
          el('label', { class: 'check' }, needsKey, 'The service needs a key'), chainidRow),
        el('div', { class: 'actions', style: 'justify-content:flex-end' },
          el('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: closeModal }),
          el('button', { class: 'btn primary', type: 'button', text: p ? 'Save' : 'Add provider', onclick: async () => {
            const body = { name: name.value.trim(), type: type.value, base_url: url.value.trim(), needs_key: needsKey.checked, chainid_param: chainid.checked, link: link.value.trim() };
            if (key.value.trim()) body.key = key.value.trim();
            try {
              await (p ? api.put(`/api/providers/${p.id}`, body) : api.post('/api/providers', body));
              closeModal(); toast(p ? 'Provider saved' : 'Provider added: press Test, then point networks to it'); route();
            } catch (e) { toast(e.message, true); }
          } })));
    };
    const providerRows = provs.filter((p) => !p.builtin || !provNeedsWallets[p.id] || p.wallets).map((p) => serviceRow({
      limitId: p.id, label: p.name,
      hint: [p.hint, p.builtin ? '' : provTypes[p.type], p.networks.length ? 'Reads: ' + p.networks.map((n) => n.name).join(', ') : 'No network uses it yet'].filter(Boolean).join(' · '),
      secret: p.needs_key || p.secret_source || ['helius', 'trongrid'].includes(p.id) ? p.secret : null, testId: p.id,
      keyNote: 'no key needed', link: p.link,
      actions: p.builtin ? null : [
        el('button', { class: 'btn small ghost', type: 'button', text: 'Edit', onclick: () => providerModal(p) }),
        el('button', { class: 'btn small ghost danger', type: 'button', text: 'Remove', disabled: !p.removable,
          title: p.removable ? 'Remove this provider and its key' : 'Networks use it: point them to another provider first',
          onclick: async () => {
            if (!confirm(`Remove ${p.name} and its key?`)) return;
            try { await api.del(`/api/providers/${p.id}`); toast('Removed'); route(); } catch (e) { toast(e.message, true); }
          } })],
    }));
    const keysCard = card('API providers', el('div', { class: 'actions' },
      badge(`keys stored in: ${cfg.secrets_backend === 'keychain' ? 'OS keychain' : 'data/secrets.json'}`, cfg.secrets_backend === 'keychain' ? 'ok' : 'warn'),
      el('button', { class: 'btn small primary', type: 'button', text: '+ Add provider', onclick: () => providerModal(null) })),
      el('p', { class: 'help', text: 'The services that read your wallets. Keys stay on this computer; only public addresses are sent. Set your plan\'s speed limit and daily calls: the app stays within them and stops calling a service once its daily cap is used (counters reset at 00:00 UTC; empty = no limit). "Test" makes one cheap call and says what it cost. Add any other provider that speaks a supported type with "+ Add provider", then choose it for a network below.' }),
      table(serviceHead, providerRows),
      el('div', { class: 'actions', style: 'margin-top:12px;justify-content:flex-end' },
        el('button', { class: 'btn primary', type: 'button', text: 'Save keys and limits', onclick: () => saveKeysAndLimits() })));

    // Fiscal year
    const tz = el('input', { type: 'text', value: s.timezone });
    const fyEnd = el('input', { type: 'text', value: s.fiscal_year_end, placeholder: 'MM-DD' });
    const firstFy = el('input', { type: 'number', value: s.first_fiscal_year ?? '', placeholder: 'auto' });
    const fyCard = card('Fiscal year', null, el('div', { class: 'form' },
      el('div', { class: 'form-row' },
        field('Time zone', tz, 'IANA name, e.g. Europe/Rome. The cutoff is 23:59:59 local time.'),
        field('Fiscal year ends on', fyEnd, 'MM-DD, 12-31 for Italy.'),
        field('First fiscal year', firstFy, 'Empty = year of your oldest transaction.')),
      el('div', { class: 'actions' }, el('button', { class: 'btn primary', type: 'button', text: 'Save', onclick: () => saveSettings({ timezone: tz.value.trim(), fiscal_year_end: fyEnd.value.trim(), first_fiscal_year: firstFy.value }) }))));

    // Price sources: market-price APIs (keys, limits, Test) and how prices are chosen
    const providers = cfg.providers;
    const orderSelects = (key) => [0, 1, 2].map((i) => el('select', {}, el('option', { value: '', text: '—' }),
      providers.map((p) => el('option', { value: p, text: p, selected: (s[key] || [])[i] === p }))));
    const hist = orderSelects('historical_price_providers');
    const curr = orderSelects('current_price_providers');
    const fx = el('select', {}, el('option', { value: 'ecb', text: 'ECB reference rate of that day (official)', selected: s.usd_to_eur === 'ecb' }),
      el('option', { value: 'binance', text: 'Binance EUR/USDT at the same minute', selected: s.usd_to_eur === 'binance' }));
    const valueTx = el('input', { type: 'checkbox', checked: s.value_transactions });
    const priceRows = [
      serviceRow({ limitId: 'binance_data', label: 'Binance public prices', hint: 'No key. 1-minute history since listing: the main source.', testId: 'binance_data' }),
      serviceRow({ limitId: 'coingecko', label: 'CoinGecko', hint: 'Optional key. Demo: 30-100 calls/min, 10,000/month, last 365 days of history.', secret: 'coingecko_api_key', testId: 'coingecko', extra: cgPlan }),
      serviceRow({ limitId: 'coinmarketcap', label: 'CoinMarketCap', hint: 'Optional key. Free plan: current prices only. Test is free.', secret: 'coinmarketcap_api_key', testId: 'coinmarketcap' }),
      serviceRow({ limitId: 'ecb', label: 'ECB exchange rates', hint: 'No key. Official USD→EUR reference rate of each day.', testId: 'ecb' }),
    ];
    const pricesCard = card('Price sources', null,
      el('p', { class: 'help', text: 'Market-price APIs used to value everything in EUR at the right moment. They do not read your wallets.' }),
      table(serviceHead, priceRows),
      el('div', { class: 'form', style: 'margin-top:14px' },
        field('Historical prices — order of providers', el('div', { class: 'actions' }, hist), 'Binance public data needs no key and has 1-minute history; CoinGecko Demo only covers the last 365 days; CoinMarketCap free has no history.'),
        field('Current prices — order of providers', el('div', { class: 'actions' }, curr)),
        field('USD-quoted prices to EUR', fx),
        el('label', { class: 'check' }, valueTx, 'Value every transaction in EUR at its own time'),
        el('div', { class: 'actions', style: 'justify-content:flex-end' }, el('button', { class: 'btn primary', type: 'button', text: 'Save', onclick: () => saveKeysAndLimits({
          historical_price_providers: [...new Set(hist.map((x) => x.value).filter(Boolean))],
          current_price_providers: [...new Set(curr.map((x) => x.value).filter(Boolean))],
          usd_to_eur: fx.value, value_transactions: valueTx.checked, coingecko_plan: cgPlan.value }) }))));

    // Snapshots & tax
    const auto = el('input', { type: 'checkbox', checked: s.auto_snapshot });
    const tol = el('input', { type: 'number', min: 0, value: s.snapshot_tolerance_seconds });
    const ic = el('input', { type: 'text', value: s.ic_rate });
    const snapCard = card('Snapshots & RW', null, el('div', { class: 'form' },
      el('label', { class: 'check' }, auto, `While the app is running, snapshot all wallets just before and after each cutoff (next: ${isoLocal(state.status.next_cutoff_local)})`),
      el('div', { class: 'form-row' }, field('Snapshot tolerance (seconds)', tol, 'A snapshot this close to the cutoff counts as the year-end balance.'),
        field('IC rate', ic, '0.002 = 0.2% (imposta sul valore delle cripto-attività).')),
      el('div', { class: 'actions' },
        el('button', { class: 'btn', type: 'button', text: 'Take a snapshot now', onclick: () => startJob('snapshot') }),
        el('button', { class: 'btn primary', type: 'button', text: 'Save', onclick: () => saveSettings({ auto_snapshot: auto.checked, snapshot_tolerance_seconds: tol.value, ic_rate: ic.value.trim() }) }))));

    // Networks: which provider reads each blockchain; add, hide or remove networks.
    const familyOf = (n) => n.family || '';
    const provFamily = Object.fromEntries(provs.map((p) => [p.id, p.family]));
    const provType = Object.fromEntries(provs.map((p) => [p.id, p.type]));
    const netEdits = {};
    const netRows = nets.filter((n) => n.kind === 'chain').map((n) => {
      const choices = provs.filter((p) => p.family === familyOf(n));
      const provName = (id) => (provs.find((p) => p.id === id) || {}).name || id;
      // Automatic: the first of the network's providers whose key you saved (e.g. Etherscan if you
      // have its key, otherwise Alchemy). Choosing a provider below fixes it.
      const auto = n.auto_providers && n.auto_providers.length ? el('option', { value: '', selected: n.provider_auto,
        text: `Automatic: ${provName(n.provider)}`, title: `The first of ${n.auto_providers.map(provName).join(', ')} whose API key is saved` }) : null;
      const pSel = el('select', { 'aria-label': 'Provider', disabled: choices.length < 2 }, auto,
        choices.map((p) => el('option', { value: p.id, text: p.name, selected: !n.provider_auto && p.id === n.provider })));
      const alch = el('input', { type: 'text', value: n.alchemy_network || '', placeholder: 'e.g. eth-mainnet', style: 'width:140px', 'aria-label': 'Alchemy name' });
      const alchWrap = el('div', { class: 'small' }, alch);
      const showAlch = () => alchWrap.classList.toggle('hidden', provType[pSel.value] !== 'alchemy');
      showAlch();
      const visible = el('input', { type: 'checkbox', checked: !n.hidden, disabled: n.wallets > 0, title: n.wallets ? 'Wallets use this network' : 'Show in the Wallets list' });
      const rpcName = `rpc_url:${n.id}`;
      const rpc = familyOf(n) === 'evm' ? el('input', { type: 'password', autocomplete: 'off', placeholder: cfg.secrets[rpcName] && cfg.secrets[rpcName].source ? 'saved' : (n.rpc_url || 'optional'), style: 'width:100%' }) : null;
      if (rpc) rpc.dataset.secret = rpcName;
      const mark = () => { netEdits[n.id] = { provider: pSel.value, alchemy_network: alch.value.trim(), hidden: !visible.checked }; };
      pSel.addEventListener('change', () => { showAlch(); mark(); });
      alch.addEventListener('input', mark);
      visible.addEventListener('change', mark);
      return el('tr', { class: n.hidden ? 'muted' : '' },
        el('td', {}, el('strong', { text: n.name }), el('div', { class: 'muted small', text: `${n.id}${n.chain_id ? ' · chain ' + n.chain_id : ''}${n.builtin ? '' : ' · added by you'}${n.wallets ? ' · ' + n.wallets + ' wallet(s)' : ''}` })),
        el('td', {}, pSel, alchWrap),
        el('td', { style: 'min-width:180px' }, rpc || el('span', { class: 'muted small', text: '—' })),
        el('td', {}, el('label', { class: 'check' }, visible, 'shown')),
        el('td', {}, n.builtin ? null : el('div', { class: 'actions', style: 'justify-content:flex-end;flex-wrap:nowrap' },
          el('button', { class: 'btn small ghost', type: 'button', text: 'Edit', onclick: () => networkModal(n) }),
          el('button', { class: 'btn small ghost danger', type: 'button', text: 'Delete', disabled: n.wallets > 0, onclick: async () => {
            if (!confirm(`Delete the network ${n.name}?`)) return;
            try { await api.del(`/api/networks/${n.id}`); toast('Deleted'); route(); } catch (e) { toast(e.message, true); } } }))));
    });
    const netTable = table(['Network', 'Read by', 'Archive node URL', 'Wallets list', ''], netRows);
    const networkModal = (n) => {
      const custom = provs.filter((p) => provTypes[p.type]);
      const f = {};
      ['id', 'name', 'chain_id', 'alchemy_network', 'bech32_prefix', 'native_denom', 'native_symbol', 'native_name', 'native_decimals', 'native_coingecko', 'native_binance', 'tx_url', 'address_url', 'coingecko_platform', 'rpc_url']
        .forEach((k) => { f[k] = el('input', { type: 'text', style: 'width:100%' }); });
      const prov = el('select', {}, custom.map((p) => el('option', { value: p.id, text: `${p.name} (${provTypes[p.type]})` })));
      const l2 = el('input', { type: 'checkbox' });
      Object.assign(f.id, { placeholder: 'gnosis' }); f.name.placeholder = 'Gnosis'; f.chain_id.placeholder = '100';
      f.alchemy_network.placeholder = 'gnosis-mainnet'; f.native_symbol.placeholder = 'XDAI'; f.native_decimals.placeholder = 'auto';
      f.native_coingecko.placeholder = 'xdai'; f.native_binance.placeholder = 'optional';
      f.tx_url.placeholder = 'https://gnosisscan.io/tx/{tx}'; f.address_url.placeholder = 'https://gnosisscan.io/address/{address}';
      f.coingecko_platform.placeholder = 'xdai'; f.rpc_url.placeholder = 'optional, for the 31/12 check';
      f.bech32_prefix.placeholder = 'e.g. cosmos, osmo'; f.native_denom.placeholder = 'e.g. uatom, uosmo';
      if (n) {
        f.id.value = n.id; f.id.disabled = true; f.name.value = n.name || ''; f.chain_id.value = n.chain_id ?? '';
        f.alchemy_network.value = n.alchemy_network || ''; prov.value = n.provider || '';
        const nat = n.native || {};
        f.native_symbol.value = nat.symbol || ''; f.native_name.value = nat.name || ''; f.native_decimals.value = nat.decimals ?? '';
        f.native_coingecko.value = nat.coingecko || ''; f.native_binance.value = nat.binance || '';
        f.tx_url.value = n.tx_url || ''; f.address_url.value = n.address_url || ''; f.coingecko_platform.value = n.coingecko_platform || '';
        f.rpc_url.value = n.rpc_url || ''; l2.checked = Boolean(n.l2);
        f.bech32_prefix.value = n.bech32_prefix || ''; f.native_denom.value = n.native_denom || '';
      }
      const evmBox = el('div', { class: 'form' }, el('div', { class: 'form-row' }, field('Chain id', f.chain_id, 'From chainlist.org'), field('Alchemy name', f.alchemy_network, 'Only for an Alchemy provider')),
        el('div', { class: 'form-row' }, field('Archive node URL', f.rpc_url), field('CoinGecko platform id', f.coingecko_platform, 'To find token prices by contract')),
        el('label', { class: 'check' }, l2, 'Rollup (layer 2): flag fees that may miss the L1 data cost'));
      const cosmosBox = el('div', { class: 'form-row' }, field('Address prefix', f.bech32_prefix, 'What addresses start with, before the 1'), field('Native denom', f.native_denom, 'Smallest unit of the native coin'));
      const fam = () => {
        const t = provType[prov.value];
        evmBox.classList.toggle('hidden', !['etherscan', 'alchemy'].includes(t));
        cosmosBox.classList.toggle('hidden', t !== 'cosmos');
      };
      prov.addEventListener('change', fam); fam();
      openModal(el('h2', { text: n ? `Edit ${n.name}` : 'Add a network' }),
        el('p', { class: 'help', text: 'Any EVM chain (Etherscan- or Alchemy-compatible provider), a Cosmos SDK chain (LCD), a Bitcoin-like chain (Esplora) or Stellar (Horizon). Add the provider first if it is not in the list.' }),
        el('div', { class: 'form' },
          el('div', { class: 'form-row' }, field('Id', f.id, 'lower-case, e.g. gnosis'), field('Name', f.name), field('Read by', prov)),
          evmBox, cosmosBox,
          el('div', { class: 'form-row' }, field('Native coin symbol', f.native_symbol), field('Native coin name', f.native_name), field('Decimals', f.native_decimals)),
          el('div', { class: 'form-row' }, field('Native coin CoinGecko id', f.native_coingecko), field('Native coin Binance ticker', f.native_binance)),
          el('div', { class: 'form-row' }, field('Transaction link', f.tx_url, '{tx} is replaced by the hash'), field('Address link', f.address_url, '{address} is replaced by the address'))),
        el('div', { class: 'actions', style: 'justify-content:flex-end' },
          el('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: closeModal }),
          el('button', { class: 'btn primary', type: 'button', text: n ? 'Save' : 'Add network', onclick: async () => {
            const body = Object.fromEntries(Object.entries(f).map(([k, i]) => [k, i.value.trim()]));
            body.provider = prov.value; body.l2 = l2.checked;
            try {
              await (n ? api.put(`/api/networks/${n.id}`, body) : api.post('/api/networks', body));
              closeModal(); toast(n ? 'Network saved' : 'Network added: it is now in the Wallets list'); route();
            } catch (e) { toast(e.message, true); }
          } })));
    };
    const networksCard = card('Networks', el('button', { class: 'btn small primary', type: 'button', text: '+ Add network', onclick: () => networkModal(null) }),
      el('p', { class: 'help', text: 'The blockchains you can track, and which API provider reads each one. One free Alchemy key reads every EVM chain. "Automatic" uses Etherscan instead where its free plan works, if you saved an Etherscan key: Etherscan also lists internal transfers on every chain and failed transactions. Choose a provider to fix it, hide the networks you do not use from the Wallets list, or add a new chain with "+ Add network". Archive node URL (optional): lets the app read each balance directly at the 31/12 block to double-check the history. When the provider of a chain with wallets changes, the next sync continues from the last block read: nothing is counted twice.' }),
      netTable,
      el('div', { class: 'actions', style: 'margin-top:12px;justify-content:flex-end' }, el('button', { class: 'btn primary', type: 'button', text: 'Save networks', onclick: async () => {
        try {
          for (const [nid, body] of Object.entries(netEdits)) {
            const n = nets.find((x) => x.id === nid);
            if (provType[body.provider] !== 'alchemy') delete body.alchemy_network;
            if (n && !n.builtin) { await api.put(`/api/networks/${nid}`, body); continue; }
            await api.put(`/api/networks/${nid}`, body);
          }
          const sec = {};
          netTable.querySelectorAll('input[data-secret]').forEach((i) => { if (i.value.trim()) sec[i.dataset.secret] = i.value.trim(); });
          if (Object.keys(sec).length) await api.put('/api/secrets', sec);
          toast('Networks saved'); route();
        } catch (e) { toast(e.message, true); }
      } })));

    const about = card('About this installation', null, el('div', { class: 'small', style: 'display:grid;gap:4px' },
      el('div', {}, 'Version ', el('strong', { text: state.status.version }), ` · Python ${state.status.python}`),
      el('div', {}, 'Data folder: ', el('code', { text: state.status.data_dir })),
      el('div', {}, 'Exports folder: ', el('code', { text: state.status.exports_dir })),
      el('div', { class: 'muted', text: 'Back up the data folder to keep your ledger. To start over, use Reset below.' })));
    const resetCard = card('Reset', null,
      el('p', { class: 'help', text: 'Delete everything and start over as after the first installation: wallets, transactions, assets, prices, year-end values, settings, API keys, exports and reports. It cannot be undone: back up the data folder first if you may need it.' }),
      el('div', { class: 'actions' }, el('button', { class: 'btn danger', type: 'button', text: 'Reset everything…', onclick: openReset })));
    resetCard.classList.add('danger-zone');
    fill(root, keysCard, pricesCard, el('div', { class: 'grid two-even' }, fyCard, snapCard), networksCard, about, resetCard);
  }

  async function openReset() {
    let phrase;
    try { phrase = (await api.get('/api/reset/phrase')).phrase; } catch (e) { toast(e.message, true); return; }
    const input = el('input', { type: 'text', autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Confirmation sentence', style: 'width:100%' });
    const go = el('button', { class: 'btn danger', type: 'button', text: 'Delete everything', disabled: true });
    input.addEventListener('paste', (e) => e.preventDefault());   // type it: no copy & paste
    input.addEventListener('input', () => { go.disabled = input.value.trim() !== phrase; });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !go.disabled) go.click(); });
    go.addEventListener('click', async () => {
      go.disabled = true; go.textContent = 'Deleting…';
      try {
        const r = await api.post('/api/reset', { phrase: input.value.trim() });
        closeModal();
        state.portfolio = '';
        try { localStorage.removeItem('cl.portfolio'); } catch (_) { /* private mode */ }
        fillPortfolios([]);
        await refreshStatus();
        toast(r.env_secrets_left && r.env_secrets_left.length
          ? `Everything deleted. Still set by environment variables: ${r.env_secrets_left.join(', ')}`
          : 'Everything deleted. Crumenae is empty.', Boolean(r.env_secrets_left && r.env_secrets_left.length));
        if (location.hash === '#/dashboard') route(); else location.hash = '#/dashboard';
      } catch (e) { toast(e.message, true); go.textContent = 'Delete everything'; go.disabled = input.value.trim() !== phrase; }
    });
    openModal(
      el('h2', { style: 'color:var(--err)', text: '⚠ Reset Crumenae' }),
      el('p', { text: 'This permanently deletes from this computer:' }),
      el('ul', { class: 'small', style: 'margin:0;padding-left:20px;display:grid;gap:2px' },
        ['all wallets and accounts, with their transactions and categories', 'the asset list, price mappings and cached prices',
          'year-end balances, snapshots and manual entries', 'all settings, API keys and call counters',
          'the exports and reports folders'].map((t) => el('li', { text: t }))),
      el('p', { class: 'small', text: 'It cannot be undone. To confirm, type this sentence:' }),
      el('div', { class: 'phrase', text: phrase }),
      input,
      el('div', { class: 'actions', style: 'justify-content:flex-end' },
        el('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: closeModal }), go));
  }

  // ---------------------------------------------------------------- jobs
  const JOB_NAMES = { sync: 'Sync', recompute: 'Recompute year-end', assets: 'Load assets', snapshot: 'Snapshot', prices: 'Prices' };
  const fmtDuration = (s) => { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
  function showJob(job) {
    const box = $('#job');
    if (!job) return;
    box.classList.remove('hidden');
    const status = job.running ? 'running' : job.error ? 'failed' : 'done';
    $('#job-icon').className = 'job-icon ' + status;
    $('#job-icon').title = { running: 'Running', done: 'Done', failed: 'Failed' }[status];
    $('#job-bar').className = 'job-bar ' + status;
    $('#job-title').textContent = JOB_NAMES[job.kind] || job.kind;
    // The latest step under the title, so progress is visible even with the log scrolled.
    const last = [...job.log].reverse().find((l) => l.trim()) || (job.running ? 'Starting…' : '');
    $('#job-step').textContent = job.error && !job.running ? job.error : last;
    // Elapsed time: ticks every second while running (client clock), final duration when done.
    clearInterval(state.jobClock);
    if (job.running) {
      if (state.jobKey !== job.started_at) { state.jobKey = job.started_at; state.jobStart = Date.now(); }
      const tick = () => { $('#job-time').textContent = fmtDuration((Date.now() - state.jobStart) / 1000); };
      tick();
      state.jobClock = setInterval(tick, 1000);
    } else if (job.finished_at && job.started_at) {
      $('#job-time').textContent = fmtDuration(job.finished_at - job.started_at);
    }
    const pre = $('#job-log');
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30;
    pre.textContent = job.log.join('\n');
    if (atBottom) pre.scrollTop = pre.scrollHeight;   // follow the log unless you scrolled up to read
    $('#sync-btn').disabled = job.running;
    $('#recompute-btn').disabled = job.running;
    $('#sync-btn').textContent = job.running && job.kind === 'sync' ? 'Syncing…' : 'Sync now';
    $('#recompute-btn').textContent = job.running && job.kind === 'recompute' ? 'Recomputing…' : 'Recompute year-end';
    $('#sync-btn').classList.toggle('busy', job.running && job.kind === 'sync');
    $('#recompute-btn').classList.toggle('busy', job.running && job.kind === 'recompute');
  }
  async function startJob(kind, body) {
    try { showJob(await api.post(`/api/jobs/${kind}`, body || {})); pollJob(); } catch (e) { toast(e.message, true); }
  }
  function pollJob() {
    clearTimeout(state.jobTimer);
    state.jobTimer = setTimeout(async () => {
      try {
        const job = await api.get('/api/jobs/current');
        showJob(job);
        if (job && job.running) pollJob();
        else {
          const failed = job && job.errors ? `Done, but ${job.errors} wallet(s) failed: see the Wallets page` : 'Done';
          toast(job && job.error ? 'Job failed: ' + job.error : failed, Boolean(job && (job.error || job.errors)));
          await refreshStatus(); route();
        }
      } catch (e) { pollJob(); }
    }, 800);
  }
  $('#job-close').addEventListener('click', () => $('#job').classList.add('hidden'));
  $('#sync-btn').addEventListener('click', () => startJob('sync'));
  $('#recompute-btn').addEventListener('click', () => startJob('recompute'));

  // ---------------------------------------------------------------- router & init
  const PAGES = {
    dashboard: ['Dashboard', pageDashboard], 'year-end': ['Year-end', pageYearEnd], transactions: ['Transactions', pageTransactions],
    wallets: ['Wallets', pageWallets], assets: ['Assets', pageAssets], settings: ['Settings', pageSettings],
  };
  function fillPortfolios(list) {
    const sel = $('#portfolio');
    const current = state.portfolio;
    fill(sel, el('option', { value: '', text: 'All' }), (list || []).map((p) => el('option', { value: p, text: p, selected: p === current })));
  }
  $('#portfolio').addEventListener('change', (e) => {
    state.portfolio = e.target.value;
    try { localStorage.setItem('cl.portfolio', state.portfolio); } catch (_) { /* private mode */ }
    route();
  });

  async function route() {
    const hash = location.hash.replace(/^#\/?/, '') || 'dashboard';
    const [name, qs] = hash.split('?');
    const page = PAGES[name] ? name : 'dashboard';
    const params = Object.fromEntries(new URLSearchParams(qs || ''));
    state.page = page;
    document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.page === page));
    $('#page-title').textContent = PAGES[page][0];
    document.title = `${PAGES[page][0]} · Crumenae`;
    const root = $('#page');
    if (!root.children.length) fill(root, el('p', { class: 'muted', text: 'Loading…' }));
    try { await PAGES[page][1](root, params); }
    catch (e) { fill(root, card('Something went wrong', null, el('p', { class: 'neg', text: e.message }))); }
  }

  // ---------------------------------------------------------------- theme (dark-first, see docs/BRAND.md)
  function applyTheme(t) {
    if (t === 'light') document.documentElement.setAttribute('data-theme', 'light');
    else document.documentElement.removeAttribute('data-theme');
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', cssVar('--bg'));
  }
  const getTheme = () => { try { return localStorage.getItem('cl.theme') || 'dark'; } catch (_) { return 'dark'; } };
  applyTheme(getTheme());
  function themeToggle() {
    const t = getTheme();
    return el('button', { class: 'btn small ghost theme-toggle', type: 'button', text: t === 'light' ? '☾ Dark theme' : '☀ Light theme',
      onclick: () => {
        const next = getTheme() === 'light' ? 'dark' : 'light';
        try { localStorage.setItem('cl.theme', next); } catch (_) { /* private mode */ }
        applyTheme(next); refreshStatus(); route();
      } });
  }

  async function refreshStatus() {
    state.status = await api.get('/api/status');
    const st = state.status;
    fill($('#sidebar-foot'),
      st.demo ? badge('DEMO DATA', 'warn') : null,
      el('div', { text: `v${st.version} · ${st.timezone}` }),
      el('div', { text: `keys: ${st.secrets_backend === 'keychain' ? 'OS keychain' : 'local file'}` }),
      themeToggle());
    fill($('#banner'), st.demo ? el('div', { class: 'banner', text: 'Demo mode: fictional wallets and synthetic prices, no API is called. Start the app with “python3 -m crumenae” to use your own data.' }) : '');
    const rb = $('#recompute-btn');
    rb.classList.toggle('attention', Boolean(st.yearend_stale) || !st.yearend_computed_at);
    rb.title = st.yearend_stale ? `Out of date: ${st.yearend_stale}` : st.yearend_computed_at
      ? `Year-end values computed ${when(st.yearend_computed_at)}` : 'Year-end values not computed yet';
    return st;
  }

  (async () => {
    try { state.portfolio = localStorage.getItem('cl.portfolio') || ''; } catch (_) { state.portfolio = ''; }
    try {
      const st = await refreshStatus();
      try { fillPortfolios((await api.get('/api/dashboard')).portfolios); } catch (_) { /* first run */ }
      if (st.job && st.job.running) { showJob(st.job); pollJob(); }
    } catch (e) { toast('Cannot reach the Crumenae server: is it running?', true); }
    window.addEventListener('hashchange', route);
    route();
  })();
})();

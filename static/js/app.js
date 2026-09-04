// Alpine dashboard state. Wraps all REST calls and form handling.

function dashboard() {
  return {
    theme: localStorage.getItem('theme') || 'dark',
    active: 'holdings',
    tabs: [
      { id: 'holdings', label: '持仓' },
      { id: 'watchlist', label: '自选' },
      { id: 'transactions', label: '交易记录' },
    ],
    activeMarket: localStorage.getItem('activeMarket') || 'us',
    activeHoldingGroup: localStorage.getItem('activeHoldingGroup') || 'us',
    displayCcys: ['CNY', 'AUD'],
    displayCcy: localStorage.getItem('displayCcy') || 'CNY',
    lastUpdatedSyd: '',
    holdings: [],
    transactions: [],
    watchlist: [],
    summary: { by_currency: {}, totals: {}, total_cny: 0, fx: {} },
    pnlToday: null,
    pnlHistory: [],
    calMonth: null,  // {year, month0} — first-of-month being shown
    calExpanded: false,  // false = show only current week, true = full month
    busy: { prices: false },
    toast: { msg: '', kind: 'ok' },
    modal: null,
    form: { holding: {}, txn: {}, txnEdit: {}, watch: {} },
    buckets: [],   // [{currency, total_amount, notes, updated_at}, ...]
    // Watchlist search-and-fill — see docs/superpowers/specs/2026-05-15-watchlist-search-design.md
    search: { q: '', results: [], loading: false, degraded: [] },
    _searchTimer: null,
    // Holdings search-to-fill — see docs/superpowers/specs/2026-05-16-holdings-search.md
    holdingSearch: { q: '', results: [], loading: false, degraded: [] },
    _holdingSearchTimer: null,
    _authPromise: null,
    // Live wall-clocks in the masthead — SYD = Sydney, NYC = US Eastern
    clocks: { syd: '', nyc: '' },
    _clocksTimer: null,

    blankHolding() {
      // Default to US Eastern trading-day date — pnl_date / action_date /
      // trade_date all use the ET calendar so a single buy during a US
      // session belongs to the same trading day regardless of where the
      // user clicks save from.
      const d = new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
      return { id: null, symbol: '', name: '', market: 'cn_a', currency: 'CNY',
               quantity: 0, avg_cost: 0, last_price: 0, unit: 'share', notes: '',
               trade_date: d };
    },
    blankTxn() {
      // Same ET trading-day default as blankHolding — toISOString() is UTC,
      // which reads as *yesterday* during a Sydney morning.
      const d = new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
      return { trade_date: d, symbol: '', name: '', market: 'cn_a', side: 'buy',
               quantity: 0, price: 0, fee: 0, currency: 'CNY', notes: '' };
    },
    blankWatch() {
      return { symbol: '', name: '', market: 'cn_a', currency: 'CNY', notes: '' };
    },

    async bootstrap() {
      this.form.holding = this.blankHolding();
      this.form.txn = this.blankTxn();
      this.form.watch = this.blankWatch();
      this.$watch('displayCcy', v => localStorage.setItem('displayCcy', v));
      // Defensive: idempotent re-apply in case the inline <head> script
      // was bypassed (e.g., user pasted theme=dark into localStorage
      // after page load via devtools). classList.toggle ignores duplicates.
      if (this.theme === 'dark') document.documentElement.classList.add('dark');
      // Live clocks: tick once immediately, then every second.
      this._tickClocks();
      this._clocksTimer = setInterval(() => this._tickClocks(), 1000);
      await Promise.all([
        this.loadHoldings(),
        this.loadTxns(),
        this.loadWatchlist(),
        this.loadPnlToday(),
        this.loadPnlHistory(),
        this.loadBuckets(),
        this.loadPoolTransactions(),
      ]);
      // Default calendar to current Sydney month
      const syd = new Date(new Date().toLocaleString('en-US', { timeZone: 'Australia/Sydney' }));
      this.calMonth = { year: syd.getFullYear(), month0: syd.getMonth() };
      await this.loadSummary();
      // Kick a price refresh on page load (non-blocking).
      this.refreshOnLoad();
      // Poll holdings + summary every 30s so prices stay fresh without
      // a manual refresh. Server-side cron updates last_price in DB; we
      // just re-read it here.
      setInterval(() => { this.loadHoldings(); this.loadSummary(); }, 30000);
    },

    async refreshOnLoad() {
      if (this.busy.prices) return;
      this.busy.prices = true;
      try {
        await this.api('/api/prices/refresh', { method: 'POST' }, { promptAuth: false });
        await Promise.all([
          this.loadHoldings(),
          this.loadWatchlist(),
          this.loadSummary(),
        ]);
      } catch (e) {
        // Silent fail — the periodic 5-min launchd job will catch up.
        console.warn('refreshOnLoad failed:', e);
      } finally {
        this.busy.prices = false;
      }
    },

    async api(path, opts = {}, { promptAuth = true } = {}) {
      // The API is gated behind API_TOKEN on the server. The token lives in
      // localStorage (one prompt per browser) and is sent for reads and writes.
      // promptAuth=false suppresses the 401 prompt — used by the automatic
      // page-load refresh so an automated / tokenless browser never gets a
      // blocking dialog it didn't ask for.
      const doFetch = () => {
        const headers = { 'Content-Type': 'application/json' };
        const tok = localStorage.getItem('apiToken');
        if (tok) headers['Authorization'] = 'Bearer ' + tok;
        return fetch(path, { ...opts, headers });
      };
      const tokenAtFirstAttempt = localStorage.getItem('apiToken') || '';
      let res = await doFetch();
      if (res.status === 401 && promptAuth) {
        // Several dashboard requests start together. If another request has
        // already collected a new token, reuse it instead of prompting again.
        const currentToken = localStorage.getItem('apiToken') || '';
        if (currentToken && currentToken !== tokenAtFirstAttempt) {
          res = await doFetch();
        }
      }
      if (res.status === 401 && promptAuth) {
        localStorage.removeItem('apiToken');
        if (!this._authPromise) {
          this._authPromise = Promise.resolve().then(() => {
            const t = window.prompt('请输入访问密码（API token）:');
            if (!t || !t.trim()) return false;
            localStorage.setItem('apiToken', t.trim());
            return true;
          }).finally(() => { this._authPromise = null; });
        }
        if (await this._authPromise) {
          res = await doFetch();
        }
      }
      if (!res.ok) {
        const t = await res.text();
        throw new Error(`${res.status} ${t}`);
      }
      return res.json();
    },

    notify(msg, kind = 'ok') {
      this.toast = { msg, kind };
      setTimeout(() => { this.toast.msg = ''; }, 2500);
    },

    async loadHoldings() {
      this.holdings = await this.api('/api/holdings');
      // Track most-recent last_price_at so the header can show "updated at …"
      const ts = this.holdings
        .map(h => h.last_price_at).filter(Boolean).sort().pop();
      this.lastUpdatedSyd = ts ? this.fmtTime(ts) : '';
    },
    async loadTxns()     { this.transactions = await this.api('/api/transactions'); },
    async loadWatchlist(){ this.watchlist = await this.api('/api/watchlist'); },
    async loadSummary()  { this.summary = await this.api('/api/summary'); },
    async loadPnlToday() {
      try { this.pnlToday = await this.api('/api/pnl/today'); }
      catch { this.pnlToday = null; }
    },
    async loadPnlHistory() {
      try { this.pnlHistory = await this.api('/api/pnl/history?limit=365'); }
      catch { this.pnlHistory = []; }
    },
    async loadBuckets() {
      try { this.buckets = await this.api('/api/buckets'); }
      catch { this.buckets = []; }
    },

    // ---- Currency bucket helpers ----
    bucketFor(ccy) {
      return this.buckets.find(b => b.currency === ccy);
    },
    // Single RMB pool. Convert any non-CNY market value to CNY via fx_rates
    // (direct rate or cross via CNY), then divide by the user-set ¥ total.
    fxToCny(amount, from) {
      if (from === 'CNY') return amount;
      const fx = this.summary.fx || {};
      const rate = fx[`${from}CNY`];
      return rate ? amount * rate : null;
    },
    poolWeight(h) {
      // Single-position weight uses the principal as denominator (matches
      // the backend's iron-floor math in advisor.py: hedge_pct and
      // single-position caps both divide by buckets.total_amount). Keeping
      // the % stable against a fixed base makes the iron-floor rules
      // easier to reason about than chasing a moving total.
      const principal = this.poolPrincipal();
      if (!principal) return null;
      const cny = this.fxToCny(h.market_value || 0, h.currency);
      if (cny == null) return null;
      return cny / principal * 100;
    },
    // Sum of every holding's market value, converted to CNY.
    totalPositionCny() {
      let total = 0;
      for (const h of this.holdings) {
        const v = this.fxToCny(h.market_value || 0, h.currency);
        if (v != null) total += v;
      }
      return total;
    },
    // The 资本本金 — what Aiden put in. Static, only changes when he
    // explicitly deposits / withdraws via the pool transactions UI.
    poolPrincipal() {
      const p = this.bucketFor('CNY');
      return p && p.total_amount > 0 ? p.total_amount : 0;
    },
    // Floating P&L across all holdings, converted to CNY.
    totalPnlCny() {
      let total = 0;
      for (const h of this.holdings) {
        const pnl = (h.pnl != null) ? h.pnl : null;
        if (pnl == null) continue;
        const cny = this.fxToCny(pnl, h.currency);
        if (cny != null) total += cny;
      }
      return total;
    },
    // What Aiden calls 总仓 — principal + current floating P&L, so the
    // number on the dashboard moves with prices instead of being a flat
    // amount-deposited line.
    poolTotal() {
      return this.poolPrincipal() + this.totalPnlCny();
    },
    cashRemaining() {
      // Cash left = principal − cost basis of all holdings. Doesn't move
      // with prices; it's the dry powder still un-deployed.
      let costSum = 0;
      for (const h of this.holdings) {
        const c = this.fxToCny((h.quantity || 0) * (h.avg_cost || 0), h.currency);
        if (c != null) costSum += c;
      }
      return Math.max(0, this.poolPrincipal() - costSum);
    },
    positionPct() {
      // Position weight against the live total (principal + P&L), so
      // the % matches what 总仓 currently is.
      const t = this.poolTotal();
      return t ? this.totalPositionCny() / t * 100 : 0;
    },
    // Inline pool edit. `poolInput` is initialized FROM the loaded
    // Pool is now a transaction log. Total = SUM(pool_transactions).
    poolTransactions: [],
    poolTxModal: { open: false, amount: '', note: '', tx_date: '', kind: 'deposit' },
    async loadPoolTransactions() {
      try { this.poolTransactions = await this.api('/api/buckets/CNY/transactions'); }
      catch { this.poolTransactions = []; }
    },
    openPoolTxModal(kind = 'deposit') {
      this.poolTxModal = {
        open: true,
        amount: '',
        note: '',
        tx_date: new Date().toLocaleDateString('en-CA', { timeZone: 'Australia/Sydney' }),
        kind,
      };
    },
    async submitPoolTx() {
      const m = this.poolTxModal;
      const raw = parseFloat(m.amount);
      if (isNaN(raw) || raw < 0) {
        this.notify('请输入有效金额', 'err');
        return;
      }
      try {
        if (m.kind === 'set') {
          if (!confirm(`这会把本金设为 ¥${raw.toLocaleString()},历史交易会被替换为单条"总额重置"。继续?`)) return;
          await this.api('/api/buckets/CNY', {
            method: 'PUT',
            body: JSON.stringify({ total_amount: raw }),
          });
          this.notify(`本金已设为 ¥${raw.toLocaleString()}`);
        } else {
          if (raw <= 0) {
            this.notify('请输入大于 0 的金额', 'err');
            return;
          }
          const signed = m.kind === 'withdraw' ? -raw : raw;
          await this.api('/api/buckets/CNY/transactions', {
            method: 'POST',
            body: JSON.stringify({ amount: signed, note: m.note || null, tx_date: m.tx_date || null }),
          });
          this.notify(m.kind === 'deposit'
            ? `已加 ¥${raw.toLocaleString()} 入池`
            : `已减 ¥${raw.toLocaleString()} 出池`);
        }
        this.poolTxModal.open = false;
        await Promise.all([this.loadBuckets(), this.loadPoolTransactions()]);
      } catch (e) { this.notify(e.message, 'err'); }
    },
    async deletePoolTx(id) {
      if (!confirm('删除这条入金/出金记录?')) return;
      try {
        await this.api(`/api/buckets/CNY/transactions/${id}`, { method: 'DELETE' });
        await Promise.all([this.loadBuckets(), this.loadPoolTransactions()]);
        this.notify('已删除');
      } catch (e) { this.notify(e.message, 'err'); }
    },

    // ---- Refresh actions ----
    async refreshPrices() {
      this.busy.prices = true;
      try {
        const r = await this.api('/api/prices/refresh', { method: 'POST' });
        const ok = r.items.filter(i => i.status === 'ok').length;
        this.notify(`行情已更新 (${ok} 条)`);
        await this.loadHoldings();
        await this.loadWatchlist();
        await this.loadSummary();
      } catch (e) { this.notify(e.message, 'err'); }
      finally { this.busy.prices = false; }
    },

    // ---- Holdings form ----
    openHoldingForm() {
      this.form.holding = this.blankHolding();
      this.resetHoldingSearch();
      this.modal = 'holding';
    },
    editHolding(h)    { this.form.holding = { ...h }; this.modal = 'holding'; },
    async saveHolding() {
      try {
        const h = this.form.holding;
        const isNew = !h.id;
        if (h.id) {
          await this.api(`/api/holdings/${h.id}`, { method: 'PUT', body: JSON.stringify(h) });
        } else {
          await this.api('/api/holdings', { method: 'POST', body: JSON.stringify(h) });
        }
        this.modal = null;
        await this.loadHoldings();
        await this.loadSummary();
        // POST also writes a transactions row; refresh that tab so the
        // new buy shows up immediately without a page reload.
        if (isNew) {
          await this.loadTxns();
        }
        this.notify('已保存');
      } catch (e) { this.notify(e.message, 'err'); }
    },
    async deleteHolding(id) {
      if (!confirm('确定删除该持仓？')) return;
      await this.api(`/api/holdings/${id}`, { method: 'DELETE' });
      await this.loadHoldings();
      await this.loadSummary();
    },

    // ---- Transactions form ----
    openTxnForm() { this.form.txn = this.blankTxn(); this.modal = 'txn'; },
    onTxnSymbolChange() {
      // When the user picks an existing holding from the symbol datalist
      // (or types a symbol that happens to match one), pre-fill name and
      // currency so they don't have to retype it. Leave the field alone
      // if it's an unknown symbol — they can fill it manually.
      const sym = (this.form.txn.symbol || '').trim();
      if (!sym) return;
      const h = this.holdings.find(x => x.symbol === sym);
      if (!h) return;
      if (!this.form.txn.name) this.form.txn.name = h.name || '';
      this.form.txn.market = h.market || this.form.txn.market;
      this.form.txn.currency = h.currency || this.form.txn.currency;
      // Also pre-fill price with last_price if user hasn't typed one yet —
      // saves a step on quick records of "just executed a market order".
      if (!this.form.txn.price && h.last_price) {
        this.form.txn.price = h.last_price;
      }
    },
    async saveTxn() {
      try {
        const res = await this.api('/api/transactions', { method: 'POST', body: JSON.stringify(this.form.txn) });
        this.modal = null;
        // Transactions POST now also touches holdings (and creates one
        // if buy of a new symbol), so refresh both + summary.
        await Promise.all([this.loadTxns(), this.loadHoldings(), this.loadSummary()]);
        if (res && res.warnings && res.warnings.length) {
          this.notify('已添加(' + res.warnings.join(';') + ')', 'warn');
        } else {
          this.notify('已添加');
        }
      } catch (e) { this.notify(e.message, 'err'); }
    },
    async deleteTxn(id) {
      if (!confirm('确定删除该交易？')) return;
      const res = await this.api(`/api/transactions/${id}`, { method: 'DELETE' });
      await Promise.all([this.loadTxns(), this.loadHoldings(), this.loadSummary()]);
      if (res && res.warnings && res.warnings.length) {
        this.notify(res.warnings.join(';'), 'warn');
      }
    },
    // Metadata-only edit — the backend PUT whitelists trade_date / name /
    // market / notes. side / quantity / price stay immutable because their
    // holdings sync was applied at POST time; correcting those = delete +
    // re-post.
    openTxnEdit(t) {
      this.form.txnEdit = { id: t.id, trade_date: t.trade_date,
                            name: t.name || '', market: t.market || '',
                            notes: t.notes || '' };
      this.modal = 'txnEdit';
    },
    async saveTxnEdit() {
      try {
        const e = this.form.txnEdit;
        await this.api(`/api/transactions/${e.id}`, {
          method: 'PUT',
          body: JSON.stringify({ trade_date: e.trade_date, name: e.name,
                                 market: e.market || null, notes: e.notes }),
        });
        this.modal = null;
        await this.loadTxns();
        this.notify('已更新');
      } catch (err) { this.notify(err.message, 'err'); }
    },

    // ---- P&L calendar ----
    pnlByDate(dateStr) {
      return this.pnlHistory.find(p => p.pnl_date === dateStr) || null;
    },
    calendarCells() {
      // Return 6×7 grid of {date, inMonth, pnl} for the current calMonth.
      // First cell is the Monday of the week containing the 1st (CN-style week).
      // IMPORTANT: build the ISO date from local Y/M/D parts — do NOT use
      // toISOString() because that's UTC and shifts the day for non-UTC users.
      if (!this.calMonth) return [];
      const { year, month0 } = this.calMonth;
      const first = new Date(year, month0, 1);
      const dow = first.getDay(); // 0=Sun..6=Sat
      const offset = (dow + 6) % 7; // Mon=0
      const start = new Date(year, month0, 1 - offset);
      const pad = n => String(n).padStart(2, '0');
      const cells = [];
      for (let i = 0; i < 42; i++) {
        const d = new Date(start);
        d.setDate(start.getDate() + i);
        const iso = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
        cells.push({
          date: iso,
          day: d.getDate(),
          inMonth: d.getMonth() === month0,
          pnl: this.pnlByDate(iso),
        });
      }
      return cells;
    },
    calMonthLabel() {
      if (!this.calMonth) return '';
      return `${this.calMonth.year} 年 ${this.calMonth.month0 + 1} 月`;
    },
    currentWeekCells() {
      // 7 cells representing this Sydney week (Mon..Sun), each with its
      // own pnl row if available. Independent of calMonth navigation —
      // always anchored on today.
      const syd = new Date(new Date().toLocaleString('en-US', { timeZone: 'Australia/Sydney' }));
      const dow = syd.getDay();           // 0=Sun..6=Sat
      const offset = (dow + 6) % 7;       // Mon=0
      const monday = new Date(syd);
      monday.setDate(syd.getDate() - offset);
      const pad = n => String(n).padStart(2, '0');
      const cells = [];
      for (let i = 0; i < 7; i++) {
        const d = new Date(monday);
        d.setDate(monday.getDate() + i);
        const iso = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
        cells.push({
          date: iso,
          day: d.getDate(),
          isToday: d.toDateString() === syd.toDateString(),
          pnl: this.pnlByDate(iso),
        });
      }
      return cells;
    },
    calPrev() {
      if (!this.calMonth) return;
      const m = this.calMonth.month0 - 1;
      if (m < 0) this.calMonth = { year: this.calMonth.year - 1, month0: 11 };
      else this.calMonth = { ...this.calMonth, month0: m };
    },
    calNext() {
      if (!this.calMonth) return;
      const m = this.calMonth.month0 + 1;
      if (m > 11) this.calMonth = { year: this.calMonth.year + 1, month0: 0 };
      else this.calMonth = { ...this.calMonth, month0: m };
    },
    calMonthSummary() {
      // Sum of pnl rows whose date is in calMonth
      if (!this.calMonth) return { cny: 0, aud: 0, usd: 0, days: 0, wins: 0, losses: 0 };
      const prefix = `${this.calMonth.year}-${String(this.calMonth.month0 + 1).padStart(2, '0')}`;
      const rows = this.pnlHistory.filter(p => (p.pnl_date || '').startsWith(prefix));
      let cny = 0, aud = 0, usd = 0, wins = 0, losses = 0;
      for (const p of rows) {
        cny += p.pnl_cny || 0;
        aud += p.pnl_aud || 0;
        usd += p.pnl_usd || 0;
        const total = (p.pnl_cny || 0) + (p.pnl_aud || 0) + (p.pnl_usd || 0);
        if (total > 0) wins++;
        else if (total < 0) losses++;
      }
      return { cny, aud, usd, days: rows.length, wins, losses };
    },

    marketDefaultCurrency(market) {
      // Source of truth: what currency does this market trade in?
      return {
        cn_a: 'CNY', cn_fund: 'CNY', spot_gold: 'CNY',
        hk: 'HKD',
        us: 'USD',
        asx_pocket: 'AUD',
      }[market] || 'CNY';
    },
    onHoldingMarketChange() {
      // Auto-snap currency to match the chosen market so the user doesn't
      // see a US$ stock cost displayed under CNY assumptions.
      this.form.holding.currency = this.marketDefaultCurrency(this.form.holding.market);
    },
    // ---- Watchlist form ----
    openWatchForm() {
      this.form.watch = this.blankWatch();
      this.resetWatchSearch();
      this.modal = 'watch';
    },
    resetWatchSearch() {
      this.search = { q: '', results: [], loading: false, degraded: [] };
      if (this._searchTimer) { clearTimeout(this._searchTimer); this._searchTimer = null; }
    },
    runSearch() {
      // 250ms debounce; min 2 chars; hit /api/search and update state.
      if (this._searchTimer) clearTimeout(this._searchTimer);
      const q = (this.search.q || '').trim();
      if (q.length < 2) {
        this.search.results = [];
        this.search.loading = false;
        this.search.degraded = [];
        return;
      }
      this.search.loading = true;
      this._searchTimer = setTimeout(async () => {
        try {
          const r = await this.api('/api/search?q=' + encodeURIComponent(q));
          // Drop stale response if the user kept typing.
          if (q !== (this.search.q || '').trim()) return;
          this.search.results = r.results || [];
          this.search.degraded = r.degraded || [];
        } catch (e) {
          this.search.results = [];
          this.search.degraded = ['search'];
        } finally {
          this.search.loading = false;
        }
      }, 250);
    },
    pickResult(r) {
      this.form.watch.symbol   = r.symbol;
      this.form.watch.name     = r.name;
      this.form.watch.market   = r.market;
      this.form.watch.currency = r.currency;
      this.search.results = [];
      this.search.q = '';
    },

    // ---- Holdings search (mirror of watchlist search; targets form.holding) ----
    resetHoldingSearch() {
      this.holdingSearch = { q: '', results: [], loading: false, degraded: [] };
      if (this._holdingSearchTimer) {
        clearTimeout(this._holdingSearchTimer);
        this._holdingSearchTimer = null;
      }
    },
    runHoldingSearch() {
      if (this._holdingSearchTimer) clearTimeout(this._holdingSearchTimer);
      const q = (this.holdingSearch.q || '').trim();
      if (q.length < 2) {
        this.holdingSearch.results = [];
        this.holdingSearch.loading = false;
        this.holdingSearch.degraded = [];
        return;
      }
      this.holdingSearch.loading = true;
      this._holdingSearchTimer = setTimeout(async () => {
        try {
          const r = await this.api('/api/search?q=' + encodeURIComponent(q));
          if (q !== (this.holdingSearch.q || '').trim()) return;  // stale guard
          this.holdingSearch.results = r.results || [];
          this.holdingSearch.degraded = r.degraded || [];
        } catch (e) {
          this.holdingSearch.results = [];
          this.holdingSearch.degraded = ['search'];
        } finally {
          this.holdingSearch.loading = false;
        }
      }, 250);
    },
    pickHoldingResult(r) {
      this.form.holding.symbol   = r.symbol;
      this.form.holding.name     = r.name;
      this.form.holding.market   = r.market;
      this.form.holding.currency = r.currency;
      this.holdingSearch.results = [];
      this.holdingSearch.q = '';
    },

    // ---- Theme toggle (light <-> dark) ----
    toggleTheme() {
      this.theme = this.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.classList.toggle('dark', this.theme === 'dark');
      localStorage.setItem('theme', this.theme);
    },
    _tickClocks() {
      const fmt = (tz) => new Date().toLocaleString('en-GB', {
        timeZone: tz, hour12: false,
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      });
      this.clocks.syd = fmt('Australia/Sydney');
      this.clocks.nyc = fmt('America/New_York');
    },
    async saveWatch() {
      try {
        await this.api('/api/watchlist', { method: 'POST', body: JSON.stringify(this.form.watch) });
        this.modal = null;
        await this.loadWatchlist();
        this.notify('已添加');
      } catch (e) { this.notify(e.message, 'err'); }
    },
    async deleteWatch(id) {
      if (!confirm('确定删除该自选？')) return;
      await this.api(`/api/watchlist/${id}`, { method: 'DELETE' });
      await this.loadWatchlist();
    },
    // Inline priority edit. Empty string = clear (NULL). On success the
    // backend's band-ASC sort means the row visually jumps into its new
    // bucket the moment loadWatchlist returns.
    async updateBand(w, value) {
      const band = value === '' ? null : parseInt(value, 10);
      try {
        await this.api(`/api/watchlist/${w.id}`, {
          method: 'PUT',
          body: JSON.stringify({ priority_band: band }),
        });
        await this.loadWatchlist();
      } catch (e) {
        this.notify('更新优先级失败: ' + e.message, 'err');
      }
    },
    // Group watchlist rows by market — drives the sub-tab strip inside the
    // 自选 tab. Order is fixed; unknown markets fall through at the end with
    // their raw code as label.
    watchlistGroups() {
      const labels = {
        us: '美股', cn_a: 'A 股', hk: '港股',
        asx_pocket: '澳股', cn_fund: '场外基金', spot_gold: '现货黄金',
      };
      const order = ['us', 'cn_a', 'hk', 'asx_pocket', 'cn_fund', 'spot_gold'];
      const buckets = {};
      for (const w of this.watchlist) {
        (buckets[w.market] = buckets[w.market] || []).push(w);
      }
      const groups = [];
      for (const m of order) {
        if (buckets[m]) groups.push({ market: m, label: labels[m], items: buckets[m] });
      }
      for (const m of Object.keys(buckets)) {
        if (!order.includes(m)) groups.push({ market: m, label: m.toUpperCase(), items: buckets[m] });
      }
      return groups;
    },
    // Currently-selected sub-tab; falls back to the first existing group so
    // we never show a blank panel after deleting the last row of a market.
    activeMarketGroup() {
      const groups = this.watchlistGroups();
      if (groups.length === 0) return null;
      return groups.find(g => g.market === this.activeMarket) || groups[0];
    },
    switchMarket(m) {
      this.activeMarket = m;
      localStorage.setItem('activeMarket', m);
    },
    // Holdings split into fixed buckets: 美股 / 港股 / 其他. Bucket key is
    // `region || market` — `region` is a display-only override so US-listed
    // Chinese ADRs (BABA, TCEHY, ...) show up under 港股 while their actual
    // `market='us'` keeps the Yahoo price-fetch path working.
    holdingGroups() {
      const us = [], hk = [], other = [];
      for (const h of this.holdings) {
        const r = h.region || h.market;
        if (r === 'us') us.push(h);
        else if (r === 'hk') hk.push(h);
        else other.push(h);
      }
      const groups = [];
      if (us.length) groups.push({ key: 'us', label: '美股', items: us });
      if (hk.length) groups.push({ key: 'hk', label: '港股', items: hk });
      if (other.length) groups.push({ key: 'other', label: 'A股', items: other });
      return groups;
    },
    activeHoldingItems() {
      const groups = this.holdingGroups();
      if (groups.length === 0) return [];
      return (groups.find(g => g.key === this.activeHoldingGroup) || groups[0]).items;
    },
    switchHoldingGroup(k) {
      this.activeHoldingGroup = k;
      localStorage.setItem('activeHoldingGroup', k);
    },
    // Priority-band visuals. 1 = L3 可执行 (best), 6 = 不追高 主升浪.
    // Colors lean green→red but band 5 is neutral (持有观察, not bad).
    bandStyle(b) {
      return {
        1: 'bg-emerald-100 text-emerald-700',
        2: 'bg-teal-100 text-teal-700',
        3: 'bg-sky-100 text-sky-700',
        4: 'bg-amber-100 text-amber-800',
        5: 'bg-slate-200 text-slate-600',
        6: 'bg-rose-100 text-rose-700',
      }[b] || 'bg-slate-100 text-slate-400';
    },
    bandLabel(b) {
      return {
        1: '可执行', 2: '观察仓', 3: '候选',
        4: '警示', 5: '持有', 6: '不追高',
      }[b] || '';
    },

    // ---- computed ----
    get totalDisplay() {
      const t = this.summary.totals || {};
      return t[this.displayCcy] || { market_value: 0, cost: 0, pnl: 0 };
    },
    // Per-currency P&L computed LIVE from each holding's day_pnl.
    // Updates every 30s along with the holdings poll — no need to wait
    // for the scheduled 17:30 compute job.
    pnlByCcy(ccy) {
      const rows = this.holdings.filter(h => h.currency === ccy && h.prev_close);
      if (rows.length === 0) return null;
      let pnl = 0, mv = 0, prev_mv = 0;
      for (const h of rows) {
        pnl += h.day_pnl || 0;
        mv += h.market_value || 0;
        prev_mv += (h.quantity || 0) * (h.prev_close || 0);
      }
      const pct = prev_mv ? (pnl / prev_mv * 100.0) : 0;
      const today = new Date().toLocaleDateString('en-CA', { timeZone: 'Australia/Sydney' });
      return { ccy, date: today, pnl, pct, mv, prev: prev_mv, net_invested: 0 };
    },

    // ---- formatters ----
    fmt(n) {
      if (n == null || isNaN(n)) return '—';
      return n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    },
    // Unit prices (成本/现价/单价): up to 4 decimals so low-priced A股 ETF
    // costs like 2.0851 don't get rounded into a misleading 2.09. Floor
    // stays at 2 so US stock prices still read 318.00, not 318.0000.
    fmt4(n) {
      if (n == null || isNaN(n)) return '—';
      return n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 4 });
    },
    ccySymbol(c) {
      return { CNY: '¥', AUD: 'A$', USD: 'US$', HKD: 'HK$' }[c] || (c + ' ');
    },
    // External chart URL for a watchlist row. Routes by market so each kind of
    // asset opens on a site that actually shows it well.
    externalChartUrl(w) {
      const code = (w.symbol || '').trim();
      if (!code) return null;
      const upper = code.toUpperCase();
      if (w.market === 'cn_a' || w.market === 'cn_fund') {
        let prefix = 'SZ';
        if (/^(5[1568]|60|68)/.test(code)) prefix = 'SH';
        else if (/^(4|8|92)/.test(code)) prefix = 'BJ';
        return `https://xueqiu.com/S/${prefix}${code}`;
      }
      if (w.market === 'hk') {
        return `https://xueqiu.com/S/${code.padStart(5, '0')}`;
      }
      if (w.market === 'us') {
        return `https://xueqiu.com/S/${upper}`;
      }
      if (w.market === 'asx_pocket') {
        return `https://finance.yahoo.com/quote/${upper}.AX`;
      }
      if (w.market === 'spot_gold') {
        return 'https://www.tradingview.com/symbols/XAUUSD/';
      }
      return `https://www.google.com/search?q=${encodeURIComponent(code + ' price chart')}`;
    },
    // Editorial masthead helpers
    todayMasthead() {
      const syd = new Date(new Date().toLocaleString('en-US', { timeZone: 'Australia/Sydney' }));
      return syd.toLocaleDateString('en-GB', {
        weekday: 'long', day: 'numeric', month: 'long', year: 'numeric'
      });
    },
    // Render any ISO/UTC timestamp string as Australia/Sydney local time.
    fmtTime(s) {
      if (!s) return '';
      let iso = String(s).trim();
      if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(iso)) iso = iso.replace(' ', 'T') + 'Z';
      const d = new Date(iso);
      if (isNaN(d)) return s;
      return d.toLocaleString('zh-CN', { timeZone: 'Australia/Sydney', hour12: false });
    },
    // Minutes since a timestamp; used to color "fresh" vs "stale" prices.
    ageMins(s) {
      if (!s) return null;
      let iso = String(s).trim();
      if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(iso)) iso = iso.replace(' ', 'T') + 'Z';
      const d = new Date(iso);
      if (isNaN(d)) return null;
      return (Date.now() - d.getTime()) / 60000;
    },
    freshness(s) {
      const m = this.ageMins(s);
      if (m == null) return { label: '未知', cls: 'text-slate-400' };
      if (m < 6)   return { label: '实时', cls: 'text-emerald-600' };
      if (m < 30)  return { label: `${Math.round(m)} 分钟前`, cls: 'text-slate-500' };
      if (m < 120) return { label: `${Math.round(m)} 分钟前`, cls: 'text-amber-600' };
      return { label: `${Math.round(m/60)} 小时前`, cls: 'text-rose-500' };
    },
    fxStr() {
      const fx = this.summary.fx || {};
      const want = ['AUDCNY', 'HKDCNY', 'USDCNY'];
      const present = want.filter(k => fx[k]);
      if (!present.length) return '未获取';
      return present.map(k => `${k}=${fx[k].toFixed(4)}`).join(' · ');
    },
  };
}

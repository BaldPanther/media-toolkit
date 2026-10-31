// Вкладка «Торренты»: поиск раздач и автоматика qBittorrent.
"use strict";

document.addEventListener("alpine:init", () => {
  Alpine.data("torrentsTab", () => ({
    view: "search",
    q: "",
    kind: "tv",
    showAll: false,
    choosing: false,      // снова показать карточки TMDb, когда тайтл уже выбран

    get t() { return this.$store.app.tabs.torrents || {}; },
    get s() { return this.t.search || {}; },
    get busy() { return this.$store.app.busy; },

    init() {
      try { this.view = localStorage.getItem("mt.torrents.view") || "search"; } catch (e) { /* без хранилища */ }
      this.$watch("s.query", (v) => { if (v && !this.q) this.q = v; });
      this.$watch("s.kind", (v) => { if (v) this.kind = v; });
    },
    setView(v) {
      this.view = v;
      try { localStorage.setItem("mt.torrents.view", v); } catch (e) { /* без хранилища */ }
    },
    asking() { return (this.t.items || []).filter((i) => i.status === "ask").length; },

    // ------------------------------------------------------------ поиск --
    async find() {
      if (!this.q.trim()) return;
      this.showAll = false;
      const res = await api.post("/api/torrents/search", { query: this.q, kind: this.kind });
      if (!res) return;
      await this.$store.app.waitJob(res.job);
      await this.$store.app.loadState();
    },
    async pick(i) {
      this.showAll = false;
      this.choosing = false;
      const res = await api.post("/api/torrents/search/select", { index: i });
      if (!res) return;
      await this.$store.app.waitJob(res.job);
      await this.$store.app.loadState();
    },
    pickOther() { this.choosing = !this.choosing; },
    async opt(changes) {
      await api.post("/api/torrents/search/options", changes);
      await this.$store.app.loadState();
    },
    toggleSeason(n) {
      const now = (this.s.seasons || []).filter((x) => x.selected).map((x) => x.number);
      const next = now.includes(n) ? now.filter((x) => x !== n) : [...now, n];
      this.opt({ seasons: next });
    },
    seasonState(se) {
      if (!se.episodes) return "анонс";
      if (se.have >= se.episodes) return "есть";
      if (se.have) return `есть ${se.have} из ${se.episodes}`;
      return `${se.episodes} сер.`;
    },
    seasonHint(se) {
      return `Серий: ${se.episodes || "?"}, год: ${se.year || "?"}, в медиатеке: ${se.have}`;
    },
    async download(r) {
      const res = await api.post("/api/torrents/search/download", { hash: r.hash });
      if (res && res.ok) this.$store.ui.toast("Добавлено в qBittorrent — разложу сам");
    },
    qualityLabel(q) { return q >= 2160 ? "4K" : q > 480 ? `${q}p` : q ? "SD" : "?"; },
    episodes(r) {
      if (!r.episodes || !r.episodes.length) return r.seasons.length > 1 ? `сез. ${r.seasons[0]}–${r.seasons[r.seasons.length - 1]}` : "—";
      const [first, last, total] = r.episodes;
      return first > 1 ? `${first}–${last}/${total}` : `${last}/${total}`;
    },
    size(bytes) {
      if (!bytes) return "—";
      const gb = bytes / 1024 ** 3;
      return gb >= 1 ? `${gb.toFixed(gb >= 100 ? 0 : 1)} ГБ` : `${Math.round(bytes / 1024 ** 2)} МБ`;
    },

    // ------------------------------------------------------- автоматика --
    lastCheck() {
      if (!this.t.last_check) return "Проверок ещё не было.";
      return `Последняя проверка — ${fmt.clock(this.t.last_check)}, дальше раз в полминуты.`;
    },
    badge(status) {
      return { done: "ok", ask: "warn", error: "danger", new: "accent" }[status] || "";
    },
    pct(p) { return `${Math.round((p || 0) * 100)}%`; },

    async checkNow() {
      if (await api.post("/api/torrents/check")) this.$store.ui.toast("Проверяю раздачи…");
    },
    async answer(hash, index) {
      if (await api.post("/api/torrents/answer", { hash, index })) {
        this.$store.ui.toast(index < 0 ? "Не раскладываю" : "Раскладываю…");
      }
    },
    async retry(hash) {
      if (await api.post("/api/torrents/retry", { hash })) this.$store.ui.toast("Разложу заново");
    },
  }));
});

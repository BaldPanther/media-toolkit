// Вкладка «Торренты»: поиск раздач и автоматика qBittorrent.
"use strict";

document.addEventListener("alpine:init", () => {
  Alpine.data("torrentsTab", () => ({
    view: "search",
    q: "",
    linkKind: "tv",       // вставлена ссылка: куда её положить
    showAll: false,
    choosing: false,      // снова показать карточки TMDb, когда тайтл уже выбран
    sorts: [["best", "Лучшие"], ["seeds", "По сидам"], ["size", "По размеру"]],
    sortBy: "best",
    sortDesc: true,

    get t() { return this.$store.app.tabs.torrents || {}; },
    get s() { return this.t.search || {}; },
    get busy() { return this.$store.app.busy; },

    init() {
      try {
        this.view = localStorage.getItem("mt.torrents.view") || "search";
        const [by, dir] = (localStorage.getItem("mt.torrents.sort") || "").split(":");
        if (this.sorts.some((o) => o[0] === by)) { this.sortBy = by; this.sortDesc = dir !== "asc"; }
      } catch (e) { /* без хранилища */ }
      // Сервер меняет название сам после «Сканировать» папки тайтла — подставить его.
      this.$watch("s.query", (v) => { if (v) this.q = v; });
      this.$watch("q", () => { if (this.isLink) this.linkKind = this.guessKind(); });
    },
    setView(v) {
      this.view = v;
      try { localStorage.setItem("mt.torrents.view", v); } catch (e) { /* без хранилища */ }
    },
    asking() { return (this.t.items || []).filter((i) => i.status === "ask").length; },

    // ------------------------------------------------------------ поиск --
    // magnet, хеш из 40 знаков или ссылка на .torrent — не поиск, а сразу в qBittorrent.
    get isLink() {
      const v = this.q.trim();
      return /^(magnet:|https?:\/\/)/i.test(v) || /^[0-9a-f]{40}$/i.test(v);
    },
    guessKind() {
      let name = this.q;
      try { name = decodeURIComponent((this.q.match(/[?&]dn=([^&]*)/) || [])[1] || this.q); } catch (e) { /* как есть */ }
      return /\bs\d{1,2}(e\d{1,3})?\b|сезон|сери[ияй]|season/i.test(name.replace(/[._+]/g, " ")) ? "tv" : "movie";
    },
    async addLink() {
      const res = await api.post("/api/torrents/add-link", { url: this.q.trim(), kind: this.linkKind });
      if (res && res.ok) {
        this.$store.ui.toast(res.text || "Добавлено в qBittorrent");
        this.q = "";
      }
    },
    async find() {
      if (!this.q.trim()) return;
      if (this.isLink) return this.addLink();
      this.showAll = false;
      const res = await api.post("/api/torrents/search", { query: this.q });
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
    // Порядок — здесь же, без сервера: «Лучшие» — как рекомендует программа.
    setSort(by) {
      if (by === this.sortBy && by !== "best") this.sortDesc = !this.sortDesc;
      else { this.sortBy = by; this.sortDesc = true; }
      try { localStorage.setItem("mt.torrents.sort", `${this.sortBy}:${this.sortDesc ? "desc" : "asc"}`); } catch (e) { /* без хранилища */ }
    },
    get releases() {
      const list = [...(this.s.releases || [])];
      if (this.sortBy === "best") return list;
      const dir = this.sortDesc ? -1 : 1;
      return list.sort((a, b) => dir * (a[this.sortBy] - b[this.sortBy]));
    },
    async opt(changes) {
      const res = await api.post("/api/torrents/search/options", changes);
      if (res && res.job) await this.$store.app.waitJob(res.job);
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
      return `Серий: ${se.episodes || "?"}, год: ${se.year || "?"}, в медиатеке: ${se.have}`
        + (se.tmdb ? `; в TMDb — ${se.tmdb}` : "");
    },
    async download(r) {
      const res = await api.post("/api/torrents/search/download", { hash: r.hash });
      if (res && res.ok) {
        this.$store.ui.toast(res.text === "Скачиваю" || !res.text ? "Добавлено в qBittorrent — разложу сам" : res.text);
        await this.$store.app.loadState();
      }
    },
    qualityLabel(q) { return q >= 2160 ? "4K" : q > 480 ? `${q}p` : q ? "SD" : "?"; },
    episodes(r) {
      if (!r.episodes || !r.episodes.length) {
        if (r.seasons.length > 1) return `сез. ${r.seasons[0]}–${r.seasons[r.seasons.length - 1]}`;
        return r.pack ? "сезон" : "—";
      }
      const [first, last, total] = r.episodes;
      if (!total) return first < last ? `${first}–${last}` : `серия ${last}`;   // «S01E07»: всего не знаем
      const text = first > 1 ? `${first}–${last}/${total}` : `${last}/${total}`;
      // У сборника серии посчитаны в его последнем сезоне.
      return r.seasons.length > 1 ? `сез. ${r.seasons[r.seasons.length - 1]}: ${text}` : text;
    },
    // Заметно крупнее вашей — скорее всего, качество лучше; заметно мельче — хуже.
    // Отношение — с поправкой на кодек (HEVC мельче при том же качестве).
    vsClass(ratio) { return ratio >= 1.5 ? "ok-text" : ratio <= 0.67 ? "warn-text" : "faint"; },
    codecHint(codec) {
      return {
        HEVC: "H.265 / x265: при том же качестве примерно на треть мельче AVC",
        AVC: "H.264 / x264: самый обычный, играет везде",
        AV1: "AV1: самый экономный — при том же качестве примерно вдвое мельче AVC; старые приставки его не тянут, новые умеют",
      }[codec] || "";
    },
    vsText(r) {
      return r.vs_library >= 0.8 && r.vs_library <= 1.25 ? "≈ как ваша" : `×${r.vs_library} к вашей`;
    },
    vsHint(r) {
      const base = "Сравнение с тем, что лежит в медиатеке (у сериала — на серию)";
      return r.vs_codec
        ? `${base}. По размеру ×${r.vs_raw}, но кодеки разные (HEVC экономнее AVC примерно на треть, AV1 — вдвое) — с поправкой ×${r.vs_library}`
        : `${base}: по размеру ×${r.vs_raw}`;
    },
    size(bytes) {
      if (!bytes) return "—";
      const gb = bytes / 1024 ** 3;
      return gb >= 1 ? `${gb.toFixed(gb >= 100 ? 0 : 1)} ГБ` : `${Math.round(bytes / 1024 ** 2)} МБ`;
    },

    // --------------------------------------------------------- слежение --
    async watch() {
      if (await api.post("/api/torrents/watch/add")) {
        this.$store.ui.toast("В слежении — проверю сейчас и дальше раз в день");
        await this.$store.app.loadState();
      }
    },
    async watchMode(id, mode) {
      await api.post("/api/torrents/watch/mode", { id, mode });
      await this.$store.app.loadState();
    },
    async watchFilter(id, change) {
      if (await api.post("/api/torrents/watch/filter", { id, ...change })) {
        this.$store.ui.toast("Условия сохранены — проверю сейчас");
        await this.$store.app.loadState();
      }
    },
    async pin(r) {
      if (await api.post("/api/torrents/watch/pin", { hash: r.hash })) {
        this.$store.ui.toast("Слежу за раздачей — сообщу, когда прибавятся серии");
        await this.$store.app.loadState();
      }
    },
    async watchUnpin(id) {
      await api.post("/api/torrents/watch/unpin", { id });
      await this.$store.app.loadState();
    },
    async watchCheck(id) {
      if (await api.post("/api/torrents/watch/check", { id })) this.$store.ui.toast("Проверяю…");
    },
    async watchRemove(w) {
      const ok = await this.$store.ui.ask({
        title: "Убрать из слежения?", text: w.title,
        buttons: [{ label: "Отмена", value: null, kind: "secondary" },
                  { label: "Убрать", value: true, kind: "danger" }],
      });
      if (!ok) return;
      await api.post("/api/torrents/watch/remove", { id: w.id });
      await this.$store.app.loadState();
    },
    async watchDownload(id, hash) {
      const res = await api.post("/api/torrents/watch/download", { id, hash });
      if (res) this.$store.ui.toast(res.text === "Скачиваю." || !res.text ? "Добавлено в qBittorrent — разложу сам" : res.text);
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

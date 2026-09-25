// Вкладка «Медиатека».
"use strict";

document.addEventListener("alpine:init", () => {
  Alpine.data("libraryTab", () => ({
    q: "",
    y: "",
    typing: false,
    // Окна вкладки.
    hitsOpen: false,
    hitIndex: 0,
    art: null,            // {title, kind, items} — сетка выбора картинки
    artFilter: "all",
    pendingOpen: false,
    pendingSel: "",
    episode: null,        // {index, name, season, episode}

    get t() { return this.$store.app.tabs.library || {}; },
    get busy() { return this.$store.app.busy; },

    init() {
      // Название и год подставляет сервер (из имени папки) — показываем их,
      // пока поле не правят руками.
      this.$watch("t.query", (v) => { if (!this.typing) this.q = v || ""; });
      this.$watch("t.year", (v) => { if (!this.typing) this.y = v || ""; });
    },

    async form(extra = {}) {
      await api.post("/api/library/form", { query: this.q, year: this.y, ...extra });
      this.$store.app.loadState();
    },
    async setKind(kind) { await this.form({ kind }); },

    // ------------------------------------------------------------ поиск --
    async find() {
      this.typing = false;
      const res = await api.post("/api/library/find",
        { kind: this.t.kind_choice, query: this.q, year: this.y });
      if (!res) return;
      const last = await this.$store.app.waitJob(res.job);
      await this.$store.app.loadState();
      if (!last || last.status !== "done") return;
      if (!this.t.hits.length) {
        await this.$store.ui.info("Не найдено",
          "TMDb ничего не вернул.\nПоправьте название или вставьте IMDb-ID вида tt6263850.");
      } else if (this.t.hits.length > 1) {
        this.openHits();
      }
    },
    openHits() { this.hitIndex = 0; this.hitsOpen = true; },
    async pickHit(i) {
      this.hitsOpen = false;
      await api.post("/api/library/select", { index: i });
    },
    async guess() {
      this.typing = false;
      await api.post("/api/library/guess");
      await this.$store.app.loadState();
      this.q = this.t.query; this.y = this.t.year;
    },

    // ---------------------------------------------- язык и картинки --
    async nameLanguage(choice) {
      await api.post("/api/library/name_language", { choice });
      this.$store.app.loadState();
    },
    async artLanguage(choice) {
      await api.post("/api/library/art_language", { choice });
      this.$store.app.loadState();
    },
    async setSeason(season) {
      await api.post("/api/library/season", { season });
      this.$store.app.loadState();
    },
    async openArt(kind) {
      const res = await api.post("/api/library/art/options", { kind });
      if (!res) return;
      this.artFilter = "all";
      this.art = res;
    },
    artItems() {
      if (!this.art) return [];
      const f = this.artFilter;
      return this.art.items.filter((c) => f === "all" || (f === "none" ? !c.lang : c.lang === f));
    },
    async chooseArt(item) {
      const kind = this.art.kind;
      this.art = null;
      await api.post("/api/library/art/choose", { kind, index: item.i });
      this.$store.app.loadState();
    },
    artClass(kind) {
      return { poster: "art-poster", seasonposter: "art-poster", fanart: "art-fanart", clearlogo: "art-logo" }[kind];
    },

    // ------------------------------------------------------------- план --
    rowBadge(kind) {
      return { change: "accent", warn: "warn", error: "danger", nochange: "plain" }[kind] || "";
    },
    async rebuild() {
      await api.post("/api/library/plan");
      this.$store.app.loadState();
    },
    async options(values) {
      await api.post("/api/library/options", values);
      this.$store.app.loadState();
    },
    async toggleJunk(row) {
      if (!row.junk || this.busy) return;
      await api.post("/api/library/junk", { index: row.i });
      this.$store.app.loadState();
    },
    async editEpisode(row) {
      if (!row.video || this.busy) return;
      const cur = await api.post("/api/library/episode/current", { index: row.i });
      if (cur) this.episode = { index: row.i, name: cur.name, season: String(cur.season), episode: String(cur.episode) };
    },
    async saveEpisode() {
      const e = this.episode;
      this.episode = null;
      await api.post("/api/library/episode", { index: e.index, season: e.season, episode: e.episode });
      this.$store.app.loadState();
    },
    async apply() {
      await api.post("/api/library/apply");
    },

    // ------------------------------------------ что не обработано --
    async pending() {
      const res = await api.post("/api/library/pending");
      if (!res) return;
      const last = await this.$store.app.waitJob(res.job);
      await this.$store.app.loadState();
      if (!last || last.status !== "done") return;
      if (!this.t.pending.length) {
        await this.$store.ui.info("Всё готово", "В библиотеке не нашлось необработанных папок.");
        return;
      }
      this.openPending();
    },
    openPending() {
      this.pendingSel = this.t.pending.length ? this.t.pending[0].path : "";
      this.pendingOpen = true;
    },
    async take(path) {
      path = path || this.pendingSel;
      if (!path) return;
      this.pendingOpen = false;
      await api.post("/api/library/pending/take", { path });
      await this.$store.app.loadState();
      this.q = this.t.query; this.y = this.t.year;
    },
    async ignore() {
      if (!this.pendingSel) return;
      const res = await api.post("/api/library/pending/ignore", { path: this.pendingSel });
      if (!res) return;
      await this.$store.app.loadState();
      this.pendingSel = this.t.pending.length ? this.t.pending[0].path : "";
    },

    // ---------------------------------------- служебные файлы систем --
    async systemJunk() {
      const res = await api.post("/api/library/system-junk");
      if (!res) return;
      const last = await this.$store.app.waitJob(res.job);
      await this.$store.app.loadState();
      if (!last || last.status !== "done") return;
      await api.post("/api/library/system-junk/delete");
    },
  }));
});

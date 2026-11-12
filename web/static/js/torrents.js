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
    // Следующий шаг: название введено, а по нему ещё не искали (или это ссылка).
    get findNext() {
      const q = this.q.trim();
      if (!q) return false;
      const searched = !!this.s.title || (this.s.hits || []).length > 0;
      return this.isLink || !searched || q !== (this.s.query || "");
    },

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
    // Клик — только этот сезон; Ctrl/Shift+клик — добавить к выбранным (или убрать).
    // Раньше клик добавлял: сразу отмеченный первый сезон оставался, и раздачи 1-го
    // сезона попадали в список, когда хотели 4-й.
    toggleSeason(n, ev) {
      const now = (this.s.seasons || []).filter((x) => x.selected).map((x) => x.number);
      if (!(ev && (ev.ctrlKey || ev.metaKey || ev.shiftKey))) return this.opt({ seasons: [n] });
      const next = now.includes(n) ? now.filter((x) => x !== n) : [...now, n];
      this.opt({ seasons: next });
    },
    seasonState(se) {
      if (se.state === "announce") return "анонс" + (se.date ? " " + se.date : "");
      if (se.have >= se.episodes) return "есть";
      if (se.state === "running") {
        return se.have ? `есть ${se.have}, вышло ${se.aired} из ${se.episodes}`
          : `идёт: ${se.aired} из ${se.episodes}`;
      }
      if (se.have) return `есть ${se.have} из ${se.episodes}`;
      return `${se.episodes} сер.`;
    },
    seasonHint(se) {
      return `Серий: ${se.episodes || "?"}`
        + (se.state === "running" ? ` (вышло ${se.aired})` : "")
        + `, год: ${se.year || "?"}, в медиатеке: ${se.have}`
        + (se.tmdb ? `; в TMDb — ${se.tmdb}` : "") + ". Ctrl+клик — выбрать несколько сезонов";
    },
    // all — «Всю раздачу»: сборник целиком, а не только выбранные сезоны из него.
    async download(r, all = false) {
      const res = await api.post("/api/torrents/search/download", { hash: r.hash, all });
      if (res && res.ok) {
        this.$store.ui.toast(res.text === "Скачиваю" || !res.text ? "Добавлено в qBittorrent — разложу сам" : res.text);
        await this.$store.app.loadState();
      }
    },
    qualityLabel(q) { return q >= 2160 ? "4K" : q > 480 ? `${q}p` : q ? "SD" : "?"; },
    // Сезон — всегда на виду: «8/8» без него не отличить 1-й сезон от 4-го.
    episodes(r) {
      const season = r.seasons.length ? `сез. ${r.seasons[r.seasons.length - 1]}: ` : "";
      if (!r.episodes || !r.episodes.length) {
        if (r.seasons.length > 1) return `сез. ${r.seasons[0]}–${r.seasons[r.seasons.length - 1]}`;
        return r.pack ? (season ? `сез. ${r.seasons[0]}` : "сезон") : "—";
      }
      const [first, last, total] = r.episodes;
      if (!total) return season + (first < last ? `${first}–${last}` : `серия ${last}`);   // «S01E07»: всего не знаем
      // У сборника серии посчитаны в его последнем сезоне.
      return season + (first > 1 ? `${first}–${last}/${total}` : `${last}/${total}`);
    },
    // Заметно крупнее вашей — скорее всего, качество лучше; заметно мельче — хуже.
    // Отношение — с поправкой на кодек (HEVC мельче при том же качестве).
    vsClass(ratio) { return ratio >= 1.5 ? "ok-text" : ratio <= 0.67 ? "warn-text" : "faint"; },
    // Сиды источник знает на момент последнего обхода трекера: у старых тем — месячной
    // давности. Свежие (до двух суток) — без даты; спрошенные у трекеров сейчас — «сейчас».
    seedsAge(r) {
      if (r.seeds_live) return "сейчас";
      const t = Date.parse(r.seeds_at || r.updated || "");
      if (!t || Date.now() - t < 2 * 86400 * 1000) return "";
      const d = new Date(t);
      const opts = { day: "numeric", month: "short" };
      if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
      return "на " + d.toLocaleDateString("ru-RU", opts);
    },
    seedsHint(r) {
      let text = r.seeds_live
        ? "Сиды — у трекеров прямо сейчас (rutor, nnm-club, открытые). Раздачи rutracker и kinozal так не посчитать: там может быть больше"
        : "Сиды — на момент, когда источник последний раз смотрел трекер: " + (r.seeds_at || r.updated || "?")
          + (r.alive ? ". Ссылка живая — проверено у трекеров сейчас" : "");
      if (r.rutracker_peers != null) {
        text += `. У трекера rutracker сейчас участников: ${this.peersText(r)} — сиды и качающие вместе`
          + " (больше 30 он не показывает). Сидов не бывает больше, чем участников";
      }
      return text;
    },
    // Участники у rutracker: больше 30 он не присылает — 30 значит «30 и больше».
    peersText(r) { return r.rutracker_peers >= 30 ? "30+" : String(r.rutracker_peers); },
    voiceLabel(v) {
      return { "Дубляж": "дубляж (любой студии)", "Любая русская": "любая русская (дубляж — первым)" }[v] || v;
    },
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

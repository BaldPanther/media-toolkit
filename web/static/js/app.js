// Каркас страницы: вкладки, строка папки, нижняя панель, настройки.
"use strict";

document.addEventListener("alpine:init", () => {
  Alpine.data("app", () => ({
    tab: "library",
    logOpen: false,
    pathInput: "",
    pathFocused: false,
    stopped: false,

    tabsList: [
      { id: "library", label: "Медиатека" },
      { id: "tracks", label: "Дорожки и субтитры" },
      { id: "edl", label: "Пропуск заставок (EDL)" },
    ],

    init() {
      const fromHash = location.hash.slice(1);
      const saved = this.load("tab");
      this.tab = this.tabsList.some((t) => t.id === fromHash) ? fromHash
        : this.tabsList.some((t) => t.id === saved) ? saved : "library";
      this.logOpen = this.load("logOpen") === "1";
      window.addEventListener("hashchange", () => {
        const id = location.hash.slice(1);
        if (this.tabsList.some((t) => t.id === id)) this.tab = id;
      });
      // Путь с сервера подставляется в поле, пока его не правят руками.
      this.$watch("$store.app.workspace.path", (p) => { if (!this.pathFocused) this.pathInput = p; });
      this.$store.app.start().then(() => { this.pathInput = this.$store.app.workspace.path; });
    },

    load(key) { try { return localStorage.getItem("mt." + key); } catch (e) { return null; } },
    save(key, value) { try { localStorage.setItem("mt." + key, value); } catch (e) { /* ничего */ } },

    setTab(id) {
      this.tab = id;
      this.save("tab", id);
      history.replaceState(null, "", "#" + id);
    },
    toggleLog() {
      this.logOpen = !this.logOpen;
      this.save("logOpen", this.logOpen ? "1" : "0");
      if (this.logOpen) this.$nextTick(() => this.scrollLog(true));
    },
    scrollLog(force = false) {
      const el = this.$refs.log;
      if (!el) return;
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
      if (force || nearBottom) el.scrollTop = el.scrollHeight;
    },

    // -------------------------------------------------------- папка --
    async commitPath() {
      this.pathFocused = false;
      const ws = this.$store.app.workspace;
      if (this.pathInput.trim() === ws.path) return;
      if (this.$store.app.busy) { this.pathInput = ws.path; return; }
      await api.post("/api/path", { path: this.pathInput.trim() });
      this.$store.app.pollSoon();
    },
    async browseFolder() {
      const p = await this.$store.browser.open({ start: this.pathInput || this.$store.app.workspace.path });
      if (p) { this.pathInput = p; await this.commitPath(); }
    },
    async browseFile() {
      const p = await this.$store.browser.open({ start: this.pathInput, files: true });
      if (p) { this.pathInput = p; await this.commitPath(); }
    },
    async setRecursive(value) {
      await api.post("/api/path", { path: this.$store.app.workspace.path, recursive: value });
      this.$store.app.pollSoon();
    },
    async scan() {
      await api.post("/api/scan", { path: this.pathInput.trim(),
                                    recursive: this.$store.app.workspace.recursive });
    },

    // ------------------------------------------------------ операции --
    async cancel() {
      await api.post("/api/cancel");
      this.$store.app.pollSoon();
    },
    async shutdown() {
      const ok = await this.$store.ui.confirm("Выключить программу?",
        "Сервер остановится, страница перестанет отвечать. Запустить снова — ярлыком программы.",
        "Выключить", true);
      if (!ok) return;
      const res = await api.post("/api/shutdown");
      if (res) this.stopped = true;
    },
    async logout() {
      await fetch("/api/logout", { method: "POST" });
      location.href = "/login";
    },

    // Подпись в нижней панели: что идёт сейчас или чем кончилось.
    dockText() {
      const st = this.$store.app.status;
      if (st.job) {
        const j = st.job;
        return `<b>${esc(j.title)}</b>${j.text ? " · " + esc(j.text) : ""}${j.cancelling ? " · отменяю…" : ""}`;
      }
      if (st.last && st.last.summary) return esc(st.last.summary);
      const log = this.$store.app.log;
      return log.length ? esc(log[log.length - 1].text) : "Готово к работе.";
    },
    progressWidth() {
      const j = this.$store.app.status.job;
      if (!j || !j.maximum) return "0%";
      return `${Math.min(100, (100 * j.value) / j.maximum).toFixed(1)}%`;
    },
  }));

  // --------------------------------------------------------- настройки --
  Alpine.data("settings", () => ({
    visible: false,
    section: "keys",
    data: null,
    saving: false,
    showPass: false,
    artPick: "",
    sections: [
      { id: "keys", label: "Ключи API", icon: "i-key" },
      { id: "subs", label: "OpenSubtitles", icon: "i-subtitles" },
      { id: "lang", label: "Языки и имена", icon: "i-globe" },
      { id: "art", label: "Картинки", icon: "i-image" },
      { id: "roots", label: "Корни библиотеки", icon: "i-library" },
    ],

    async open(section) {
      const data = await api.get("/api/settings");
      if (!data) return;
      this.data = data;
      this.section = section || "keys";
      this.showPass = false;
      this.artPick = "";
      this.visible = true;
    },
    close() { this.visible = false; },

    artLabel(code) {
      const labels = this.data.choices.art_languages;
      return code in labels ? labels[code] : code;
    },
    artAvailable() {
      const have = new Set(this.data.meta.art_languages);
      return Object.entries(this.data.choices.art_languages).filter(([code]) => !have.has(code));
    },
    // Выбор в списке «Добавить…» — сразу в конец приоритета; список сбрасывается.
    artAdd(event) {
      const code = event.target.value;
      event.target.value = "__none__";
      if (code === "__none__") return;
      const list = this.data.meta.art_languages;
      if (!list.includes(code)) list.push(code);
    },
    // Любой код ISO 639-1, которого нет в списке (например «pl»).
    artAddCode() {
      const code = this.artPick.trim().toLowerCase();
      this.artPick = "";
      if (!/^[a-z]{2}$/.test(code)) return;
      const list = this.data.meta.art_languages;
      if (!list.includes(code)) list.push(code);
    },
    artMove(i, d) {
      const list = this.data.meta.art_languages;
      const j = i + d;
      if (j < 0 || j >= list.length) return;
      [list[i], list[j]] = [list[j], list[i]];
    },
    artRemove(i) { this.data.meta.art_languages.splice(i, 1); },

    async addRoot(kind) {
      const list = this.data.meta[kind];
      const p = await this.$store.browser.open({
        start: list[list.length - 1] || "",
        title: kind === "movies_roots" ? "Папка с фильмами" : "Папка с сериалами",
      });
      if (p && !list.includes(p)) list.push(p);
    },
    removeRoot(kind, i) { this.data.meta[kind].splice(i, 1); },

    async save() {
      this.saving = true;
      try {
        const res = await api.post("/api/settings", { meta: this.data.meta, subs: this.data.subs });
        if (res) {
          this.visible = false;
          this.$store.ui.toast("Настройки сохранены");
        }
      } finally {
        this.saving = false;
      }
    },
  }));
});

// Текст → безопасный HTML (для x-html в подписях с выделением).
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

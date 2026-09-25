// Вкладка «Пропуск заставок (EDL)».
"use strict";

const EDL_BOUNDS = [
  ["ie", "конец интро"], ["is", "начало интро"], ["os", "начало титров"],
  ["oe", "конец титров"], ["rc", "конец recap"],
];
// Граница → отступ сезона, который её двигает (у recap отступа нет).
const EDL_BOUND_PAD = { is: "intro_start", ie: "intro_end", os: "outro_start", oe: "outro_end", rc: null };

document.addEventListener("alpine:init", () => {
  Alpine.data("edlTab", () => ({
    sub: "opt",
    scope: "all",
    sel: {},              // выделенные серии: путь → true
    anchor: null,         // от какой строки тянуть выделение с Shift
    manual: { intro_start: "", intro_end: "", intro_dur: "", outro_start: "", outro_end: "",
              outro_last: "", outro_from_start: "", recap_end: "" },
    calcText: "",
    edit: null,           // правка серии
    fr: null,             // полоса кадров в окне правки
    online: null,         // окно онлайн-правил

    get t() { return this.$store.app.tabs.edl || {}; },
    get busy() { return this.$store.app.busy; },
    get rows() { return this.t.rows || []; },

    init() {
      // Пересканировали — выделение по путям, которых больше нет, снимаем.
      this.$watch("t.rows", (rows) => {
        const have = new Set((rows || []).map((r) => r.path));
        for (const p of Object.keys(this.sel)) if (!have.has(p)) delete this.sel[p];
      });
    },

    // ---------------------------------------------------- выделение --
    selected() { return Object.keys(this.sel).filter((p) => this.sel[p]); },
    scopeBody() { return { scope: this.scope, selected: this.selected() }; },
    rowClick(ev, row, i) {
      if (ev.detail > 1) return;                  // второй клик двойного — не трогаем выделение
      if (ev.shiftKey && this.anchor !== null) {
        const [a, b] = [Math.min(this.anchor, i), Math.max(this.anchor, i)];
        if (!(ev.metaKey || ev.ctrlKey)) this.sel = {};
        for (let k = a; k <= b; k++) this.sel[this.rows[k].path] = true;
      } else if (ev.metaKey || ev.ctrlKey) {
        this.sel[row.path] = !this.sel[row.path];
        this.anchor = i;
      } else {
        const only = this.sel[row.path] && this.selected().length === 1;
        this.sel = only ? {} : { [row.path]: true };
        this.anchor = i;
      }
    },
    toggleRow(row, i) {
      this.sel[row.path] = !this.sel[row.path];
      this.anchor = i;
    },
    allSelected() { return this.rows.length > 0 && this.selected().length === this.rows.length; },
    toggleAll() {
      if (this.allSelected()) this.sel = {};
      else this.sel = Object.fromEntries(this.rows.map((r) => [r.path, true]));
    },

    // ----------------------------------------------------- настройки --
    async settings(patch) {
      await api.post("/api/edl/settings", patch);
      this.$store.app.loadState();
    },
    async sortBy(col) {
      await api.post("/api/edl/sort", { col });
      this.$store.app.loadState();
    },
    sortMark(col) {
      const [c, rev] = this.t.sort || [];
      return c === col ? (rev ? " ▼" : " ▲") : "";
    },

    // ------------------------------------------------ задать вручную --
    async manualOp(op) {
      const res = await api.post("/api/edl/manual", { op, values: this.manual, ...this.scopeBody() });
      if (res) this.$store.app.loadState();
    },
    async calc() {
      const res = await api.post("/api/edl/manual",
        { op: "calc_outro_last", values: this.manual, selected: this.selected() });
      if (res && res.outro_last) {
        this.manual.outro_last = res.outro_last;
        this.calcText = res.calc;
      }
    },

    // ---------------------------------------------------- операции --
    async detect() { await api.post("/api/edl/detect", this.scopeBody()); },
    async write() { await api.post("/api/edl/write", this.scopeBody()); },
    async del() {
      const res = await api.post("/api/edl/delete", this.scopeBody());
      if (res) this.$store.app.loadState();
    },
    async writeChapters() { await api.post("/api/edl/chapters", this.scopeBody()); },
    async clearChapters() { await api.post("/api/edl/chapters/clear", this.scopeBody()); },

    // ------------------------------------------------- правка серии --
    async openEdit(row) {
      if (this.busy) return;
      const res = await api.post("/api/edl/episode/get", { path: row.path });
      if (!res) return;
      this.edit = res;
      this.fr = { bound: "ie", step: String(this.t.frame_step || 1), token: "", times: [],
                  ready: {}, pick: null, center: null, status: "", note: "Нажмите «Показать кадры»." };
    },
    lengthText(key) {
      const e = this.edit;
      if (!e) return "";
      const p = (x) => fmt.parseTime(x);
      const put = (len, note = "") => (len !== null && len > 0
        ? `длительность ${fmt.time(len)} = ${Math.round(len)} сек${note}` : "");
      if (key === "rc") return put(p(e.rc));
      if (key === "ie") {
        const s = p(e.is), en = p(e.ie);
        return put(s !== null && en !== null ? en - s : null);
      }
      if (key === "oe") {
        const s = p(e.os), en = p(e.oe);
        const end = en !== null ? en : e.duration;
        return put(s !== null && end ? end - s : null, en !== null ? "" : " (до конца файла)");
      }
      return "";
    },
    async saveEdit() {
      const e = this.edit;
      const res = await api.post("/api/edl/episode", {
        path: e.path, rc: e.rc, is: e.is, ie: e.ie, os: e.os, oe: e.oe,
        intro_dur: this.manual.intro_dur });
      if (!res) return;
      this.closeEdit();
      this.$store.app.loadState();
    },
    closeEdit() {
      this.edit = null;
      if (this.fr) this.fr.token = "";
      this.fr = null;
    },

    // --------------------------------------------- проверка кадром --
    // Полоса строится вокруг ДЕЙСТВУЮЩЕЙ границы (значение серии плюс отступ
    // сезона) — именно её и пропустит Kodi.
    padOf(bound) {
      const key = EDL_BOUND_PAD[bound];
      return key ? (fmt.parseTime(this.t.pad[key]) || 0) : 0;
    },
    effective(bound) {
      const raw = fmt.parseTime(this.edit[bound]);
      return raw === null ? null : raw + this.padOf(bound);
    },
    boundLabel(bound) { return (EDL_BOUNDS.find((b) => b[0] === bound) || [])[1]; },
    boundChanged() {
      Object.assign(this.fr, { token: "", times: [], ready: {}, pick: null, center: null,
                               note: "Нажмите «Показать кадры»." });
    },
    async showFrames() {
      const center = this.effective(this.fr.bound);
      if (center === null) {
        await this.$store.ui.info("Нет границы", `У этой серии не задана граница «${this.boundLabel(this.fr.bound)}» — `
          + "показывать нечего вокруг пустого значения.");
        return;
      }
      const res = await api.post("/api/edl/frames", { path: this.edit.path, center, step: this.fr.step });
      if (!res) return;
      Object.assign(this.fr, { token: res.token, times: res.times, ready: {}, pick: null, center,
        note: `Действующая граница: ${fmt.time(center)}. Кликните кадр, на котором граница должна быть.`,
        status: `читаю кадры 0/${res.times.length}…` });
      this.pollFrames(res.token);
    },
    async pollFrames(token) {
      while (this.fr && this.fr.token === token) {
        let data;
        try {
          data = await (await fetch(`/api/edl/frames/${token}`)).json();
        } catch (e) { return; }
        if (!this.fr || this.fr.token !== token || data.error) return;
        this.fr.ready = data.ready;
        const got = Object.keys(data.ready).length;
        this.fr.status = data.done ? "" : `читаю кадры ${got}/${this.fr.times.length}…`;
        if (data.done) return;
        await new Promise((r) => setTimeout(r, 300));
      }
    },
    frameSrc(i) { return this.fr.ready[i] ? `/api/edl/frames/${this.fr.token}/${i}.png` : ""; },
    pickFrame(i) {
      if (!this.fr.ready[i]) return;
      this.fr.pick = this.fr.times[i];
      const delta = this.fr.pick - this.fr.center;
      this.fr.note = `Сейчас ${fmt.time(this.fr.center)} → выбрано ${fmt.time(this.fr.pick)} `
        + `(сдвиг ${delta >= 0 ? "+" : ""}${Math.round(delta)} с)`;
    },
    delta() { return this.fr && this.fr.pick !== null ? this.fr.pick - this.fr.center : 0; },
    toEpisode() {
      // В поле живёт «сырое» значение, отступ сезона ляжет на него сверху.
      const b = this.fr.bound;
      this.edit[b] = fmt.time(this.fr.pick - this.padOf(b));
      this.fr.note = `Граница «${this.boundLabel(b)}» этой серии — ${fmt.time(this.fr.pick)}. `
        + "Не забудьте «Сохранить».";
    },
    async toSeason() {
      const res = await api.post("/api/edl/shift",
        { bound: this.fr.bound, delta: this.delta(), path: this.edit.path });
      if (!res) return;
      await this.$store.app.loadState();
      this.fr.note = `Отступ «${this.boundLabel(this.fr.bound)}» теперь ${res.pad} с — применён ко всем сериям.`;
    },
    async inPlayer() {
      const at = this.fr.pick !== null ? this.fr.pick : this.effective(this.fr.bound);
      if (at === null) {
        await this.$store.ui.info("Нет границы", `У этой серии не задана граница «${this.boundLabel(this.fr.bound)}» — открывать не на чем.`);
        return;
      }
      const res = await api.post("/api/edl/player", { path: this.edit.path, at });
      if (res) this.fr.note = `Открыто в ${res.player} на ${res.at}.`;
    },

    // ------------------------------------------ онлайн-тайминги --
    async openOnline() {
      const res = await api.post("/api/edl/online/rules");
      if (!res) return;
      this.online = { ...res, suggesting: false, summary: "", candidates: [] };
    },
    addRule() {
      this.online.rules.push({ source: "AniSkip", id: "", from: this.online.emin,
                               to: this.online.emax, first: 1 });
    },
    async suggest() {
      this.online.suggesting = true;
      try {
        const res = await api.post("/api/edl/online/suggest");
        if (res) Object.assign(this.online, { rules: res.rules, summary: res.summary,
                                              candidates: res.candidates });
      } finally {
        if (this.online) this.online.suggesting = false;
      }
    },
    async loadOnline() {
      const res = await api.post("/api/edl/online/load", { rules: this.online.rules });
      if (res) this.online = null;
    },
  }));
});

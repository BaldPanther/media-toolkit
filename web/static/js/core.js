// Общее для всей страницы: запросы к серверу, диалоги, опрос статуса,
// окно выбора папки. Вкладки — в своих файлах и пользуются тем, что здесь.
"use strict";

// ---------------------------------------------------------------- формат --
const fmt = {
  // Секунды → «1:02:03» / «2:03», без долей секунды — как пишет сервер.
  time(s) {
    if (s === null || s === undefined || Number.isNaN(s)) return "—";
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const mm = String(m).padStart(h ? 2 : 1, "0"), ss = String(sec).padStart(2, "0");
    return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
  },
  // Время строки журнала.
  clock(t) {
    const d = new Date(t * 1000);
    return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  },
  // «MM:SS», «H:MM:SS» или секунды → число секунд; пусто или мусор → null.
  parseTime(txt) {
    txt = String(txt ?? "").trim().replace(",", ".");
    if (!txt) return null;
    let s = 0;
    for (const part of txt.split(":")) {
      if (part === "" || Number.isNaN(Number(part))) return null;
      s = s * 60 + Number(part);
    }
    return s;
  },
  plural(n, one, few, many) {
    const a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return many;
    if (b > 1 && b < 5) return few;
    if (b === 1) return one;
    return many;
  },
};

// ------------------------------------------------------------ запросы --
// Ответ сервера бывает четырёх видов: данные, ошибка (показать и остановиться),
// сообщение (показать и продолжить) и вопрос. На вопрос страница показывает
// диалог и повторяет тот же запрос с ответом в `answers` — см. web/replies.py.
const api = {
  async call(method, url, body = {}) {
    const answers = {};
    for (;;) {
      let res, data;
      try {
        res = await fetch(url, {
          method,
          headers: method === "GET" ? {} : { "Content-Type": "application/json" },
          body: method === "GET" ? undefined : JSON.stringify({ ...body, answers }),
        });
      } catch (e) {
        await Alpine.store("ui").error("Нет связи с программой",
          "Сервер не отвечает. Если программа выключена — запустите её снова.");
        return null;
      }
      try {
        data = await res.json();
      } catch (e) {
        await Alpine.store("ui").error("Ошибка сервера", `Ответ ${res.status} — подробности в журнале сервера.`);
        return null;
      }
      if (data.login) { location.href = "/login"; return null; }
      if (data.error) {
        await Alpine.store("ui").error(data.error.title, data.error.text);
        return null;
      }
      if (data.ask) {
        const value = await Alpine.store("ui").ask(data.ask);
        if (value === null || value === undefined) return null;
        answers[data.ask.id] = value;
        continue;
      }
      if (data.message) await Alpine.store("ui").info(data.message.title, data.message.text);
      if (data.job) Alpine.store("app").watchJob(data.job);
      return data;
    }
  },
  get(url) { return this.call("GET", url); },
  post(url, body = {}) { return this.call("POST", url, body); },
};

document.addEventListener("alpine:init", () => {
  // ------------------------------------------------------------ диалоги --
  Alpine.store("ui", {
    dialogs: [],        // стек: {kind, title, text, buttons, resolve}
    toasts: [],

    _seq: 0,
    open(dialog) {
      const id = ++this._seq;
      return new Promise((resolve) => this.dialogs.push({ ...dialog, id, resolve }));
    },
    close(dialog, value) {
      this.dialogs = this.dialogs.filter((d) => d !== dialog);
      dialog.resolve(value);
    },
    ask(q) {
      return this.open({ kind: "ask", title: q.title, text: q.text, buttons: q.buttons });
    },
    error(title, text = "") {
      return this.open({ kind: "error", title, text,
        buttons: [{ label: "Понятно", value: true, kind: "primary" }] });
    },
    info(title, text = "") {
      return this.open({ kind: "info", title, text,
        buttons: [{ label: "OK", value: true, kind: "primary" }] });
    },
    confirm(title, text, yes = "Да", danger = false) {
      return this.open({ kind: "ask", title, text, buttons: [
        { label: "Отмена", value: null, kind: "secondary" },
        { label: yes, value: true, kind: danger ? "danger" : "primary" }] });
    },
    toast(text, ms = 3500) {
      const t = { id: Date.now() + Math.random(), text };
      this.toasts.push(t);
      setTimeout(() => { this.toasts = this.toasts.filter((x) => x !== t); }, ms);
    },
    // Клавиши в верхнем диалоге: Esc — отмена, Enter — главная кнопка.
    key(e) {
      const top = this.dialogs[this.dialogs.length - 1];
      if (!top) return false;
      if (e.key === "Escape") {
        const cancel = top.buttons.find((b) => b.value === null);
        this.close(top, cancel ? null : top.buttons[top.buttons.length - 1].value);
        return true;
      }
      if (e.key === "Enter") {
        const main = top.buttons.find((b) => b.kind === "primary" || b.kind === "danger")
          || top.buttons[top.buttons.length - 1];
        this.close(top, main.value);
        return true;
      }
      return false;
    },
  });

  // ---------------------------------------------------------- состояние --
  // Всё, что показывает страница, приходит с сервера (/api/state). Здесь —
  // его копия и опрос: раз в секунду /api/status, а когда на сервере что-то
  // поменялось (растёт version), — заново /api/state.
  Alpine.store("app", {
    info: { mode: "desktop", edl_missing: [], auth: false },
    version: -1,
    workspace: { path: "", recursive: true, summary: "" },
    tabs: {},
    status: { busy: false, job: null, last: null },
    log: [],
    logSeq: 0,
    connected: true,
    loaded: false,
    watched: new Set(),   // операции, начатые с этой страницы, — их ошибки показать
    shownFailures: new Set(),
    waiters: [],          // кто ждёт конца операции (waitJob)

    get busy() { return this.status.busy; },

    watchJob(job) {
      if (job && job.id) this.watched.add(job.id);
      this.status.busy = true;
      this.pollSoon();
    },

    async loadState() {
      try {
        const res = await fetch("/api/state");
        if (res.status === 401) { location.href = "/login"; return; }
        const data = await res.json();
        this.version = data.version;
        this.workspace = data.workspace;
        this.tabs = data.tabs;
        this.loaded = true;
      } catch (e) { /* связи нет — опрос покажет */ }
    },

    async poll() {
      clearTimeout(this._timer);
      try {
        const res = await fetch(`/api/status?log=${this.logSeq}`);
        if (res.status === 401) { location.href = "/login"; return; }
        const data = await res.json();
        this.connected = true;
        if (data.job) this.watched.add(data.job.id);
        this.status = { busy: data.busy, job: data.job, last: data.last };
        if (data.log.length) {
          this.log.push(...data.log);
          if (this.log.length > 1500) this.log.splice(0, this.log.length - 1500);
          this.logSeq = data.log[data.log.length - 1].seq;
        }
        if (data.version !== this.version) await this.loadState();
        const last = data.last;
        if (this.waiters.length) {
          this.waiters = this.waiters.filter((w) => {
            if (data.job && data.job.id === w.id) return true;
            if (last && last.id >= w.id) { w.resolve(last.id === w.id ? last : null); return false; }
            return true;
          });
        }
        if (last && last.status === "failed" && this.watched.has(last.id)
            && !this.shownFailures.has(last.id)) {
          this.shownFailures.add(last.id);
          Alpine.store("ui").error(last.title, last.error);
        }
      } catch (e) {
        this.connected = false;
      }
      this._timer = setTimeout(() => this.poll(), this.status.busy ? 500 : 1000);
    },

    // Дождаться конца операции: отдаёт её итог (status: done/failed/cancelled).
    waitJob(job) {
      if (!job) return Promise.resolve(null);
      return new Promise((resolve) => {
        this.waiters.push({ id: job.id, resolve });
        this.pollSoon();
      });
    },

    pollSoon() {
      clearTimeout(this._timer);
      this._timer = setTimeout(() => this.poll(), 150);
    },

    async start() {
      try {
        const res = await fetch("/api/info");
        this.info = await res.json();
      } catch (e) { /* покажет опрос */ }
      await this.loadState();
      this.poll();
    },
  });

  // -------------------------------------------------- окно выбора папки --
  // Системного диалога у браузера нет (он не видит диски сервера), поэтому
  // своё окно: ходит по папкам через /api/fs. open() отдаёт выбранный путь
  // или null, если окно закрыли.
  Alpine.store("browser", {
    visible: false,
    files: false,         // выбираем файл (фильм одним файлом), а не папку
    title: "",
    path: "",
    parent: "",
    entries: [],
    roots: [],
    selected: "",
    typed: "",
    loading: false,
    _resolve: null,

    open({ start = "", files = false, title = "" } = {}) {
      this.files = files;
      this.title = title || (files ? "Выбор файла" : "Выбор папки");
      this.selected = "";
      this.visible = true;
      this.go(start);
      return new Promise((resolve) => { this._resolve = resolve; });
    },
    async go(path) {
      this.loading = true;
      try {
        const q = new URLSearchParams({ path: path || "", files: this.files ? "1" : "0" });
        const res = await fetch(`/api/fs?${q}`);
        const data = await res.json();
        if (data.error) {
          // Такой папки нет — остаёмся где были, но показываем список мест.
          Alpine.store("ui").toast(`${data.error.title}: ${data.error.text}`);
          if (!this.roots.length) await this.go("");
        } else {
          this.path = data.path;
          this.parent = data.parent;
          this.entries = data.entries;
          this.roots = data.roots;
          this.typed = data.path;
          this.selected = "";
        }
      } finally {
        this.loading = false;
      }
    },
    crumbs() {
      if (!this.path) return [];
      const sep = this.path.includes("\\") && !this.path.includes("/") ? "\\" : "/";
      const parts = this.path.split(sep).filter(Boolean);
      const out = [];
      let acc = this.path.startsWith(sep) ? sep : "";
      for (const part of parts) {
        acc = acc && !acc.endsWith(sep) ? acc + sep + part : acc + part;
        out.push({ name: part, path: acc.endsWith(":") ? acc + sep : acc });
      }
      return out;
    },
    click(entry) {
      if (entry.dir) this.go(entry.path);
      else this.selected = entry.path;
    },
    pick(entry) {
      if (entry.dir) this.go(entry.path);
      else this.done(entry.path);
    },
    done(value) {
      this.visible = false;
      if (this._resolve) this._resolve(value ?? null);
      this._resolve = null;
    },
    choose() {
      if (this.files) { if (this.selected) this.done(this.selected); }
      else if (this.path) this.done(this.path);
    },
  });
});

// Вкладка «Дорожки и субтитры».
"use strict";

document.addEventListener("alpine:init", () => {
  Alpine.data("tracksTab", () => ({
    ...rowSelection(),

    get t() { return this.$store.app.tabs.tracks || {}; },
    get rows() { return this.t.rows || []; },
    hasFiles() { return this.rows.length > 0; },

    init() {
      this.$watch("t.rows", (rows) => this.pruneSelection(rows));
    },

    // Строка вне «Применять к» — её «Применить» не тронет, показываем приглушённой.
    inScope(row) { return this.scope === "all" || !!this.sel[row.path]; },
    count(kind) {
      return this.rows.filter((r) => this.inScope(r)
        && (kind === "warn" ? r.kind === "warn" || r.kind === "error" : r.kind === kind)).length;
    },

    badge(kind) {
      return { change: "accent", warn: "warn", error: "danger", nochange: "plain" }[kind] || "";
    },
    async choose(audio, sub) {
      await api.post("/api/tracks/choice", { audio, sub });
      this.$store.app.loadState();
    },
    async apply() {
      await api.post("/api/tracks/apply", this.scopeBody());
    },
    async downloadSubs() {
      await api.post("/api/tracks/subs", { only_missing: this.t.subs_only_missing, ...this.scopeBody() });
    },
  }));
});

// Вкладка «Дорожки и субтитры».
"use strict";

document.addEventListener("alpine:init", () => {
  Alpine.data("tracksTab", () => ({
    get t() { return this.$store.app.tabs.tracks || {}; },
    hasFiles() { return (this.t.rows || []).length > 0; },

    badge(kind) {
      return { change: "accent", warn: "warn", error: "danger", nochange: "plain" }[kind] || "";
    },
    async choose(audio, sub) {
      await api.post("/api/tracks/choice", { audio, sub });
      this.$store.app.loadState();
    },
    async apply() {
      await api.post("/api/tracks/apply");
    },
    async downloadSubs() {
      await api.post("/api/tracks/subs", { only_missing: this.t.subs_only_missing });
    },
  }));
});

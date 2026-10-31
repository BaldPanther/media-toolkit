// Вкладка «Торренты».
"use strict";

document.addEventListener("alpine:init", () => {
  Alpine.data("torrentsTab", () => ({
    get t() { return this.$store.app.tabs.torrents || {}; },

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

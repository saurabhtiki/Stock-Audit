// Items list: tick New items (by hand, whole page, or every item matching the filter)
// and mark them Reviewed together. Loaded before Alpine starts (Alpine is deferred).
document.addEventListener("alpine:init", () => {
  Alpine.data("bulkReview", () => ({
    picked: [], // ids ticked by hand
    allMatching: false, // "select all N matching" is on
    excluded: [], // ids unticked while allMatching is on
    matching: 0, // New items matching the current search/filters (from the server)
    filters: "", // the list's search and column filters, e.g. "q=rm&uom=KG"
    filtered: false, // any search or filter on
    busy: false,

    // Called by #item-table every time it is (re)loaded. New filters = start the ticking again.
    tableLoaded(table) {
      if ((table.dataset.filters || "") !== this.filters) this.reset();
      this.matching = Number(table.dataset.matching || 0);
      this.filters = table.dataset.filters || "";
      this.filtered = table.dataset.filtered !== "0";
    },
    reset() {
      this.picked = [];
      this.excluded = [];
      this.allMatching = false;
    },
    get count() {
      return this.allMatching ? this.matching - this.excluded.length : this.picked.length;
    },
    isTicked(id) {
      return this.allMatching ? !this.excluded.includes(id) : this.picked.includes(id);
    },
    tick(id, on) {
      const list = this.allMatching ? this.excluded : this.picked;
      const keep = this.allMatching ? !on : on;
      const index = list.indexOf(id);
      if (keep && index < 0) list.push(id);
      if (!keep && index >= 0) list.splice(index, 1);
    },
    tickPage(on) {
      this.$root.querySelectorAll("[data-review-id]").forEach((box) => this.tick(box.dataset.reviewId, on));
    },
    selectAllMatching() {
      this.allMatching = true;
      this.excluded = [];
    },
    plural(n) {
      return `${n.toLocaleString()} New item${n === 1 ? "" : "s"}`;
    },
    selectedText() {
      return `${this.plural(this.count)} ticked`;
    },
    selectAllText() {
      const filter = this.filtered ? " matching your filters" : "";
      return `Select all ${this.plural(this.matching)}${filter}`;
    },
    confirmText() {
      return `${this.plural(this.count)} will be marked Reviewed. Continue?`;
    },
  }));
});

/* Pop-ups for the server-side table (templates/partials/datatable/).
   The panel uses position: fixed so the table's sideways scroll box can't cut it off.
   Loaded before Alpine starts (both deferred, this one first). */
document.addEventListener("alpine:init", () => {
  Alpine.data("dtPopup", () => ({
    open: false,
    style: {},
    toggle() {
      if (this.open) return this.close();
      this.place();
      this.open = true;
      this.$nextTick(() => this.$refs.panel.querySelector("input:not([type=hidden])")?.focus());
    },
    close() {
      this.open = false;
    },
    place() {
      const box = this.$refs.button.getBoundingClientRect();
      const screen = window.innerWidth;
      const phone = screen < 640;
      const width = phone ? screen - 32 : 288;
      const left = phone ? 16 : Math.max(16, Math.min(box.left, screen - width - 16));
      let top = box.bottom + 6;
      // Little room below the button (short screen): move the pop-up up so it stays on screen.
      if (window.innerHeight - top < 320) top = Math.max(16, window.innerHeight - 16 - 400);
      const height = window.innerHeight - top - 16;
      // An object (not a string) so Alpine merges it with x-show's display: none.
      this.style = { top: `${top}px`, left: `${left}px`, width: `${width}px`, maxHeight: `${height}px` };
    },
  }));

  Alpine.data("dtChoice", () => ({
    search: "",
    matches(label) {
      return label.toLowerCase().includes(this.search.trim().toLowerCase());
    },
    // Ticks or unticks only the values still shown after "Find a value".
    setAll(on) {
      this.$root.querySelectorAll("label[data-label]").forEach((row) => {
        if (this.matches(row.dataset.label)) row.querySelector("input").checked = on;
      });
    },
  }));
});

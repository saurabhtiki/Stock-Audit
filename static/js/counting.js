/* Count screen: after Enter saves a Physical Qty, jump to the next line's box,
   so the auditor can type count, Enter, count, Enter… down the page. */
(() => {
  let pending = null; // { row: id of the saved line, next: id of the line below }

  document.addEventListener("keydown", (event) => {
    const input = event.target;
    if (event.key !== "Enter" || !input.classList?.contains("count-input")) return;
    const row = input.closest("tr");
    pending = { row: row?.id, next: row?.nextElementSibling?.id };
  });

  document.addEventListener("htmx:afterSettle", () => {
    if (!pending) return;
    const { row, next } = pending;
    pending = null;
    // Stay on the box if the count was refused, so the auditor can fix it.
    const refused = document.querySelector(`#${row} .count-input.input-error`);
    const box = refused || (next && document.querySelector(`#${next} .count-input`));
    box?.focus();
    box?.select();
  });
})();

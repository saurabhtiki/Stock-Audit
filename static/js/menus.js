// Top-bar menus (<details class="dropdown">): only one stays open at a time.
// A menu closes when you open another one, click anywhere else, or press Esc.
(function () {
  "use strict";

  function openMenus() {
    return document.querySelectorAll("details.dropdown[open]");
  }

  document.addEventListener("click", function (event) {
    openMenus().forEach(function (menu) {
      // Keep the menu you just clicked on; close all the others.
      if (!menu.contains(event.target)) {
        menu.removeAttribute("open");
      }
    });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    openMenus().forEach(function (menu) {
      menu.removeAttribute("open");
    });
  });
})();

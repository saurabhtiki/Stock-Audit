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

  // Phone side menu (a checkbox): close it when a page comes back with Back / Forward,
  // so the page isn't hidden behind a menu left open.
  window.addEventListener("pageshow", function () {
    var drawer = document.getElementById("nav-drawer");
    if (drawer) drawer.checked = false;
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var drawer = document.getElementById("nav-drawer");
    if (drawer) drawer.checked = false;
    openMenus().forEach(function (menu) {
      menu.removeAttribute("open");
    });
  });
})();

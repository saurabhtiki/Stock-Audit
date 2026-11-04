/* Light/dark theme. Runs in <head> so the saved theme shows before the page paints. */
(function () {
  var KEY = "stockaudit-theme";
  var LIGHT = "stockaudit";
  var DARK = "stockaudit-dark";

  function saved() {
    try {
      return localStorage.getItem(KEY);
    } catch (error) {
      return null; // private window or blocked storage: just use the default
    }
  }

  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme);
  }

  var prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  apply(saved() || (prefersDark ? DARK : LIGHT));

  // Used by the toggle button: x-data="themeToggle()"
  window.themeToggle = function () {
    return {
      dark: document.documentElement.getAttribute("data-theme") === DARK,
      toggle: function () {
        this.dark = !this.dark;
        var theme = this.dark ? DARK : LIGHT;
        apply(theme);
        try {
          localStorage.setItem(KEY, theme);
        } catch (error) {
          /* not saved; the choice still works until the page is closed */
        }
      },
    };
  };
})();

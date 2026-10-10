(() => {
  const buttons = document.querySelectorAll("[data-theme-toggle]");
  const themeColor = document.querySelector('meta[name="theme-color"]');

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    buttons.forEach((button) => {
      button.setAttribute("aria-pressed", String(theme === "dark"));
      button.setAttribute(
        "aria-label",
        theme === "dark" ? "Switch to light mode" : "Switch to dark mode",
      );
    });
    if (themeColor) {
      themeColor.content = theme === "dark" ? "#131b18" : "#f5f7f2";
    }
    try {
      localStorage.setItem("aranya-theme", theme);
    } catch {
      // The theme still applies for this page even if storage is unavailable.
    }
  }

  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      const nextTheme =
        document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(nextTheme);
    });
  });

  applyTheme(document.documentElement.dataset.theme === "dark" ? "dark" : "light");
})();

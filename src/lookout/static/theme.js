"use strict";

// Dark is the default. A viewer's choice of light is kept in localStorage and applied
// as data-theme on <html>; the toggle button in the top bar flips it.

(function () {
  const STORE = "lookout.theme";
  const root = document.documentElement;

  function current() {
    return root.dataset.theme === "light" ? "light" : "dark";
  }

  function apply(theme) {
    if (theme === "light") root.dataset.theme = "light";
    else delete root.dataset.theme;
    const button = document.getElementById("theme-toggle");
    if (button) button.setAttribute("aria-label", theme === "light" ? "Switch to dark theme" : "Switch to light theme");
  }

  let saved = null;
  try { saved = localStorage.getItem(STORE); } catch { /* storage unavailable */ }
  apply(saved === "light" ? "light" : "dark");

  function wire() {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    apply(current());
    button.addEventListener("click", () => {
      const next = current() === "light" ? "dark" : "light";
      apply(next);
      try { localStorage.setItem(STORE, next); } catch { /* not remembered */ }
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();

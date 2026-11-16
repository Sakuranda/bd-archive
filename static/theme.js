"use strict";

(() => {
  const key = "bd-archive.theme";
  const colors = new Map([
    ["blue", "#f5f8fe"],
    ["warm", "#f9f8f5"],
    ["dark", "#111925"],
  ]);
  const root = document.documentElement;
  const chrome = document.querySelector('meta[name="theme-color"]');
  let current = "blue";
  let picker;

  function apply(theme) {
    current = colors.has(theme) ? theme : "blue";
    root.dataset.theme = current;
    chrome.content = colors.get(current);
    if (picker) picker.value = current;
  }

  try {
    apply(localStorage.getItem(key));
  } catch {
    apply("blue");
  }

  document.addEventListener("DOMContentLoaded", () => {
    picker = document.getElementById("theme-select");
    picker.value = current;
    picker.addEventListener("change", () => {
      apply(picker.value);
      try {
        localStorage.setItem(key, current);
      } catch {
        // The selected theme still applies for this page.
      }
    });
  });

  window.addEventListener("storage", (event) => {
    if (event.key === key || event.key === null) apply(event.newValue);
  });
})();

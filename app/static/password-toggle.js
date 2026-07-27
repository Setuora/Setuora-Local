(function () {
  function initPasswordField(field) {
    const input = field.querySelector(
      "input[type='password'], input[type='text']",
    );
    const button = field.querySelector("[data-password-toggle]");
    if (!input || !button) return;

    button.addEventListener("click", function () {
      const hidden = input.type === "password";
      input.type = hidden ? "text" : "password";
      button.textContent = hidden ? "Hide" : "Show";
      button.setAttribute("aria-pressed", hidden ? "true" : "false");
    });
  }

  document.querySelectorAll("[data-password-field]").forEach(initPasswordField);
})();

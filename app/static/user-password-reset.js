(function () {
  const dialog = document.getElementById("password-reset-modal");
  const form = dialog && dialog.querySelector("[data-reset-form]");
  const username = dialog && dialog.querySelector("[data-reset-username]");
  if (!dialog || !form || !username) return;

  function openDialog(trigger) {
    form.action =
      "/users/" + encodeURIComponent(trigger.dataset.userId) + "/password";
    username.textContent = trigger.dataset.username;
    form.reset();
    if (dialog.showModal) {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
    const input = form.querySelector("input[name='new_password']");
    if (input) input.focus();
  }

  function closeDialog() {
    if (dialog.close) {
      dialog.close();
    } else {
      dialog.removeAttribute("open");
    }
  }

  document
    .querySelectorAll("[data-reset-password]")
    .forEach(function (trigger) {
      trigger.addEventListener("click", function () {
        openDialog(trigger);
      });
    });
  dialog.querySelectorAll("[data-reset-close]").forEach(function (trigger) {
    trigger.addEventListener("click", closeDialog);
  });
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) closeDialog();
  });
})();

(function () {
  const dialog = document.querySelector("[data-label-pdf-dialog]");
  if (!dialog) return;

  const openDialog = () => {
    if (dialog.open) return;
    if (dialog.showModal) {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
  };

  const closeDialog = () => {
    if (dialog.close) {
      dialog.close();
    } else {
      dialog.removeAttribute("open");
    }
  };

  document.querySelectorAll("[data-label-pdf-open]").forEach((trigger) => {
    trigger.addEventListener("click", (event) => {
      event.preventDefault();
      openDialog();
    });
  });

  dialog.querySelectorAll("[data-label-pdf-close]").forEach((trigger) => {
    trigger.addEventListener("click", closeDialog);
  });

  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) closeDialog();
  });

  const form = dialog.querySelector("form");
  if (form) {
    form.addEventListener("submit", () => window.setTimeout(closeDialog, 80));
  }
})();

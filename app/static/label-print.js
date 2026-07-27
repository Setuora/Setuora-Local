(function () {
  const controls = document.querySelector("[data-label-dimensions]");
  const root = document.documentElement;

  function applyDimension(input) {
    const value = Number.parseFloat(input.value);
    if (!Number.isFinite(value) || value <= 0) return;
    root.style.setProperty(input.dataset.printVar, `${value}mm`);
  }

  if (controls) {
    controls.querySelectorAll("[data-print-var]").forEach((input) => {
      applyDimension(input);
      input.addEventListener("input", () => applyDimension(input));
    });
  }

  const button = document.querySelector("[data-print-once]");
  if (!button) return;

  const originalText = button.textContent;
  const status = document.querySelector("[data-print-status]");

  function setStatus(message, isError) {
    if (!status) return;
    status.textContent = message;
    status.classList.toggle("error-text", Boolean(isError));
  }

  button.addEventListener("click", async () => {
    if (button.disabled) return;

    button.disabled = true;
    button.textContent = "Preparing print";

    const ids = (button.dataset.printIds || "")
      .split(",")
      .map((value) => Number.parseInt(value, 10))
      .filter((value) => Number.isInteger(value));

    try {
      const response = await fetch(button.dataset.printUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({ ids }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        button.textContent = "Print unavailable";
        setStatus(
          payload.error || "Print option is unavailable for these labels.",
          true,
        );
        return;
      }
      button.textContent = "Print used";
      setStatus("Select the printer in the print dialog.", false);
      window.setTimeout(() => window.print(), 80);
    } catch (error) {
      button.disabled = false;
      button.textContent = originalText;
      setStatus(
        "Could not start print. Check the connection and try again.",
        true,
      );
    }
  });
})();

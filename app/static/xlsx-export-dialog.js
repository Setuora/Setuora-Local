(function () {
  const dialog = document.getElementById("xlsx-export-dialog");
  if (!dialog) return;

  const form = dialog.querySelector("[data-xlsx-form]");
  const parameterSection = dialog.querySelector(
    "[data-xlsx-parameter-section]",
  );
  const parameterContainer = dialog.querySelector("[data-xlsx-parameters]");
  const fieldContainer = dialog.querySelector("[data-xlsx-fields]");
  const error = dialog.querySelector("[data-xlsx-error]");
  const title = dialog.querySelector("#xlsx-export-title");
  let activeLink = null;

  const parameterLabels = {
    action: "Action",
    q: "Search",
    start: "From",
    end: "To",
    product_id: "Product ID",
    warehouse: "Warehouse",
    category: "Category",
    brand: "Brand",
    batch: "Batch",
    franchise_level: "Franchise level",
    expiry_period: "Expiry period",
    movement: "Movement",
    voucher_type: "Voucher type",
    voucher_number: "Voucher number",
    party_ledger: "Party ledger",
  };

  function parameterInputType(name) {
    if (name === "start" || name === "end") return "date";
    if (name === "product_id" || name === "voucher_number") return "number";
    return "text";
  }

  function makeParameter(name, value) {
    const label = document.createElement("label");
    label.textContent = parameterLabels[name] || name.replaceAll("_", " ");
    const input = document.createElement("input");
    input.name = name;
    input.type = parameterInputType(name);
    input.value = value;
    if (input.type === "number") input.min = "1";
    if (name === "voucher_type") input.placeholder = "Use configured default";
    label.appendChild(input);
    return label;
  }

  function parseFieldSet(value) {
    return new Set(
      (value || "")
        .split("|")
        .map((field) => field.trim())
        .filter(Boolean),
    );
  }

  function makeField(field, index, deselectedFields, requiredFields) {
    const label = document.createElement("label");
    label.className = "xlsx-export-field";
    const input = document.createElement("input");
    const required = requiredFields.has(field);
    input.type = "checkbox";
    input.name = "xlsx_field";
    input.value = field;
    input.checked = required || !deselectedFields.has(field);
    input.disabled = required;
    input.id = `xlsx-export-field-${index}`;
    const text = document.createElement("span");
    text.textContent = field;
    label.append(input, text);
    return label;
  }

  function openDialog(link) {
    activeLink = link;
    const url = new URL(link.href, window.location.href);
    const parameterNames = (link.dataset.xlsxParameters || "")
      .split("|")
      .map((name) => name.trim())
      .filter(Boolean);
    const fields = (link.dataset.xlsxFields || "")
      .split("|")
      .map((field) => field.trim())
      .filter(Boolean);
    const deselectedFields = parseFieldSet(link.dataset.xlsxDeselectedFields);
    const requiredFields = parseFieldSet(link.dataset.xlsxRequiredFields);
    let defaults = {};
    try {
      defaults = JSON.parse(link.dataset.xlsxDefaults || "{}");
    } catch (_error) {
      defaults = {};
    }

    title.textContent = link.dataset.xlsxTitle || "Customize Excel export";
    parameterContainer.replaceChildren(
      ...parameterNames.map((name) =>
        makeParameter(name, url.searchParams.get(name) || defaults[name] || ""),
      ),
    );
    parameterSection.hidden = parameterNames.length === 0;
    fieldContainer.replaceChildren(
      ...fields.map((field, index) =>
        makeField(field, index, deselectedFields, requiredFields),
      ),
    );
    error.hidden = true;
    if (dialog.showModal) dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  document.querySelectorAll("[data-xlsx-export]").forEach((link) => {
    link.addEventListener("click", (event) => {
      if (
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      )
        return;
      event.preventDefault();
      openDialog(link);
    });
  });

  dialog.querySelectorAll("[data-xlsx-close]").forEach((button) => {
    button.addEventListener("click", () => dialog.close());
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog
    .querySelector("[data-xlsx-select-all]")
    .addEventListener("click", () => {
      fieldContainer.querySelectorAll("input").forEach((input) => {
        input.checked = true;
      });
      error.hidden = true;
    });
  dialog
    .querySelector("[data-xlsx-clear-all]")
    .addEventListener("click", () => {
      fieldContainer.querySelectorAll("input").forEach((input) => {
        input.checked = input.disabled;
      });
    });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!activeLink) return;
    const selectedFields = Array.from(
      fieldContainer.querySelectorAll("input:checked"),
    ).map((input) => input.value);
    if (!selectedFields.length) {
      error.hidden = false;
      return;
    }

    const url = new URL(activeLink.href, window.location.href);
    parameterContainer.querySelectorAll("input").forEach((input) => {
      const value = input.value.trim();
      if (value) url.searchParams.set(input.name, value);
      else url.searchParams.delete(input.name);
    });
    url.searchParams.set("fields", selectedFields.join("|"));
    dialog.close();
    window.location.assign(url.toString());
  });
})();

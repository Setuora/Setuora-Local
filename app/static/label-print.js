(function () {
  const editor = document.querySelector("[data-label-editor]");
  const preview = document.querySelector("[data-label-preview]");
  const source = document.querySelector("[data-label-source]");
  if (!preview || !source) return;

  const printButton = document.querySelector("[data-print-once]");
  const testButton = document.querySelector("[data-test-print]");
  const status = document.querySelector("[data-print-status]");
  const summary = document.querySelector("[data-layout-summary]");
  const pageCount = document.querySelector("[data-preview-page-count]");
  const isAdmin = editor ? editor.dataset.isAdmin === "true" : false;
  const labels = Array.from(source.querySelectorAll(".barcode-label"));
  const pageStyle = document.createElement("style");
  pageStyle.dataset.labelPageStyle = "";
  document.head.appendChild(pageStyle);

  let defaults = {};
  let savedTemplates = [];
  try { defaults = JSON.parse(editor ? editor.dataset.defaultLayout : "{}"); } catch (_error) { defaults = {}; }
  try { savedTemplates = JSON.parse(editor ? editor.dataset.savedTemplates : "[]"); } catch (_error) { savedTemplates = []; }
  let layout = Object.assign({}, defaults);

  const numericFields = new Set([
    "page_width_mm", "page_height_mm", "margin_top_mm", "margin_bottom_mm",
    "margin_left_mm", "margin_right_mm", "horizontal_spacing_mm", "vertical_spacing_mm",
    "label_width_mm", "label_height_mm", "qr_size_mm", "rows", "columns",
    "start_position", "scale_percent",
  ]);
  const integerFields = new Set(["rows", "columns", "start_position"]);
  const fields = editor ? Array.from(editor.querySelectorAll("[data-layout-field]")) : [];

  function setStatus(message, isError) {
    if (!status) return;
    status.textContent = message;
    status.classList.toggle("error-text", Boolean(isError));
  }

  function displayPageSize() {
    const width = Number(layout.page_width_mm);
    const height = Number(layout.page_height_mm);
    return layout.orientation === "landscape" ? [height, width] : [width, height];
  }

  function setPagePreset(pageSize) {
    if (pageSize === "A4") {
      layout.page_width_mm = 210;
      layout.page_height_mm = 297;
    } else if (pageSize === "Letter") {
      layout.page_width_mm = 215.9;
      layout.page_height_mm = 279.4;
    }
  }

  function syncFields() {
    fields.forEach((input) => {
      const key = input.dataset.layoutField;
      input.value = layout[key] == null ? "" : layout[key];
      if (key === "page_width_mm" || key === "page_height_mm") {
        input.disabled = layout.page_size !== "Custom";
      }
      if (key === "start_position") input.max = String(Math.max(1, Number(layout.rows) * Number(layout.columns)));
    });
  }

  function readFields() {
    fields.forEach((input) => {
      const key = input.dataset.layoutField;
      if (numericFields.has(key)) {
        const value = integerFields.has(key) ? Number.parseInt(input.value, 10) : Number.parseFloat(input.value);
        if (Number.isFinite(value)) layout[key] = value;
      } else {
        layout[key] = input.value;
      }
    });
  }

  function validate() {
    const rows = Number(layout.rows);
    const columns = Number(layout.columns);
    const scale = Number(layout.scale_percent) / 100;
    const [pageWidth, pageHeight] = displayPageSize();
    const usedWidth = Number(layout.margin_left_mm) + Number(layout.margin_right_mm)
      + scale * (columns * Number(layout.label_width_mm) + (columns - 1) * Number(layout.horizontal_spacing_mm));
    const usedHeight = Number(layout.margin_top_mm) + Number(layout.margin_bottom_mm)
      + scale * (rows * Number(layout.label_height_mm) + (rows - 1) * Number(layout.vertical_spacing_mm));
    const capacity = rows * columns;
    let error = "";
    if (!Number.isFinite(capacity) || rows < 1 || columns < 1) error = "Rows and columns must be at least 1.";
    else if (Number(layout.start_position) < 1 || Number(layout.start_position) > capacity) error = `Starting label must be between 1 and ${capacity}.`;
    else if (Number(layout.qr_size_mm) > Math.min(Number(layout.label_width_mm), Number(layout.label_height_mm))) error = "QR size must fit inside the label.";
    else if (usedWidth > pageWidth + 0.01 || usedHeight > pageHeight + 0.01) {
      error = `Layout needs ${usedWidth.toFixed(1)} × ${usedHeight.toFixed(1)} mm; page is ${pageWidth.toFixed(1)} × ${pageHeight.toFixed(1)} mm.`;
    }
    if (summary) {
      summary.textContent = error || `${capacity} slots per sheet · content uses ${usedWidth.toFixed(1)} × ${usedHeight.toFixed(1)} mm of ${pageWidth.toFixed(1)} × ${pageHeight.toFixed(1)} mm.`;
      summary.classList.toggle("error-text", Boolean(error));
    }
    if (printButton && !printButton.dataset.policyDisabled) printButton.disabled = Boolean(error) || labels.length === 0;
    if (testButton) testButton.disabled = Boolean(error);
    return !error;
  }

  function applyVariables() {
    const [pageWidth, pageHeight] = displayPageSize();
    const scale = Number(layout.scale_percent) / 100;
    const root = document.documentElement;
    root.style.setProperty("--label-page-width", `${pageWidth}mm`);
    root.style.setProperty("--label-page-height", `${pageHeight}mm`);
    root.style.setProperty("--label-grid-top", `${Number(layout.margin_top_mm)}mm`);
    root.style.setProperty("--label-grid-left", `${Number(layout.margin_left_mm)}mm`);
    root.style.setProperty("--print-label-width", `${Number(layout.label_width_mm) * scale}mm`);
    root.style.setProperty("--print-label-height", `${Number(layout.label_height_mm) * scale}mm`);
    root.style.setProperty("--print-qr-size", `${Number(layout.qr_size_mm) * scale}mm`);
    root.style.setProperty("--label-column-gap", `${Number(layout.horizontal_spacing_mm) * scale}mm`);
    root.style.setProperty("--label-row-gap", `${Number(layout.vertical_spacing_mm) * scale}mm`);
    root.style.setProperty("--label-columns", String(layout.columns));
    pageStyle.textContent = `@page { size: ${pageWidth}mm ${pageHeight}mm; margin: 0; }`;
  }

  function makeSlot(slotNumber, kind, label) {
    const slot = document.createElement("div");
    slot.className = `label-slot label-slot--${kind}`;
    slot.dataset.slot = String(slotNumber);
    if (label) slot.appendChild(label.cloneNode(true));
    const number = document.createElement("span");
    number.className = "label-slot-number print-hide";
    number.textContent = String(slotNumber);
    slot.appendChild(number);
    return slot;
  }

  function renderPreview() {
    applyVariables();
    preview.replaceChildren();
    const rows = Math.max(1, Number(layout.rows));
    const columns = Math.max(1, Number(layout.columns));
    const capacity = rows * columns;
    const firstPosition = Math.min(capacity, Math.max(1, Number(layout.start_position)));
    let labelIndex = 0;
    let pageIndex = 0;
    do {
      const page = document.createElement("article");
      page.className = "label-page";
      const grid = document.createElement("div");
      grid.className = "label-grid";
      const pageStart = pageIndex === 0 ? firstPosition : 1;
      for (let position = 1; position <= capacity; position += 1) {
        if (position < pageStart) grid.appendChild(makeSlot(position, "used"));
        else if (labelIndex < labels.length) grid.appendChild(makeSlot(position, "filled", labels[labelIndex++]));
        else grid.appendChild(makeSlot(position, "empty"));
      }
      page.appendChild(grid);
      preview.appendChild(page);
      pageIndex += 1;
    } while (labelIndex < labels.length);
    if (pageCount) pageCount.textContent = `${pageIndex} page${pageIndex === 1 ? "" : "s"}`;
    renderSlotPicker();
    validate();
  }

  function renderSlotPicker() {
    if (!editor) return;
    const picker = editor.querySelector("[data-start-slot-picker]");
    if (!picker) return;
    picker.replaceChildren();
    picker.style.setProperty("--slot-picker-columns", String(layout.columns));
    const capacity = Number(layout.rows) * Number(layout.columns);
    for (let position = 1; position <= capacity; position += 1) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = String(position);
      button.className = position < Number(layout.start_position) ? "is-used" : "";
      if (position === Number(layout.start_position)) button.classList.add("is-start");
      button.setAttribute("aria-label", `Start at label ${position}`);
      button.addEventListener("click", () => {
        layout.start_position = position;
        syncFields();
        renderPreview();
      });
      picker.appendChild(button);
    }
  }

  fields.forEach((input) => {
    input.addEventListener("input", () => {
      const previousPageSize = layout.page_size;
      readFields();
      if (input.dataset.layoutField === "page_size" && layout.page_size !== previousPageSize) setPagePreset(layout.page_size);
      const capacity = Math.max(1, Number(layout.rows) * Number(layout.columns));
      layout.start_position = Math.min(capacity, Math.max(1, Number(layout.start_position)));
      syncFields();
      renderPreview();
    });
  });

  const templateSelect = editor ? editor.querySelector("[data-template-select]") : null;
  const templateName = editor ? editor.querySelector("[data-template-name]") : null;
  const templateDelete = editor ? editor.querySelector("[data-template-delete]") : null;
  if (templateSelect) {
    templateSelect.addEventListener("change", () => {
      const selected = savedTemplates.find((item) => String(item.id) === templateSelect.value);
      if (templateDelete) templateDelete.disabled = !selected;
      if (!selected) return;
      layout = Object.assign({}, defaults, selected.settings);
      if (templateName) templateName.value = selected.name;
      syncFields();
      renderPreview();
      setStatus(`Loaded “${selected.name}”.`, false);
    });
  }

  const saveTemplate = editor ? editor.querySelector("[data-template-save]") : null;
  if (saveTemplate) saveTemplate.addEventListener("click", async () => {
    if (!validate()) return;
    const name = templateName ? templateName.value.trim() : "";
    if (!name) { setStatus("Enter a template name.", true); if (templateName) templateName.focus(); return; }
    saveTemplate.disabled = true;
    try {
      const response = await fetch(editor.dataset.templateUrl, {
        method: "POST", headers: {"Content-Type": "application/json", Accept: "application/json"},
        body: JSON.stringify({name, layout}),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || "Could not save template");
      const index = savedTemplates.findIndex((item) => item.id === payload.template.id || item.name === payload.template.name);
      if (index >= 0) savedTemplates[index] = payload.template; else savedTemplates.push(payload.template);
      savedTemplates.sort((a, b) => a.name.localeCompare(b.name));
      let option = Array.from(templateSelect.options).find((item) => item.value === String(payload.template.id));
      if (!option) { option = document.createElement("option"); templateSelect.appendChild(option); }
      option.value = String(payload.template.id); option.textContent = payload.template.name;
      templateSelect.value = option.value;
      if (templateDelete) templateDelete.disabled = false;
      setStatus(`Saved template “${payload.template.name}”.`, false);
    } catch (error) { setStatus(error.message || "Could not save template.", true); }
    finally { saveTemplate.disabled = false; }
  });

  if (templateDelete) templateDelete.addEventListener("click", async () => {
    const selected = savedTemplates.find((item) => String(item.id) === templateSelect.value);
    if (!selected || !window.confirm(`Delete template “${selected.name}”?`)) return;
    const response = await fetch(`${editor.dataset.templateUrl}/${selected.id}`, {method: "DELETE", headers: {Accept: "application/json"}});
    if (!response.ok) { setStatus("Could not delete template.", true); return; }
    savedTemplates = savedTemplates.filter((item) => item.id !== selected.id);
    templateSelect.querySelector(`option[value="${selected.id}"]`).remove();
    templateSelect.value = ""; templateDelete.disabled = true;
    setStatus(`Deleted template “${selected.name}”.`, false);
  });

  function beginBrowserPrint(testMode) {
    document.body.dataset.labelPrintMode = testMode ? "test" : "labels";
    const cleanup = () => { delete document.body.dataset.labelPrintMode; window.removeEventListener("afterprint", cleanup); };
    window.addEventListener("afterprint", cleanup);
    window.setTimeout(() => window.print(), 80);
  }

  if (testButton) testButton.addEventListener("click", () => {
    if (!validate()) return;
    setStatus("Test sheet contains slot outlines only; no QR codes or print history will be created.", false);
    beginBrowserPrint(true);
  });

  if (printButton) {
    if (printButton.disabled) printButton.dataset.policyDisabled = "true";
    const originalText = printButton.textContent;
    printButton.addEventListener("click", async () => {
      if (printButton.disabled || !validate()) return;
      const copiesInput = editor ? editor.querySelector("[data-print-copies]") : null;
      const reasonInput = editor ? editor.querySelector("[data-print-reason]") : null;
      const copies = Number.parseInt(copiesInput ? copiesInput.value : "1", 10);
      const reason = reasonInput ? reasonInput.value.trim() : "";
      if (isAdmin && copies > 1 && !reason) { setStatus("Enter a reason when printing multiple copies.", true); reasonInput.focus(); return; }
      printButton.disabled = true;
      printButton.textContent = "Preparing print";
      const ids = (printButton.dataset.printIds || "").split(",").map((value) => Number.parseInt(value, 10)).filter(Number.isInteger);
      const selected = savedTemplates.find((item) => templateSelect && String(item.id) === templateSelect.value);
      try {
        const response = await fetch(printButton.dataset.printUrl, {
          method: "POST", headers: {"Content-Type": "application/json", Accept: "application/json"},
          body: JSON.stringify({ids, copies, reason, template_name: selected ? selected.name : "", layout}),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.error || "Print option is unavailable for these labels.");
        printButton.textContent = isAdmin ? "Admin reprint" : "Print used";
        if (!isAdmin) { printButton.dataset.policyDisabled = "true"; printButton.disabled = true; }
        else printButton.disabled = false;
        setStatus(`In the printer dialog, choose ${copies} ${copies === 1 ? "copy" : "copies"}, 100% / Actual size, and no headers or footers.`, false);
        beginBrowserPrint(false);
      } catch (error) {
        printButton.disabled = false;
        printButton.textContent = originalText;
        setStatus(error.message || "Could not start print. Check the connection and try again.", true);
      }
    });
  }

  syncFields();
  renderPreview();
})();

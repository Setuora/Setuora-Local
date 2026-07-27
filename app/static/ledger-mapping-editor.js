(function () {
  function parseRows(value) {
    return (value || "")
      .split(/\r?\n/)
      .map(function (line) {
        const parts = line.split("|").map(function (part) {
          return part.trim();
        });
        while (parts.length < 5) parts.push("");
        return parts.slice(0, 5);
      })
      .filter(function (parts) {
        return parts.some(Boolean);
      });
  }

  function rowField(label, field, value, options) {
    const wrapper = document.createElement("label");
    wrapper.className = "ledger-mapping-field ledger-mapping-field--" + field;
    wrapper.textContent = label;
    const input = document.createElement("input");
    input.type = options.type || "text";
    input.value = value || "";
    input.placeholder = options.placeholder || "";
    input.required = true;
    input.setAttribute("data-ledger-field", field);
    if (options.inputMode) input.inputMode = options.inputMode;
    if (options.min !== undefined) input.min = options.min;
    if (options.max !== undefined) input.max = options.max;
    if (options.step !== undefined) input.step = options.step;
    if (options.listId) input.setAttribute("list", options.listId);
    wrapper.appendChild(input);
    return wrapper;
  }

  function initEditor(editor) {
    const hidden = editor.querySelector(
      'input[name="sales_gst_ledger_mappings"]',
    );
    const list = editor.querySelector("[data-ledger-mapping-list]");
    const empty = editor.querySelector("[data-ledger-mapping-empty]");
    const summary = editor.querySelector("[data-ledger-summary]");
    const addButton = editor.querySelector("[data-add-ledger-mapping]");

    function rows() {
      return Array.from(list.querySelectorAll("[data-ledger-mapping-row]"));
    }

    function updateState() {
      const count = rows().length;
      empty.hidden = count > 0;
      summary.textContent =
        count > 0
          ? "Product GST ledgers (" + count + ")"
          : "Add product ledger";
    }

    function serialize() {
      hidden.value = rows()
        .map(function (row) {
          return ["rate", "sales", "cgst", "sgst", "igst"]
            .map(function (field) {
              return row
                .querySelector('[data-ledger-field="' + field + '"]')
                .value.trim();
            })
            .join(" | ");
        })
        .join("\n");
      updateState();
      hidden.dispatchEvent(new Event("input", { bubbles: true }));
    }

    function addRow(values, focusNewRow) {
      const row = document.createElement("section");
      row.className = "ledger-mapping-row";
      row.setAttribute("data-ledger-mapping-row", "");

      const header = document.createElement("div");
      header.className = "ledger-mapping-row__head";
      const title = document.createElement("strong");
      title.textContent = "Product ledger";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "button small ghost";
      remove.textContent = "Remove";
      remove.addEventListener("click", function () {
        row.remove();
        serialize();
      });
      header.append(title, remove);

      const grid = document.createElement("div");
      grid.className = "ledger-mapping-grid";
      const ledgerListId = editor.dataset.ledgerListId || "";
      grid.append(
        rowField("GST rate %", "rate", values[0], {
          type: "number",
          inputMode: "decimal",
          min: "0",
          max: "100",
          step: "0.01",
          placeholder: "e.g. 5",
        }),
        rowField("Sales ledger", "sales", values[1], {
          placeholder: "e.g. Sales @ 5%",
          listId: ledgerListId,
        }),
        rowField("CGST ledger", "cgst", values[2], {
          placeholder: "e.g. Output CGST @ 2.5%",
          listId: ledgerListId,
        }),
        rowField("SGST ledger", "sgst", values[3], {
          placeholder: "e.g. Output SGST @ 2.5%",
          listId: ledgerListId,
        }),
        rowField("IGST ledger", "igst", values[4], {
          placeholder: "e.g. Output IGST @ 5%",
          listId: ledgerListId,
        }),
      );
      grid.querySelectorAll("input").forEach(function (input) {
        input.addEventListener("input", serialize);
        input.addEventListener("change", serialize);
      });

      row.append(header, grid);
      list.appendChild(row);
      updateState();
      if (focusNewRow) grid.querySelector("input").focus();
    }

    parseRows(hidden.value).forEach(function (values) {
      addRow(values, false);
    });
    updateState();
    addButton.addEventListener("click", function () {
      editor.open = true;
      addRow(["", "", "", "", ""], true);
    });
  }

  document.querySelectorAll("[data-ledger-mapping-editor]").forEach(initEditor);
})();

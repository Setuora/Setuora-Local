const workflow = document.querySelector(".warehouse-workflow");
const locationRows = JSON.parse(workflow?.dataset.locations || "[]");
const locations = locationRows.map((row) => ({
  id: row[0],
  code: row[1],
  warehouse: row[2],
  zone: row[3],
  section: row[4],
  rack: row[5],
  shelf: row[6],
  bin: row[7],
  path: row[8],
}));

const levels = ["warehouse", "zone", "section", "rack", "shelf", "bin"];
const selectors = Object.fromEntries(
  levels.map((level) => [
    level,
    document.querySelector(`[data-location-level="${level}"]`),
  ]),
);
const basket = new Map();
const moveItemsBody = document.getElementById("move-items");
const resultBox = document.getElementById("search-results");
const alertBox = document.getElementById("relocation-alert");
const destinationId = document.getElementById("destination-id");
const destinationSummary = document.getElementById("destination-summary");
const confirmButton = document.getElementById("confirm-relocation");
const moveSummary = document.getElementById("move-summary");
const searchInput = document.getElementById("stock-search");

function showAlert(message, kind = "error") {
  alertBox.textContent = message;
  alertBox.className = `alert ${kind}`;
  alertBox.hidden = false;
  alertBox.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function clearAlert() {
  alertBox.hidden = true;
  alertBox.textContent = "";
}

function cell(text, tag = "td") {
  const node = document.createElement(tag);
  node.textContent = text ?? "-";
  return node;
}

function itemKey(item) {
  if (item.serial_id) return `serial:${item.serial_id}`;
  return [
    item.product_id,
    item.batch_number || "",
    item.source_location_id || "",
    item.legacy_warehouse || "",
  ].join("|");
}

function renderSearchResults(results) {
  resultBox.replaceChildren();
  if (!results.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No movable stock matched that search.";
    resultBox.append(empty);
    return;
  }
  const wrap = document.createElement("div");
  wrap.className = "table-scroll";
  const table = document.createElement("table");
  const head = document.createElement("thead");
  head.innerHTML =
    "<tr><th>Product</th><th>Batch</th><th>Expiry</th><th>Current location</th><th>Available</th><th></th></tr>";
  const body = document.createElement("tbody");
  results.forEach((item) => {
    const row = document.createElement("tr");
    const product = cell(item.product_name);
    const sub = document.createElement("small");
    sub.textContent = item.serial_number || item.product_code;
    product.append(sub);
    row.append(product);
    row.append(cell(item.batch_number || "No batch"));
    row.append(cell(item.expiry_date || "-"));
    row.append(cell(item.source_location));
    row.append(cell(String(item.quantity)));
    const action = document.createElement("td");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button small";
    button.textContent = basket.has(itemKey(item)) ? "Added" : "Add";
    button.disabled = basket.has(itemKey(item));
    button.addEventListener("click", () => {
      basket.set(itemKey(item), {
        ...item,
        move_quantity: item.quantity,
        move_all: true,
      });
      renderBasket();
      button.textContent = "Added";
      button.disabled = true;
    });
    action.append(button);
    row.append(action);
    body.append(row);
  });
  table.append(head, body);
  wrap.append(table);
  resultBox.append(wrap);
}

function renderBasket() {
  moveItemsBody.replaceChildren();
  if (!basket.size) {
    const row = document.createElement("tr");
    const empty = cell("No stock added yet");
    empty.colSpan = 6;
    empty.className = "empty";
    row.append(empty);
    moveItemsBody.append(row);
    updateConfirmation();
    return;
  }
  basket.forEach((item, key) => {
    const row = document.createElement("tr");
    const product = cell(item.product_name);
    const sub = document.createElement("small");
    sub.textContent = item.serial_number || item.product_code;
    product.append(sub);
    row.append(
      product,
      cell(item.batch_number || "No batch"),
      cell(item.source_location),
      cell(String(item.quantity)),
    );

    const quantityCell = document.createElement("td");
    const controls = document.createElement("div");
    controls.className = "move-quantity";
    const input = document.createElement("input");
    input.type = "number";
    input.min = "1";
    input.max = String(item.quantity);
    input.value = String(item.move_quantity);
    input.disabled = item.move_all;
    input.setAttribute(
      "aria-label",
      `Quantity of ${item.product_name} to move`,
    );
    input.addEventListener("input", () => {
      item.move_quantity = Math.max(
        1,
        Math.min(item.quantity, Number(input.value) || 1),
      );
      updateConfirmation();
    });
    const allLabel = document.createElement("label");
    allLabel.className = "checkline";
    const all = document.createElement("input");
    all.type = "checkbox";
    all.checked = item.move_all;
    all.addEventListener("change", () => {
      item.move_all = all.checked;
      input.disabled = all.checked;
      if (all.checked) {
        item.move_quantity = item.quantity;
        input.value = String(item.quantity);
      }
      updateConfirmation();
    });
    allLabel.append(all, document.createTextNode("Move all"));
    controls.append(input, allLabel);
    quantityCell.append(controls);
    row.append(quantityCell);

    const action = document.createElement("td");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "button small ghost";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => {
      basket.delete(key);
      renderBasket();
    });
    action.append(remove);
    row.append(action);
    moveItemsBody.append(row);
  });
  updateConfirmation();
}

document
  .getElementById("stock-search-form")
  ?.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearAlert();
    const query = searchInput.value.trim();
    if (!query) return;
    resultBox.innerHTML = '<p class="empty">Searching...</p>';
    try {
      const response = await fetch(
        `/warehouse/api/search?q=${encodeURIComponent(query)}`,
        {
          headers: { Accept: "application/json" },
        },
      );
      const payload = await response.json();
      if (!response.ok || !payload.ok)
        throw new Error(payload.error || "Search failed");
      renderSearchResults(payload.results);
      if (payload.results.length === 1 && payload.results[0].serial_id) {
        const item = payload.results[0];
        const key = itemKey(item);
        if (!basket.has(key))
          basket.set(key, { ...item, move_quantity: 1, move_all: true });
        renderBasket();
      }
    } catch (error) {
      resultBox.replaceChildren();
      showAlert(error.message || "Could not search stock");
    }
  });

function selectedFilters(untilIndex) {
  const values = {};
  for (let index = 0; index < untilIndex; index += 1) {
    values[levels[index]] = selectors[levels[index]].value;
  }
  return values;
}

function matchingLocations(filters) {
  return locations.filter((location) =>
    Object.entries(filters).every(([key, value]) => location[key] === value),
  );
}

function fillLevel(index) {
  const level = levels[index];
  const select = selectors[level];
  const filters = selectedFilters(index);
  const options = [
    ...new Set(matchingLocations(filters).map((location) => location[level])),
  ].sort();
  select.replaceChildren(new Option(`Select ${level}`, ""));
  options.forEach((value) => select.add(new Option(value, value)));
  select.disabled = !options.length;
}

function resetAfter(index) {
  for (let next = index + 1; next < levels.length; next += 1) {
    const select = selectors[levels[next]];
    select.replaceChildren(new Option(`Select ${levels[next]}`, ""));
    select.disabled = true;
  }
  destinationId.value = "";
  destinationSummary.textContent = "No destination selected";
}

levels.forEach((level, index) => {
  selectors[level]?.addEventListener("change", () => {
    resetAfter(index);
    if (!selectors[level].value) {
      updateConfirmation();
      return;
    }
    if (index < levels.length - 1) {
      fillLevel(index + 1);
    } else {
      const match = matchingLocations(selectedFilters(levels.length))[0];
      if (match) selectDestination(match);
    }
    updateConfirmation();
  });
});
if (locations.length) fillLevel(0);

function selectDestination(location) {
  levels.forEach((level, index) => {
    if (index > 0) fillLevel(index);
    selectors[level].value = location[level];
  });
  destinationId.value = String(location.id);
  document.getElementById("destination-code").value = location.code;
  destinationSummary.textContent = `${location.code} · ${location.path}`;
  destinationSummary.classList.add("is-selected");
  updateConfirmation();
}

async function useDestinationCode() {
  clearAlert();
  const code = document.getElementById("destination-code").value.trim();
  if (!code) return;
  try {
    const response = await fetch(
      `/warehouse/api/location?q=${encodeURIComponent(code)}`,
      {
        headers: { Accept: "application/json" },
      },
    );
    const payload = await response.json();
    if (!response.ok || !payload.ok)
      throw new Error(payload.error || "Location not found");
    selectDestination(payload.location);
  } catch (error) {
    destinationId.value = "";
    destinationSummary.textContent = "No destination selected";
    updateConfirmation();
    showAlert(error.message || "Location code is invalid or inactive");
  }
}
document
  .getElementById("destination-lookup")
  ?.addEventListener("click", useDestinationCode);
document
  .getElementById("destination-code")
  ?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      useDestinationCode();
    }
  });

function updateConfirmation() {
  const quantity = [...basket.values()].reduce(
    (total, item) => total + Number(item.move_quantity || 0),
    0,
  );
  const ready = quantity > 0 && Boolean(destinationId.value);
  confirmButton.disabled = !ready;
  moveSummary.textContent = ready
    ? `${quantity} unit${quantity === 1 ? "" : "s"} across ${basket.size} line${basket.size === 1 ? "" : "s"} will move immediately.`
    : "Add stock and choose a destination.";
}

confirmButton?.addEventListener("click", async () => {
  clearAlert();
  confirmButton.disabled = true;
  confirmButton.textContent = "Moving...";
  const items = [...basket.values()].map((item) => ({
    product_id: item.product_id,
    batch_number: item.batch_number,
    source_location_id: item.source_location_id,
    legacy_warehouse: item.legacy_warehouse,
    serial_id: item.serial_id,
    quantity: Number(item.move_quantity),
  }));
  try {
    const response = await fetch("/warehouse/relocate", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify({
        destination_id: Number(destinationId.value),
        reason: document.getElementById("move-reason").value,
        items,
      }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok)
      throw new Error(payload.error || "Relocation failed");
    basket.clear();
    renderBasket();
    resultBox.innerHTML =
      '<p class="empty">Move complete. Search or scan the next product.</p>';
    searchInput.value = "";
    document.getElementById("move-reason").value = "";
    showAlert(
      `${payload.message}. ${payload.references.join(", ")}`,
      "success",
    );
  } catch (error) {
    showAlert(error.message || "The relocation could not be completed");
  } finally {
    confirmButton.textContent = "Confirm relocation";
    updateConfirmation();
  }
});

const scannerDialog = document.getElementById("warehouse-scanner");
const scannerVideo = document.getElementById("warehouse-scanner-video");
const scannerStatus = document.getElementById("warehouse-scanner-status");
let scanTarget = "product";
let scanStream = null;
let scannerReader = null;
let scannerActive = false;

async function stopScanner() {
  scannerActive = false;
  if (scannerReader && typeof scannerReader.reset === "function")
    scannerReader.reset();
  scannerReader = null;
  if (scanStream) scanStream.getTracks().forEach((track) => track.stop());
  scanStream = null;
  scannerVideo.srcObject = null;
}

async function handleScan(value) {
  const clean = String(value || "").trim();
  if (!clean || !scannerActive) return;
  await stopScanner();
  scannerDialog.close();
  if (scanTarget === "destination") {
    document.getElementById("destination-code").value = clean;
    await useDestinationCode();
  } else {
    searchInput.value = clean;
    document.getElementById("stock-search-form").requestSubmit();
  }
}

async function startNativeScanner() {
  scanStream = await navigator.mediaDevices.getUserMedia({
    video: { facingMode: { ideal: "environment" } },
    audio: false,
  });
  scannerVideo.srcObject = scanStream;
  await scannerVideo.play();
  const formats = await BarcodeDetector.getSupportedFormats();
  const detector = new BarcodeDetector({
    formats: formats.filter((format) =>
      ["qr_code", "code_128", "ean_13"].includes(format),
    ),
  });
  const detect = async () => {
    if (!scannerActive) return;
    try {
      const codes = await detector.detect(scannerVideo);
      if (codes.length) return handleScan(codes[0].rawValue);
    } catch (_error) {
      scannerStatus.textContent = "Keep the code centered and steady.";
    }
    requestAnimationFrame(detect);
  };
  requestAnimationFrame(detect);
}

async function startZxingScanner() {
  if (!window.ZXing?.BrowserMultiFormatReader)
    throw new Error("Camera scanning is not supported in this browser");
  scannerReader = new window.ZXing.BrowserMultiFormatReader();
  scannerReader.decodeFromVideoDevice(undefined, scannerVideo, (result) => {
    if (result)
      handleScan(
        typeof result.getText === "function" ? result.getText() : result.text,
      );
  });
}

async function openScanner(target) {
  scanTarget = target;
  scannerActive = true;
  scannerDialog.showModal();
  scannerStatus.textContent = "Starting camera...";
  document.getElementById("warehouse-scanner-help").textContent =
    target === "destination"
      ? "Scan the destination location QR code."
      : "Scan a product QR code or barcode.";
  try {
    if ("BarcodeDetector" in window) await startNativeScanner();
    else await startZxingScanner();
    scannerStatus.textContent = "Scanning...";
  } catch (error) {
    scannerStatus.textContent = error.message || "Camera could not start";
    await stopScanner();
  }
}

document.querySelectorAll("[data-open-scanner]").forEach((button) => {
  button.addEventListener("click", () =>
    openScanner(button.dataset.openScanner),
  );
});
document
  .getElementById("warehouse-scanner-close")
  ?.addEventListener("click", async () => {
    await stopScanner();
    scannerDialog.close();
  });
scannerDialog?.addEventListener("close", stopScanner);

renderBasket();

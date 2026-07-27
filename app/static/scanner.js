const form = document.getElementById("scan-form");
const input = document.getElementById("serial-input");
const sourceInput = document.getElementById("scan-source");
const video = document.getElementById("scanner-video");
const scanStatusLabel = document.getElementById("scan-status-label");
const scannerTools = document.getElementById("scanner-tools");
const scannerBox = document.getElementById("scanner-box");
const scanLine = document.querySelector(".scan-line");
const cameraButton = document.getElementById("camera-button");
const scanCountLabel = document.getElementById("scan-count");
const submitBatchButton = document.getElementById("submit-batch-button");
const shelfVerificationStatus = document.getElementById(
  "shelf-verification-status",
);
const scanModeInput = document.getElementById("scan-mode");
const saleModeButtons = document.querySelectorAll("[data-scan-mode-button]");
const saleReturnStatus = document.getElementById("sale-return-status");
const voucherPreviewTable = document.getElementById("voucher-preview-table");
const voucherPreviewBody = document.getElementById("voucher-preview-body");
const voucherPreviewFoot = document.getElementById("voucher-preview-foot");
const voucherLineCount = document.getElementById("voucher-line-count");
const scannedSerialsTable = document.getElementById("scanned-serials-table");
const scannedSerialsBody = document.getElementById("scanned-serials-body");

const canManual = form && form.dataset.canManual === "true";
const canEditVoucher = voucherPreviewTable?.dataset.canEdit === "true";
const canEditScans = scannedSerialsTable?.dataset.canEdit === "true";
const batchId =
  voucherPreviewTable?.dataset.batchId ||
  scannedSerialsTable?.dataset.batchId ||
  "";
const batchType = scannedSerialsTable?.dataset.batchType || "";

let nativeDetector = null;
let activeStream = null;
let scanning = false;
let lastCode = "";
let submitting = false;
let scanTimer = null;
let inputSubmitTimer = null;
let cameraStarting = false;
let toastContainer = null;
let scanCount = Number(scanCountLabel?.dataset.count || 0);
let saleReturnPending = Number(saleReturnStatus?.dataset.pending || 0);
const MAX_DESKTOP_TOASTS = 3;
const MAX_MOBILE_TOASTS = 2;

function ensureToastContainer() {
  if (toastContainer) return toastContainer;
  toastContainer = document.createElement("div");
  toastContainer.id = "scan-toast-container";
  document.body.appendChild(toastContainer);
  return toastContainer;
}

function showToast(message, kind = "info", duration = 3500) {
  const container = ensureToastContainer();
  pruneToasts(container, kind);
  const toast = document.createElement("div");
  toast.className = `scan-toast ${kind}`;
  toast.dataset.kind = kind;
  toast.textContent = message;
  container.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add("visible"));
  if (kind === "success") playBeep(800, 120);
  if (kind === "error") playBeep(300, 200);
  if (kind === "warn") playBeep(500, 150);
  if (duration > 0) setTimeout(() => dismissToast(toast), duration);
}

function maxVisibleToasts() {
  return window.matchMedia("(max-width: 640px)").matches
    ? MAX_MOBILE_TOASTS
    : MAX_DESKTOP_TOASTS;
}

function pruneToasts(container, kind) {
  const activeToasts = Array.from(container.querySelectorAll(".scan-toast"));
  if (kind === "success") {
    activeToasts
      .filter((toast) => toast.dataset.kind === "success")
      .forEach((toast) => toast.remove());
  }

  const remainingToasts = Array.from(container.querySelectorAll(".scan-toast"));
  while (remainingToasts.length >= maxVisibleToasts()) {
    remainingToasts.shift().remove();
  }
}

function dismissToast(toast) {
  if (!toast.parentNode) return;
  toast.classList.remove("visible");
  toast.classList.add("exit");
  toast.addEventListener("animationend", () => toast.remove(), { once: true });
  setTimeout(() => {
    if (toast.parentNode) toast.remove();
  }, 500);
}

function playBeep(freq, durationMs) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.frequency.value = freq;
    osc.type = "sine";
    gain.gain.value = 0.15;
    gain.gain.exponentialRampToValueAtTime(
      0.001,
      ctx.currentTime + durationMs / 1000,
    );
    osc.start();
    osc.stop(ctx.currentTime + durationMs / 1000);
  } catch (e) {
    return;
  }
}

function setScanStatus(text) {
  if (scanStatusLabel) scanStatusLabel.textContent = text;
}

function syncSubmitButton() {
  if (!submitBatchButton) return;
  const shelfPending = Number(shelfVerificationStatus?.dataset.pending || 0);
  submitBatchButton.disabled =
    scanCount === 0 || shelfPending > 0 || saleReturnPending > 0;
}

function setScanCount(value) {
  scanCount = Number(value || 0);
  if (scanCountLabel) {
    scanCountLabel.dataset.count = String(scanCount);
    scanCountLabel.textContent = `${scanCount} scanned`;
  }
  syncSubmitButton();
}

function updateScanCount() {
  setScanCount(scanCount + 1);
}

function updateShelfState(payload) {
  const pending = Number(payload?.pending_count || 0);
  const required = payload?.shelf_required === true;
  if (shelfVerificationStatus)
    shelfVerificationStatus.dataset.pending = String(pending);
  syncSubmitButton();
  if (!shelfVerificationStatus) return;
  shelfVerificationStatus.classList.toggle("warn", pending > 0);
  if (pending > 0) {
    const suffix = required
      ? " Scan the shelf QR now—more product scans are blocked."
      : " Scan the shelf QR before submitting.";
    shelfVerificationStatus.textContent = `${pending} product${pending === 1 ? "" : "s"} awaiting shelf verification.${suffix}`;
  } else if (payload?.scan_type === "shelf") {
    shelfVerificationStatus.textContent = `Shelf verified: ${payload.location || payload.location_code || "location recorded"}.`;
  } else {
    shelfVerificationStatus.textContent =
      "Shelf placement is verified. Products with a configured interval will require a shelf QR scan.";
  }
}

function setSaleScanMode(mode, options = {}) {
  if (!scanModeInput) return;
  let nextMode = mode === "return" ? "return" : "sale";
  if (nextMode === "sale" && saleReturnPending > 0) {
    nextMode = "return";
    if (!options.silent)
      showToast("Scan the shelf QR before continuing the sale", "warn", 3500);
  }
  scanModeInput.value = nextMode;
  saleModeButtons.forEach((button) => {
    const active = button.dataset.scanModeButton === nextMode;
    button.setAttribute("aria-pressed", active ? "true" : "false");
    button.classList.toggle("primary", active && nextMode === "sale");
    button.classList.toggle("danger", active && nextMode === "return");
  });
  lastCode = "";
  if (options.status !== false) {
    if (saleReturnPending > 0) {
      setScanStatus(
        scanning
          ? "Return shelf pending - scan shelf QR"
          : "Return shelf pending",
      );
    } else if (nextMode === "return") {
      setScanStatus(
        scanning ? "Return mode - scan product QR" : "Return mode ready",
      );
    } else if (!scanning) {
      setScanStatus("Scanning stopped");
    }
  }
}

function updateSaleReturnState(state) {
  if (!saleReturnStatus || !state) return;
  saleReturnPending = Number(state.pending_count || 0);
  saleReturnStatus.dataset.pending = String(saleReturnPending);
  saleReturnStatus.hidden = saleReturnPending === 0;
  if (saleReturnPending > 0) {
    const first = Array.isArray(state.pending_serials)
      ? state.pending_serials[0]
      : null;
    const label = first?.serial
      ? `: ${first.serial}${first.product ? " - " + first.product : ""}`
      : "";
    saleReturnStatus.textContent = `${saleReturnPending} returned product${saleReturnPending === 1 ? "" : "s"} waiting for shelf QR${label}`;
    setSaleScanMode("return", { silent: true, status: false });
  } else {
    saleReturnStatus.textContent = "";
  }
  syncSubmitButton();
}

function clearNode(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
}

function appendCell(row, text, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = text;
  row.appendChild(cell);
  return cell;
}

function createSmallText(text) {
  const small = document.createElement("small");
  small.textContent = text;
  return small;
}

function createRateForm(action, rate) {
  const formNode = document.createElement("form");
  formNode.method = "post";
  formNode.action = action;
  formNode.className = "inline-form compact-inline";
  formNode.dataset.autosave = action;

  const rateInput = document.createElement("input");
  rateInput.name = "rate";
  rateInput.type = "number";
  rateInput.min = "0";
  rateInput.step = "0.01";
  rateInput.value = rate;

  const button = document.createElement("button");
  button.className = "button small";
  button.type = "submit";
  button.textContent = "Set";

  formNode.append(rateInput, button);
  return formNode;
}

function updateVoucherPreview(summary) {
  if (!voucherPreviewBody || !summary) return;
  const lines = Array.isArray(summary.lines) ? summary.lines : [];
  clearNode(voucherPreviewBody);
  if (voucherLineCount) {
    voucherLineCount.textContent = `${lines.length} product line${lines.length === 1 ? "" : "s"}`;
  }

  if (!lines.length) {
    const row = document.createElement("tr");
    appendCell(
      row,
      batchType === "SALE"
        ? "Scan product QR codes to build the sale"
        : "Scan serials to build the voucher preview",
      "empty",
    ).colSpan = 10;
    voucherPreviewBody.appendChild(row);
  } else {
    lines.forEach((line) => {
      const row = document.createElement("tr");
      const productCell = appendCell(row, "");
      const strong = document.createElement("strong");
      strong.textContent = line.product_name || "";
      productCell.appendChild(strong);
      if (line.tally_stock_item_name)
        productCell.appendChild(createSmallText(line.tally_stock_item_name));
      appendCell(row, line.hsn || "-");
      appendCell(row, `${line.quantity || 0} ${line.unit || ""}`.trim());
      const rateCell = appendCell(row, "");
      if (canEditVoucher && batchId) {
        rateCell.appendChild(
          createRateForm(
            `/batches/${batchId}/products/${line.product_id}/rate`,
            line.rate || "0.00",
          ),
        );
      } else {
        rateCell.textContent = line.rate || "0.00";
      }
      const discountCell = appendCell(row, `${line.discount_rate || "0.00"}%`);
      if (Number(line.discount_amount || 0) > 0) {
        discountCell.appendChild(createSmallText(`-${line.discount_amount}`));
      }
      appendCell(row, line.taxable_value || "0.00");
      appendCell(row, line.cgst_amount || "0.00");
      appendCell(row, line.sgst_amount || "0.00");
      appendCell(row, line.igst_amount || "0.00");
      appendCell(row, line.line_total || "0.00");
      voucherPreviewBody.appendChild(row);
    });
  }

  if (!voucherPreviewFoot) return;
  voucherPreviewFoot.hidden = !lines.length;
  clearNode(voucherPreviewFoot);
  [
    ["Taxable value", summary.taxable_value],
    ["CGST", summary.cgst_amount],
    ["SGST", summary.sgst_amount],
    ["IGST", summary.igst_amount],
    ["Round off", summary.round_off],
    ["Final invoice value", summary.final_value, "grand-total"],
  ].forEach(([label, value, className]) => {
    const row = document.createElement("tr");
    if (className) row.className = className;
    const heading = document.createElement("th");
    heading.colSpan = 6;
    heading.textContent = label;
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.textContent = value || "0.00";
    row.append(heading, cell);
    voucherPreviewFoot.appendChild(row);
  });
}

function createStatusBadge(status) {
  const badge = document.createElement("span");
  badge.className = `status ${String(status || "").toLowerCase()}`;
  badge.textContent = status || "-";
  return badge;
}

function updateScannedSerials(items) {
  if (!scannedSerialsBody) return;
  const rows = Array.isArray(items) ? items : [];
  clearNode(scannedSerialsBody);
  if (!rows.length) {
    const row = document.createElement("tr");
    appendCell(row, "No scans yet", "empty").colSpan = 8;
    scannedSerialsBody.appendChild(row);
    return;
  }

  rows.forEach((item) => {
    const row = document.createElement("tr");
    appendCell(row, item.serial_number || "");
    appendCell(row, item.product_name || "");
    const batchCell = appendCell(row, item.product_batch_number || "-");
    if (item.fefo_picked) batchCell.appendChild(createSmallText("FEFO picked"));
    appendCell(row, item.expiry_date || "-");
    const shelfCell = appendCell(row, "");
    if (item.shelf_code) {
      const strong = document.createElement("strong");
      strong.textContent = item.shelf_code;
      shelfCell.appendChild(strong);
      if (item.shelf_verified_at)
        shelfCell.appendChild(
          createSmallText(`Verified ${item.shelf_verified_at}`),
        );
    } else if (item.shelf_pending) {
      shelfCell.appendChild(createStatusBadge("PENDING_SYNC"));
    } else {
      shelfCell.textContent = "Not required";
    }
    const statusCell = appendCell(row, "");
    statusCell.appendChild(createStatusBadge(item.status));
    const rateCell = appendCell(row, "");
    if (canEditScans && batchType !== "AUDIT" && batchId) {
      rateCell.appendChild(
        createRateForm(
          `/batches/${batchId}/items/${item.id}/rate`,
          item.rate || "0.00",
        ),
      );
    } else {
      rateCell.textContent = item.rate || "0.00";
    }
    const actionCell = appendCell(row, "");
    if (canEditScans && batchId) {
      const formNode = document.createElement("form");
      formNode.method = "post";
      formNode.action = `/batches/${batchId}/items/${item.id}/delete`;
      const button = document.createElement("button");
      button.className = "button small ghost";
      button.type = "submit";
      button.textContent = "Remove";
      formNode.appendChild(button);
      actionCell.appendChild(formNode);
    }
    scannedSerialsBody.appendChild(row);
  });
}

function updateBatchState(payload) {
  if (!payload) return;
  if (typeof payload.item_count !== "undefined")
    setScanCount(payload.item_count);
  if (payload.summary) updateVoucherPreview(payload.summary);
  if (payload.items) updateScannedSerials(payload.items);
  if (payload.sale_return) updateSaleReturnState(payload.sale_return);
}

function showScannerTools() {
  if (scannerTools) scannerTools.style.display = "flex";
}

function setCameraControls(active) {
  if (!cameraButton) return;
  cameraButton.hidden = false;
  cameraButton.disabled = cameraStarting;
  cameraButton.textContent = cameraStarting
    ? "Starting..."
    : active
      ? "Stop scanning"
      : "Start scanning";
  cameraButton.setAttribute("aria-pressed", active ? "true" : "false");
  cameraButton.className = active ? "button" : "button primary";
}

function resultText(result) {
  if (!result) return "";
  const text =
    typeof result.getText === "function"
      ? result.getText()
      : String(result.text || result.rawValue || "");
  return cleanDecodedText(text);
}

function cleanDecodedText(text) {
  const value = String(text || "").trim();
  const serial = value.match(/[A-Z0-9][A-Z0-9_-]{0,64}-\d{3,}/i);
  return (serial ? serial[0] : value).replace(/\s+/g, "").toUpperCase();
}

function looksLikeSerial(text) {
  return /^[A-Z0-9][A-Z0-9_-]{0,64}-\d{3,}$/.test(cleanDecodedText(text));
}

const scanCanvas = document.createElement("canvas");
const scanCtx = scanCanvas.getContext("2d", { willReadFrequently: true });
const cropCanvas = document.createElement("canvas");
const cropCtx = cropCanvas.getContext("2d", { willReadFrequently: true });
const enhCanvas = document.createElement("canvas");
const enhCtx = enhCanvas.getContext("2d", { willReadFrequently: true });
const nativeFormatPreference = ["qr_code", "code_128"];
const scanRegions = [
  { x: 0.05, y: 0.22, w: 0.9, h: 0.58 },
  { x: 0.05, y: 0.32, w: 0.9, h: 0.34 },
  { x: 0.0, y: 0.0, w: 1.0, h: 1.0 },
  { x: 0.0, y: 0.18, w: 1.0, h: 0.64 },
];

function captureFrame() {
  if (!video || video.readyState < 2) return false;
  const w = video.videoWidth;
  const h = video.videoHeight;
  if (!w || !h) return false;
  scanCanvas.width = w;
  scanCanvas.height = h;
  scanCtx.drawImage(video, 0, 0, w, h);
  return true;
}

function copyFrameRegion(region) {
  const baseW = scanCanvas.width;
  const baseH = scanCanvas.height;
  const sx = Math.max(0, Math.floor(baseW * region.x));
  const sy = Math.max(0, Math.floor(baseH * region.y));
  const sw = Math.min(baseW - sx, Math.max(1, Math.floor(baseW * region.w)));
  const sh = Math.min(baseH - sy, Math.max(1, Math.floor(baseH * region.h)));
  const scale = sw < 900 ? 900 / sw : 1;
  cropCanvas.width = Math.round(sw * scale);
  cropCanvas.height = Math.round(sh * scale);
  cropCtx.imageSmoothingEnabled = false;
  cropCtx.drawImage(
    scanCanvas,
    sx,
    sy,
    sw,
    sh,
    0,
    0,
    cropCanvas.width,
    cropCanvas.height,
  );
  return cropCanvas;
}

function createEnhancedFrame(source) {
  const w = source.width;
  const h = source.height;
  enhCanvas.width = w;
  enhCanvas.height = h;
  enhCtx.drawImage(source, 0, 0);
  const imageData = enhCtx.getImageData(0, 0, w, h);
  const data = imageData.data;
  let min = 255;
  let max = 0;
  for (let i = 0; i < data.length; i += 4) {
    const gray = (data[i] * 77 + data[i + 1] * 150 + data[i + 2] * 29) >> 8;
    data[i] = gray;
    data[i + 1] = gray;
    data[i + 2] = gray;
    if (gray < min) min = gray;
    if (gray > max) max = gray;
  }
  const range = max - min || 1;
  for (let i = 0; i < data.length; i += 4) {
    const v = (((data[i] - min) * 255) / range) | 0;
    const bounded = v < 0 ? 0 : v > 255 ? 255 : v;
    data[i] = bounded;
    data[i + 1] = bounded;
    data[i + 2] = bounded;
  }
  enhCtx.putImageData(imageData, 0, 0);
  enhCtx.filter = "contrast(1.8) brightness(1.05)";
  enhCtx.drawImage(enhCanvas, 0, 0);
  enhCtx.filter = "none";
  return enhCanvas;
}

function createBinarizedFrame(source) {
  const w = source.width;
  const h = source.height;
  enhCanvas.width = w;
  enhCanvas.height = h;
  enhCtx.drawImage(source, 0, 0);
  const imageData = enhCtx.getImageData(0, 0, w, h);
  const data = imageData.data;
  let sum = 0;
  const count = data.length / 4;
  for (let i = 0; i < data.length; i += 4) {
    const gray = (data[i] * 77 + data[i + 1] * 150 + data[i + 2] * 29) >> 8;
    data[i] = gray;
    data[i + 1] = gray;
    data[i + 2] = gray;
    sum += gray;
  }
  const threshold = (sum / count) * 0.85;
  for (let i = 0; i < data.length; i += 4) {
    const v = data[i] < threshold ? 0 : 255;
    data[i] = v;
    data[i + 1] = v;
    data[i + 2] = v;
  }
  enhCtx.putImageData(imageData, 0, 0);
  return enhCanvas;
}

const ZX = window.ZXing || {};
let zxingHints = null;
let code128Reader = null;
let qrReader = null;

function canDecodeCanvasQr() {
  return !!(
    ZX.QRCodeReader &&
    ZX.HTMLCanvasElementLuminanceSource &&
    ZX.HybridBinarizer &&
    ZX.BinaryBitmap
  );
}

function getZxingHints() {
  if (!ZX.DecodeHintType || !ZX.BarcodeFormat) return null;
  if (!zxingHints) {
    const formats = ["CODE_128"]
      .map((name) => ZX.BarcodeFormat[name])
      .filter((format) => typeof format !== "undefined");
    zxingHints = new Map();
    if (formats.length)
      zxingHints.set(ZX.DecodeHintType.POSSIBLE_FORMATS, formats);
    zxingHints.set(ZX.DecodeHintType.TRY_HARDER, true);
  }
  return zxingHints;
}

function getCode128Reader() {
  if (!ZX.Code128Reader || !ZX.BitArray) return null;
  if (!code128Reader) code128Reader = new ZX.Code128Reader();
  if (!zxingHints) getZxingHints();
  return code128Reader;
}

function getQrReader() {
  if (!canDecodeCanvasQr()) return null;
  if (!qrReader) qrReader = new ZX.QRCodeReader();
  return qrReader;
}

function decodeCanvasQr(canvas) {
  const reader = getQrReader();
  if (!reader) return "";
  try {
    const luminance = new ZX.HTMLCanvasElementLuminanceSource(canvas, false);
    const binarizer = new ZX.HybridBinarizer(luminance);
    return resultText(reader.decode(new ZX.BinaryBitmap(binarizer)));
  } catch (e) {
    return "";
  } finally {
    if (typeof reader.reset === "function") reader.reset();
  }
}

function decodeRowsCode128(source) {
  const reader = getCode128Reader();
  if (!reader || !source || typeof source.getContext !== "function") return "";
  const ctx = source.getContext("2d", { willReadFrequently: true });
  if (!ctx) return "";
  const w = source.width;
  const h = source.height;
  const rows = [
    0.18, 0.22, 0.26, 0.3, 0.34, 0.38, 0.42, 0.46, 0.5, 0.54, 0.58, 0.62, 0.66,
    0.7, 0.74, 0.78, 0.82,
  ];

  for (const fraction of rows) {
    const y = Math.max(0, Math.min(h - 1, Math.round(h * fraction)));
    const gray = readGrayRow(ctx, w, h, y);
    let min = 255;
    let max = 0;
    let sum = 0;

    for (let x = 0; x < w; x++) {
      const value = gray[x];
      if (value < min) min = value;
      if (value > max) max = value;
      sum += value;
    }

    if (max - min < 35) continue;
    const avg = sum / w;
    const thresholds = [
      (min + max) / 2,
      avg * 0.72,
      avg * 0.82,
      avg * 0.95,
      avg * 1.08,
    ];
    for (const threshold of thresholds) {
      const attempts = code128RowAttempts(gray, threshold);
      for (const attempt of attempts) {
        const code =
          decodeCode128Row(attempt, threshold, y) ||
          decodeCode128Row(reverseGray(attempt), threshold, y);
        if (code) return code;
      }
    }
  }
  return "";
}

function readGrayRow(ctx, width, height, centerY) {
  const bandTop = Math.max(0, centerY - 2);
  const bandHeight = Math.min(height - bandTop, 5);
  const pixels = ctx.getImageData(0, bandTop, width, bandHeight).data;
  const gray = new Uint8Array(width);
  for (let x = 0; x < width; x++) {
    let sum = 0;
    for (let y = 0; y < bandHeight; y++) {
      const i = (y * width + x) * 4;
      sum += (pixels[i] * 77 + pixels[i + 1] * 150 + pixels[i + 2] * 29) >> 8;
    }
    gray[x] = Math.round(sum / bandHeight);
  }
  return gray;
}

function code128RowAttempts(gray, threshold) {
  const attempts = [gray];
  const trimmed = trimBarcodeRow(gray, threshold);
  if (trimmed) attempts.push(trimmed);
  return attempts;
}

function trimBarcodeRow(gray, threshold) {
  const transitions = [];
  let previous = gray[0] < threshold;
  for (let x = 1; x < gray.length; x++) {
    const current = gray[x] < threshold;
    if (current !== previous) transitions.push(x);
    previous = current;
  }
  if (transitions.length < 18) return null;

  const maxGap = Math.max(18, Math.min(90, Math.round(gray.length * 0.07)));
  let bestStart = 0;
  let bestEnd = 0;
  let bestCount = 0;
  let start = 0;
  for (let i = 1; i <= transitions.length; i++) {
    const split =
      i === transitions.length || transitions[i] - transitions[i - 1] > maxGap;
    if (!split) continue;
    const count = i - start;
    if (count > bestCount) {
      bestStart = start;
      bestEnd = i - 1;
      bestCount = count;
    }
    start = i;
  }

  if (bestCount < 18) return null;
  const padding = Math.max(48, Math.round(gray.length * 0.04));
  const startX = Math.max(0, transitions[bestStart] - padding);
  const endX = Math.min(gray.length, transitions[bestEnd] + padding);
  if (endX - startX < 140) return null;

  const quiet = Math.max(64, Math.round((endX - startX) * 0.1));
  const row = new Uint8Array(endX - startX + quiet * 2);
  row.fill(255);
  row.set(gray.slice(startX, endX), quiet);
  return row;
}

function reverseGray(gray) {
  const reversed = new Uint8Array(gray.length);
  for (let i = 0, j = gray.length - 1; i < gray.length; i++, j--) {
    reversed[i] = gray[j];
  }
  return reversed;
}

function decodeCode128Row(gray, threshold, rowNumber) {
  const reader = getCode128Reader();
  const row = new ZX.BitArray(gray.length);
  for (let x = 0; x < gray.length; x++) {
    if (gray[x] < threshold) row.set(x);
  }
  try {
    return resultText(reader.decodeRow(rowNumber, row, zxingHints || null));
  } catch (e) {
    return "";
  }
}

async function createNativeDetector() {
  if (!("BarcodeDetector" in window)) return null;
  try {
    let formats = nativeFormatPreference;
    if (typeof BarcodeDetector.getSupportedFormats === "function") {
      const supported = await BarcodeDetector.getSupportedFormats();
      formats = supported.length
        ? nativeFormatPreference.filter((format) => supported.includes(format))
        : nativeFormatPreference;
      if (!formats.length) return null;
    }
    return new BarcodeDetector({ formats });
  } catch (e) {
    return null;
  }
}

async function decodeNative(source) {
  if (!nativeDetector) return "";
  try {
    const codes = await nativeDetector.detect(source);
    return resultText(codes[0]);
  } catch (e) {
    return "";
  }
}

function stopCamera() {
  scanning = false;
  lastCode = "";
  if (scanTimer) {
    clearTimeout(scanTimer);
    scanTimer = null;
  }
  if (activeStream) {
    activeStream.getTracks().forEach((track) => track.stop());
    activeStream = null;
  }
  if (video) video.srcObject = null;
  if (scanLine) {
    scanLine.style.display = "none";
    scanLine.classList.remove("detected");
  }
  setCameraControls(false);
}

function scheduleScan(delay = 120) {
  if (!scanning) return;
  if (scanTimer) clearTimeout(scanTimer);
  scanTimer = setTimeout(() => {
    scanTimer = null;
    scanLoop();
  }, delay);
}

function finishSubmission(delay = 250) {
  submitting = false;
  if (input) input.value = "";
  if (scanLine) {
    setTimeout(() => scanLine.classList.remove("detected"), 180);
  }
  setCameraControls(scanning);
  scheduleScan(delay);
}

async function submitSerial(serial, source = "camera") {
  serial = cleanDecodedText(serial);
  if (!serial || submitting || !form) return;
  submitting = true;
  const activeScanMode = scanModeInput?.value || "sale";
  if (input) input.value = serial;
  if (sourceInput) sourceInput.value = source;
  if (scanLine) scanLine.classList.add("detected");
  setScanStatus(
    `${activeScanMode === "return" ? "Returning" : "Adding"} ${serial}...`,
  );

  const data = new FormData();
  data.append("serial_number", serial);
  data.append("scan_source", source);
  data.append("scan_mode", activeScanMode);

  try {
    const response = await fetch(form.action, {
      method: "POST",
      body: data,
      credentials: "same-origin",
      headers: { Accept: "application/json", "X-Requested-With": "fetch" },
    });
    const payload = await readScanResponse(response);
    if (!payload.ok) {
      const errorMsg = payload.error || payload.detail || "Scan rejected";
      const kind = errorMsg.includes("Already scanned") ? "warn" : "error";
      showToast(
        errorMsg.includes("not found")
          ? `Serial not found: ${serial}`
          : errorMsg,
        kind,
        4000,
      );
      updateShelfState(payload);
      updateBatchState(payload);
      setScanStatus(
        scanning
          ? payload.shelf_required || payload.sale_return?.return_shelf_required
            ? "Shelf QR required"
            : "Scan rejected - scan the next code"
          : "Scanning stopped",
      );
      finishSubmission();
      return;
    }

    if (payload.scan_type === "sale_return_product") {
      const product = payload.product || "";
      const serialNum = payload.serial || serial;
      showToast(
        `${serialNum}${product ? " - " + product : ""} returned`,
        "warn",
        3500,
      );
      updateBatchState(payload);
      setSaleScanMode("return", { silent: true, status: false });
      setScanStatus(
        scanning ? "Returned - scan shelf QR" : "Return shelf pending",
      );
      finishSubmission(500);
      return;
    }

    if (payload.scan_type === "sale_return_shelf") {
      showToast(
        `${payload.verified_count || 0} returned product(s) placed at ${payload.location_code || "shelf"}`,
        "success",
        3500,
      );
      updateBatchState(payload);
      setSaleScanMode("sale", { silent: true, status: false });
      setScanStatus(
        scanning
          ? "Return complete - scan the next product"
          : "Scanning stopped",
      );
      finishSubmission(500);
      return;
    }

    if (payload.scan_type === "shelf") {
      showToast(
        `${payload.verified_count || 0} product(s) verified at ${payload.location_code || "shelf"}`,
        "success",
        3500,
      );
      updateShelfState(payload);
      updateBatchState(payload);
      setScanStatus(
        scanning
          ? "Shelf verified - scan the next product"
          : "Scanning stopped",
      );
      finishSubmission(500);
      return;
    }

    const product = payload.product || "";
    const serialNum = payload.serial || serial;
    showToast(
      `${serialNum}${product ? " - " + product : ""} added`,
      "success",
      2500,
    );
    if (typeof payload.item_count === "undefined") updateScanCount();
    updateBatchState(payload);
    updateShelfState(payload);
    setScanStatus(
      scanning
        ? payload.shelf_required
          ? "Product added - scan the shelf QR now"
          : `${serialNum} added - scan the next code`
        : "Scanning stopped",
    );
    finishSubmission();
  } catch (e) {
    showToast("Network error - check connection", "error", 5000);
    setScanStatus(
      scanning ? "Network error - scan the code again" : "Scanning stopped",
    );
    lastCode = "";
    finishSubmission(500);
  }
}

async function readScanResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  if (response.redirected && response.url.includes("/login")) {
    return { ok: false, error: "Session expired. Sign in again." };
  }
  if (response.ok) {
    return { ok: true };
  }
  return { ok: false, error: "Scan failed. Refresh and try again." };
}

function scheduleInputAutoSubmit() {
  if (!scanning || submitting || !input || !looksLikeSerial(input.value))
    return;
  clearTimeout(inputSubmitTimer);
  inputSubmitTimer = setTimeout(() => {
    if (!submitting && looksLikeSerial(input.value)) {
      submitSerial(input.value, "camera");
    }
  }, 250);
}

async function scanLoop() {
  if (!scanning || submitting) {
    scheduleScan();
    return;
  }
  if (!captureFrame()) {
    scheduleScan(150);
    return;
  }

  let code = "";
  try {
    code = await decodeFrame();
  } catch (e) {
    code = "";
  }

  if (!scanning) return;
  if (code && code !== lastCode) {
    lastCode = code;
    await submitSerial(code, "camera");
    return;
  }
  scheduleScan();
}

async function decodeFrame() {
  let code = await decodeNative(video);
  if (code) return code;

  for (const region of scanRegions) {
    code = await decodeSource(copyFrameRegion(region));
    if (code) return code;
  }
  return "";
}

async function decodeSource(source) {
  let code = decodeCanvasQr(source);
  if (code) return code;

  code = await decodeNative(source);
  if (code) return code;

  code = decodeRowsCode128(source);
  if (code) return code;

  const enhanced = createEnhancedFrame(source);
  code = decodeCanvasQr(enhanced) || decodeRowsCode128(enhanced);
  if (code) return code;

  code = await decodeNative(enhanced);
  if (code) return code;

  const binarized = createBinarizedFrame(source);
  return (
    decodeCanvasQr(binarized) ||
    decodeRowsCode128(binarized) ||
    (await decodeNative(binarized))
  );
}

async function startCamera() {
  if (activeStream || scanning) return true;
  if (cameraStarting) return false;
  cameraStarting = true;
  setCameraControls(false);

  if (
    !navigator.mediaDevices ||
    !navigator.mediaDevices.getUserMedia ||
    !window.isSecureContext
  ) {
    setScanStatus("HTTPS required for live camera");
    showToast("Live camera needs HTTPS or localhost", "warn", 5000);
    cameraStarting = false;
    setCameraControls(false);
    return false;
  }

  stopCamera();
  cameraStarting = true;
  setCameraControls(false);
  setScanStatus("Starting camera...");
  if (scanLine) scanLine.style.display = "";

  try {
    activeStream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {
        facingMode: { ideal: "environment" },
        width: { ideal: 1920 },
        height: { ideal: 1080 },
        focusMode: { ideal: "continuous" },
      },
    });
    video.srcObject = activeStream;
    video.setAttribute("playsinline", "");
    await video.play();
    nativeDetector = await createNativeDetector();
    scanning = true;
    setScanStatus("Scanning - align barcode with line");
    cameraStarting = false;
    setCameraControls(true);
    scanLoop();
    return true;
  } catch (e) {
    stopCamera();
    setScanStatus("Camera unavailable");
    showToast("Camera permission denied or unavailable", "error", 5000);
    cameraStarting = false;
    setCameraControls(false);
    return false;
  }
}

saleModeButtons.forEach((button) => {
  button.addEventListener("click", () => {
    setSaleScanMode(button.dataset.scanModeButton || "sale");
  });
});
setSaleScanMode(scanModeInput?.value || "sale", {
  silent: true,
  status: false,
});
syncSubmitButton();

if (form) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!canManual) {
      showToast("Use camera scan to add serials", "warn");
      return;
    }
    await submitSerial(input.value.trim(), "manual");
  });
}

if (input) {
  input.addEventListener("input", scheduleInputAutoSubmit);
  input.addEventListener("change", scheduleInputAutoSubmit);
}

window.addEventListener("beforeunload", stopCamera);

if (scannerBox && video) {
  showScannerTools();
  setCameraControls(false);
  setScanStatus("Starting camera...");
  if (cameraButton) {
    cameraButton.addEventListener("click", async () => {
      if (activeStream || scanning) {
        stopCamera();
        setScanStatus("Scanning stopped");
        return;
      }
      await startCamera();
    });
  }
  startCamera().then((ok) => {
    if (!ok) setScanStatus("Press Start scanning");
  });
}

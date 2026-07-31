// Frontend logic for the Bill OCR app: file selection, preview, calling the
// backend, rendering the results table, and triggering the Excel download.

const dropZone = document.getElementById("dropZone");
const fileInput = document.getElementById("fileInput");
const browseBtn = document.getElementById("browseBtn");
const previewGrid = document.getElementById("previewGrid");
const actionsRow = document.getElementById("actionsRow");
const extractBtn = document.getElementById("extractBtn");
const extractBtnText = document.getElementById("extractBtnText");
const clearBtn = document.getElementById("clearBtn");
const loadingState = document.getElementById("loadingState");
const loadingText = document.getElementById("loadingText");
const resultsSection = document.getElementById("resultsSection");
const billsContainer = document.getElementById("billsContainer");
const downloadBtn = document.getElementById("downloadBtn");
const settingsToggle = document.getElementById("settingsToggle");
const settingsPanel = document.getElementById("settingsPanel");
const apiKeyInput = document.getElementById("apiKeyInput");
const saveKeyBtn = document.getElementById("saveKeyBtn");
const toastContainer = document.getElementById("toastContainer");

// Holds the File objects the user has currently selected.
let selectedFiles = [];

// ---------- Settings (API key persisted in localStorage) ----------

apiKeyInput.value = localStorage.getItem("gemini_api_key") || "";

settingsToggle.addEventListener("click", () => {
  settingsPanel.classList.toggle("hidden");
});

saveKeyBtn.addEventListener("click", () => {
  localStorage.setItem("gemini_api_key", apiKeyInput.value.trim());
  showToast("API key saved", "success");
});

// ---------- File selection (drag-and-drop + click-to-browse) ----------

browseBtn.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("click", (event) => {
  // Avoid double-triggering when the click originated from the browse button.
  if (event.target !== browseBtn) fileInput.click();
});

fileInput.addEventListener("change", () => {
  addFiles(fileInput.files);
  // Reset so re-picking the same filename still fires a change event.
  fileInput.value = "";
});

["dragenter", "dragover"].forEach((eventName) => {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.add("drag-over");
  });
});

["dragleave", "drop"].forEach((eventName) => {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.remove("drag-over");
  });
});

dropZone.addEventListener("drop", (event) => {
  addFiles(event.dataTransfer.files);
});

function addFiles(fileList) {
  const newImages = Array.from(fileList).filter((file) => file.type.startsWith("image/"));
  selectedFiles = selectedFiles.concat(newImages);
  invalidateResults();
  renderPreviews();
}

// Any change to the file selection makes the on-screen results stale. Since
// the download now ships exactly what's displayed, drop both together so the
// user can never export a table that doesn't match their current files.
function invalidateResults() {
  lastResults = null;
  billsContainer.innerHTML = "";
  resultsSection.classList.add("hidden");
}

function renderPreviews() {
  previewGrid.innerHTML = "";

  if (selectedFiles.length === 0) {
    previewGrid.classList.add("hidden");
    actionsRow.classList.add("hidden");
    return;
  }

  selectedFiles.forEach((file, index) => {
    const item = document.createElement("div");
    item.className = "preview-item";

    const img = document.createElement("img");
    img.src = URL.createObjectURL(file);
    img.onload = () => URL.revokeObjectURL(img.src);

    const removeBtn = document.createElement("button");
    removeBtn.className = "remove-btn";
    removeBtn.textContent = "✕";
    removeBtn.addEventListener("click", (event) => {
      // Stop the click bubbling to the drop zone, which would reopen the picker.
      event.stopPropagation();
      selectedFiles.splice(index, 1);
      invalidateResults();
      renderPreviews();
    });

    item.append(img, removeBtn);
    previewGrid.appendChild(item);
  });

  previewGrid.classList.remove("hidden");
  actionsRow.classList.remove("hidden");
}

clearBtn.addEventListener("click", () => {
  selectedFiles = [];
  fileInput.value = "";
  invalidateResults();
  renderPreviews();
});

// ---------- Extraction ----------

extractBtn.addEventListener("click", extractBills);

let lastResults = null;

async function extractBills() {
  if (selectedFiles.length === 0) return;

  setLoading(true, "Reading your bill…");
  resultsSection.classList.add("hidden");

  try {
    const formData = new FormData();
    selectedFiles.forEach((file) => formData.append("files", file));
    const apiKey = localStorage.getItem("gemini_api_key");
    if (apiKey) formData.append("api_key", apiKey);

    const response = await fetch("/api/bills/preview", {
      method: "POST",
      body: formData,
    });

    if (!response.ok) {
      const detail = await safeErrorDetail(response);
      throw new Error(detail || `Server returned ${response.status}`);
    }

    lastResults = await response.json();
    renderResults(lastResults);
    resultsSection.classList.remove("hidden");
    showToast("Extraction complete", "success");
  } catch (error) {
    showToast(error.message || "Something went wrong", "error");
  } finally {
    setLoading(false);
  }
}

async function safeErrorDetail(response) {
  try {
    const body = await response.json();
    return body.detail;
  } catch {
    return null;
  }
}

function setLoading(isLoading, text) {
  loadingState.classList.toggle("hidden", !isLoading);
  extractBtn.disabled = isLoading;
  if (text) loadingText.textContent = text;
}

// ---------- Rendering the extracted data preview ----------

function renderResults(results) {
  billsContainer.innerHTML = "";

  results.forEach((result, index) => {
    const bill = result.bill;
    const card = document.createElement("div");
    card.className = "bill-card";

    const title = document.createElement("h3");
    title.innerHTML = `Bill ${index + 1}: ${escapeHtml(bill.vendor_name || result.source_filename)}
      <span class="engine-badge ${result.engine}">${result.engine}</span>`;

    const summary = document.createElement("div");
    summary.className = "summary-grid";
    summary.innerHTML = [
      ["Bill Number", bill.bill_number],
      ["Date", bill.bill_date],
      ["Subtotal", formatMoney(bill.subtotal)],
      ["Tax", formatMoney(bill.tax_total)],
      ["Grand Total", formatMoney(bill.grand_total)],
      ["Payment", bill.payment_method],
    ]
      .map(([label, value]) => `<div><span>${label}</span>${escapeHtml(value ?? "—")}</div>`)
      .join("");

    const tableWrap = document.createElement("div");
    tableWrap.className = "items-table-wrap";
    tableWrap.innerHTML = buildItemsTable(bill.line_items);

    card.append(title, summary, tableWrap);
    billsContainer.appendChild(card);
  });
}

function buildItemsTable(items) {
  if (!items || items.length === 0) {
    return "<p class=\"hint\">No line items detected.</p>";
  }

  const headers = ["#", "Item", "Qty", "Unit", "Rate", "Discount", "Tax %", "Total"];
  const rows = items
    .map(
      (item) => `<tr>
        <td>${item.serial_no ?? ""}</td>
        <td>${escapeHtml(item.item_name)}</td>
        <td>${item.quantity ?? ""}</td>
        <td>${escapeHtml(item.unit ?? "")}</td>
        <td>${formatMoney(item.rate)}</td>
        <td>${item.discount ?? ""}</td>
        <td>${item.tax_rate ?? ""}</td>
        <td>${formatMoney(item.total)}</td>
      </tr>`
    )
    .join("");

  return `<table class="items-table">
      <thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function formatMoney(value) {
  if (value === null || value === undefined) return "—";
  return Number(value).toFixed(2);
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

// ---------- Download ----------

downloadBtn.addEventListener("click", downloadExcel);

async function downloadExcel() {
  // Send back the data we already extracted rather than re-uploading the
  // images, so the download doesn't trigger a second (billed) OCR pass.
  if (!lastResults || lastResults.length === 0) return;

  setLoading(true, "Building your spreadsheet…");
  try {
    const response = await fetch("/api/bills/workbook", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(lastResults),
    });

    if (!response.ok) {
      const detail = await safeErrorDetail(response);
      throw new Error(detail || `Server returned ${response.status}`);
    }

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "extracted_bills.xlsx";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);

    showToast("Excel file downloaded", "success");
  } catch (error) {
    showToast(error.message || "Download failed", "error");
  } finally {
    setLoading(false);
  }
}

// ---------- Toasts ----------

function showToast(message, type = "success") {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  toastContainer.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}

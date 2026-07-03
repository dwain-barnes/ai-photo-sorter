const state = {
  reviewRows: [],
  pollTimer: null,
  activeScanJobId: null,
};

const DEFAULT_CONFIDENCE_THRESHOLD = 0.75;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

document.addEventListener("DOMContentLoaded", () => {
  bindTabs();
  bindControls();
  refreshCategories();
});

function bindTabs() {
  $$(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      $$(".tab").forEach((item) => item.classList.toggle("active", item === button));
      $$(".section").forEach((section) => {
        section.classList.toggle("active", section.id === button.dataset.section);
      });
      if (button.dataset.section === "search") refreshCategories();
    });
  });
}

function bindControls() {
  $$("[data-pick]").forEach((button) => {
    button.addEventListener("click", async () => {
      const response = await postJson("/api/pick-folder", {});
      if (response.path) $("#" + button.dataset.pick).value = response.path;
    });
  });

  $("#scanImages").addEventListener("click", startScan);
  $("#cancelScan").addEventListener("click", cancelScan);
  $("#approveHigh").addEventListener("click", approveHighConfidence);
  $("#sendLowReview").addEventListener("click", sendLowConfidenceToReview);
  $("#applyChanges").addEventListener("click", applyChanges);
  $("#runSearch").addEventListener("click", runSearch);
  $("#searchQuery").addEventListener("keydown", (event) => {
    if (event.key === "Enter") runSearch();
  });
}

function collectSettings() {
  return {
    input_folder: $("#inputFolder").value.trim(),
    output_folder: $("#outputFolder").value.trim(),
    rename_images: true,
    organize_folders: true,
    create_database: true,
    filename_structure: true,
    category_mode: "automatic",
    custom_categories: "",
    confidence_threshold: DEFAULT_CONFIDENCE_THRESHOLD,
    batch_limit: 0,
    mode: $("#fileMode").value,
  };
}

async function startScan() {
  if (state.activeScanJobId) return;

  const settings = collectSettings();
  if (!settings.input_folder || !settings.output_folder) {
    showToast("Choose input and output folders first.");
    return;
  }

  setScanRunning(true);
  $("#applyChanges").disabled = true;
  setProgress(0, "Starting scan");
  renderReview([]);

  try {
    const response = await postJson("/api/scan", settings);
    if (!response.ok) throw new Error(response.error || "Scan could not start.");
    state.activeScanJobId = response.job_id;
    pollJob(response.job_id);
  } catch (error) {
    state.activeScanJobId = null;
    setScanRunning(false);
    setProgress(0, error.message);
    showToast(error.message);
  }
}

async function cancelScan() {
  if (!state.activeScanJobId) return;
  setBusy("#cancelScan", true);
  setProgress(currentProgressPercent(), "Stopping scan...");
  try {
    const response = await postJson(`/api/jobs/${state.activeScanJobId}/cancel`, {});
    if (!response.ok) throw new Error(response.error || "Scan could not be cancelled.");
  } catch (error) {
    setBusy("#cancelScan", false);
    showToast(error.message);
  }
}

function pollJob(jobId) {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    try {
      const response = await fetch(`/api/jobs/${jobId}`);
      const job = await response.json();
      if (!job.ok) throw new Error(job.error || "Scan job not found.");
      const percent = job.total ? (job.done / job.total) * 100 : 0;
      setProgress(percent, job.message || "Scanning");
      if (job.status === "done") {
        state.reviewRows = job.results.map((row) => ({ ...row, approved: !row.needs_review }));
        renderReview(state.reviewRows);
        $("#applyChanges").disabled = state.reviewRows.length === 0;
        state.activeScanJobId = null;
        setScanRunning(false);
        showToast(`Scan complete: ${state.reviewRows.length} image${state.reviewRows.length === 1 ? "" : "s"}.`);
        refreshCategories();
        return;
      }
      if (job.status === "cancelled") {
        state.activeScanJobId = null;
        setScanRunning(false);
        $("#applyChanges").disabled = true;
        showToast("Scan cancelled.");
        return;
      }
      if (job.status === "error") {
        throw new Error(job.error || "Scan failed.");
      }
      pollJob(jobId);
    } catch (error) {
      state.activeScanJobId = null;
      setScanRunning(false);
      setProgress(currentProgressPercent(), error.message);
      showToast(error.message);
    }
  }, 650);
}

function setScanRunning(running) {
  setBusy("#scanImages", running);
  $("#cancelScan").classList.toggle("hidden", !running);
  $("#cancelScan").disabled = false;
}

function setProgress(percent, message) {
  $("#progressBar").style.width = `${Math.max(0, Math.min(100, percent))}%`;
  $("#scanMessage").textContent = message;
}

function currentProgressPercent() {
  return Number($("#progressBar").style.width.replace("%", "") || 0);
}

function renderReview(rows) {
  const body = $("#reviewBody");
  body.textContent = "";
  if (!rows.length) {
    const row = document.createElement("tr");
    row.className = "empty-row";
    row.innerHTML = `<td colspan="8">Choose folders and scan.</td>`;
    body.appendChild(row);
    return;
  }

  rows.forEach((item, index) => {
    const row = document.createElement("tr");
    row.dataset.index = String(index);
    row.innerHTML = `
      <td><input class="approve-input" type="checkbox" ${item.approved ? "checked" : ""}></td>
      <td><img class="thumb" src="${item.thumbnail_url}" alt=""></td>
      <td class="filename-cell">${escapeHtml(item.original_filename || "")}</td>
      <td><input class="new-name-input" type="text" value="${escapeAttr(item.new_filename || "")}"></td>
      <td><input class="folder-input" type="text" value="${escapeAttr(item.suggested_folder || "")}"></td>
      <td><input class="caption-input" type="text" value="${escapeAttr(item.caption || "")}"></td>
      <td><input class="tags-input" type="text" value="${escapeAttr((item.tags || []).join(", "))}"></td>
      <td>
        <div class="confidence">
          <meter min="0" max="1" value="${Number(item.confidence || 0)}"></meter>
          <span>${Math.round(Number(item.confidence || 0) * 100)}%</span>
        </div>
      </td>
    `;
    body.appendChild(row);
  });
}

function rowsFromReviewTable() {
  return $$("#reviewBody tr[data-index]").map((row) => {
    const item = state.reviewRows[Number(row.dataset.index)];
    return {
      ...item,
      approved: row.querySelector(".approve-input").checked,
      new_filename: row.querySelector(".new-name-input").value.trim(),
      suggested_folder: row.querySelector(".folder-input").value.trim(),
      caption: row.querySelector(".caption-input").value.trim(),
      tags: row.querySelector(".tags-input").value.split(",").map((tag) => tag.trim()).filter(Boolean),
    };
  });
}

function approveHighConfidence() {
  const threshold = currentConfidenceThreshold();
  state.reviewRows = rowsFromReviewTable().map((row) => ({
    ...row,
    approved: Number(row.confidence || 0) >= threshold && !row.needs_review,
  }));
  renderReview(state.reviewRows);
}

function sendLowConfidenceToReview() {
  const threshold = currentConfidenceThreshold();
  state.reviewRows = rowsFromReviewTable().map((row) => {
    if (Number(row.confidence || 0) < threshold || row.needs_review) {
      return {
        ...row,
        approved: false,
        category: "Needs Review",
        suggested_folder: "Needs Review",
        needs_review: true,
      };
    }
    return row;
  });
  renderReview(state.reviewRows);
}

function currentConfidenceThreshold() {
  return DEFAULT_CONFIDENCE_THRESHOLD;
}

async function applyChanges() {
  const rows = rowsFromReviewTable();
  const approved = rows.filter((row) => row.approved);
  if (!approved.length) {
    showToast("No approved rows to apply.");
    return;
  }

  setBusy("#applyChanges", true);
  try {
    const response = await postJson("/api/apply", {
      rows,
      output_folder: $("#outputFolder").value.trim(),
      mode: $("#fileMode").value,
    });
    if (!response.ok) throw new Error(response.error || "Apply failed.");
    showToast(`Applied ${response.applied.length} image${response.applied.length === 1 ? "" : "s"}.`);
    refreshCategories();
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy("#applyChanges", false);
  }
}

async function runSearch() {
  const query = $("#searchQuery").value.trim();
  if (!query && !$("#searchCategory").value && !$("#searchNeedsReview").checked) {
    showToast("Enter a search or choose a filter.");
    return;
  }
  setBusy("#runSearch", true);
  $("#interpretedQuery").textContent = "";
  try {
    const response = await postJson("/api/search", {
      ...collectSettings(),
      query,
      category: $("#searchCategory").value,
      needs_review: $("#searchNeedsReview").checked,
    });
    if (!response.ok) throw new Error(response.error || "Search failed.");
    $("#interpretedQuery").textContent = response.interpreted_query
      ? `Query: ${response.interpreted_query}`
      : "";
    renderSearchResults(response.results || []);
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy("#runSearch", false);
  }
}

function renderSearchResults(results) {
  const shell = $("#searchResults");
  shell.textContent = "";
  if (!results.length) {
    shell.innerHTML = `<div class="panel progress-panel"><p>No results</p></div>`;
    return;
  }

  results.forEach((item) => {
    const card = document.createElement("article");
    card.className = "result-card";
    const path = item.current_path || item.original_path || "";
    card.innerHTML = `
      <img class="thumb" src="${item.thumbnail_url}" alt="">
      <div>
        <h3>${escapeHtml(item.new_filename || item.original_filename || "")}</h3>
        <p>${escapeHtml(item.caption || "")}</p>
        <div class="tag-row">${(item.tags || []).slice(0, 6).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</div>
        <button class="link-button" type="button">Open Folder</button>
      </div>
    `;
    card.querySelector("button").addEventListener("click", () => {
      postJson("/api/open-folder", { path }).catch((error) => showToast(error.message));
    });
    shell.appendChild(card);
  });
}

async function refreshCategories() {
  try {
    const response = await fetch("/api/categories");
    const data = await response.json();
    if (!data.ok) return;
    const current = $("#searchCategory").value;
    $("#searchCategory").innerHTML = `<option value="">All categories</option>` + data.categories
      .map((category) => `<option value="${escapeAttr(category)}">${escapeHtml(category)}</option>`)
      .join("");
    $("#searchCategory").value = current;
  } catch {
  }
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.message || "Request failed.");
  return data;
}

function setBusy(selector, busy) {
  const button = $(selector);
  if (!button) return;
  button.disabled = busy;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 3800);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

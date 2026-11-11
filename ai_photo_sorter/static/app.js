const state = {
  reviewRows: [],
  pollTimer: null,
  pollMisses: 0,
  activeScanJobId: null,
  setupJobId: null,
};

const DEFAULT_CONFIDENCE_THRESHOLD = 0.75;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

document.addEventListener("DOMContentLoaded", () => {
  bindTabs();
  bindControls();
  refreshCategories();
  refreshAiStatus();
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

  $$("input[name='fileModeChoice']").forEach((radio) => {
    radio.addEventListener("change", () => {
      $("#fileMode").value = radio.value;
      $("#fileModeHint").textContent = radio.value === "move"
        ? "Move puts the originals in the output folder."
        : "Copy leaves your originals untouched.";
    });
  });

  $("#scanImages").addEventListener("click", startScan);
  $("#cancelScan").addEventListener("click", cancelScan);
  $("#approveHigh").addEventListener("click", approveHighConfidence);
  $("#sendLowReview").addEventListener("click", sendLowConfidenceToReview);
  $("#applyChanges").addEventListener("click", applyChanges);
  $("#setupAi").addEventListener("click", setUpLocalAi);
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

/* Local AI status */

function setAiChip(kind, text, showSetup) {
  const chip = $("#aiChip");
  chip.classList.remove("checking", "ready", "missing", "working");
  chip.classList.add(kind);
  $("#aiChipText").textContent = text;
  $("#setupAi").classList.toggle("hidden", !showSetup);
}

async function refreshAiStatus() {
  if (state.setupJobId) return;
  try {
    const status = await postJson("/api/test-ai", {});
    if (status.ok) {
      setAiChip("ready", "Local AI ready", false);
    } else {
      setAiChip("missing", "Local AI not set up", true);
    }
  } catch {
    setAiChip("missing", "Local AI not set up", true);
  }
}

async function setUpLocalAi() {
  if (state.setupJobId) return;
  setBusy("#setupAi", true);
  try {
    const response = await postJson("/api/prepare-local-ai", {});
    state.setupJobId = response.job_id;
    setAiChip("working", "Preparing local AI…", false);
    pollSetupJob(response.job_id);
  } catch (error) {
    setBusy("#setupAi", false);
    showToast(error.message);
  }
}

function pollSetupJob(jobId) {
  setTimeout(async () => {
    let job;
    try {
      const response = await fetch(`/api/jobs/${jobId}`);
      job = await response.json();
    } catch {
      pollSetupJob(jobId);
      return;
    }
    if (job.status === "done") {
      state.setupJobId = null;
      setBusy("#setupAi", false);
      showToast("Local AI is ready.");
      refreshAiStatus();
      return;
    }
    if (job.status === "error" || !job.ok) {
      state.setupJobId = null;
      setBusy("#setupAi", false);
      setAiChip("missing", "Local AI setup failed", true);
      showToast(job.error || "Local AI setup failed.");
      return;
    }
    setAiChip("working", job.message || "Preparing local AI…", false);
    pollSetupJob(jobId);
  }, 900);
}

/* Scanning */

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
    state.pollMisses = 0;
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
  setProgress(currentProgressPercent(), "Stopping scan…");
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
    let job;
    try {
      const response = await fetch(`/api/jobs/${jobId}`);
      job = await response.json();
      state.pollMisses = 0;
    } catch {
      // The backend keeps scanning even if one poll drops, so only give up
      // after several misses in a row.
      state.pollMisses += 1;
      if (state.pollMisses < 5) {
        pollJob(jobId);
      } else {
        state.activeScanJobId = null;
        setScanRunning(false);
        showToast("Lost contact with the app. Is it still running?");
      }
      return;
    }

    try {
      if (!job.ok) throw new Error(job.error || "Scan job not found.");
      const percent = job.total ? (job.done / job.total) * 100 : 0;
      setProgress(percent, job.message || "Scanning");
      if (job.status === "done") {
        state.reviewRows = job.results.map((row) => ({ ...row, approved: !row.needs_review && !row.already_sorted }));
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

/* Review table */

function renderReview(rows) {
  const body = $("#reviewBody");
  body.textContent = "";
  updateReviewStats(rows);
  if (!rows.length) {
    const row = document.createElement("tr");
    row.className = "empty-row";
    row.innerHTML = `
      <td colspan="8">
        <div class="empty-state">
          <strong>Nothing to review yet</strong>
          <span>Pick an input and output folder, then scan. Suggestions land here for you to check before anything is written.</span>
        </div>
      </td>`;
    body.appendChild(row);
    return;
  }

  rows.forEach((item, index) => {
    const percent = Math.round(Number(item.confidence || 0) * 100);
    const low = item.needs_review || Number(item.confidence || 0) < DEFAULT_CONFIDENCE_THRESHOLD;
    const row = document.createElement("tr");
    row.className = "review-row";
    row.dataset.index = String(index);
    row.style.animationDelay = `${Math.min(index * 25, 500)}ms`;
    row.innerHTML = `
      <td><input class="approve-input" type="checkbox" ${item.approved ? "checked" : ""}></td>
      <td><img class="thumb" src="${item.thumbnail_url}" alt="" loading="lazy"></td>
      <td class="filename-cell">${escapeHtml(item.original_filename || "")}${
        item.already_sorted
          ? `<span class="sorted-badge" title="${escapeAttr(item.current_path || "")}">Already sorted</span>`
          : ""
      }</td>
      <td><input class="new-name-input" type="text" value="${escapeAttr(item.new_filename || "")}"></td>
      <td><input class="folder-input" type="text" value="${escapeAttr(item.suggested_folder || "")}"></td>
      <td><input class="caption-input" type="text" value="${escapeAttr(item.caption || "")}"></td>
      <td><input class="tags-input" type="text" value="${escapeAttr((item.tags || []).join(", "))}"></td>
      <td>
        <div class="confidence ${low ? "low" : ""}">
          <div class="conf-track"><div class="conf-fill" style="width:${percent}%"></div></div>
          <span>${percent}%</span>
        </div>
      </td>
    `;
    body.appendChild(row);
  });
}

function updateReviewStats(rows) {
  if (!rows.length) {
    $("#reviewStats").textContent = "";
    return;
  }
  const needsReview = rows.filter((row) => row.needs_review).length;
  const parts = [`${rows.length} image${rows.length === 1 ? "" : "s"}`];
  if (needsReview) parts.push(`${needsReview} need${needsReview === 1 ? "s" : ""} review`);
  const alreadySorted = rows.filter((row) => row.already_sorted).length;
  if (alreadySorted) parts.push(`${alreadySorted} already sorted`);
  $("#reviewStats").textContent = parts.join(" · ");
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
  const threshold = DEFAULT_CONFIDENCE_THRESHOLD;
  state.reviewRows = rowsFromReviewTable().map((row) => ({
    ...row,
    approved: Number(row.confidence || 0) >= threshold && !row.needs_review && !row.already_sorted,
  }));
  renderReview(state.reviewRows);
}

function sendLowConfidenceToReview() {
  const threshold = DEFAULT_CONFIDENCE_THRESHOLD;
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
    const applied = response.applied.length;
    const failed = (response.skipped || []).filter(
      (item) => item.reason !== "Not approved" && !item.reason.startsWith("Already sorted")
    );
    const alreadySorted = (response.skipped || []).filter((item) => item.reason.startsWith("Already sorted")).length;
    let message = `Applied ${applied} image${applied === 1 ? "" : "s"}.`;
    if (alreadySorted) message += ` ${alreadySorted} already sorted, left as is.`;
    if (failed.length) message += ` ${failed.length} skipped: ${failed[0].reason}`;
    showToast(message);
    refreshCategories();
  } catch (error) {
    showToast(error.message);
  } finally {
    setBusy("#applyChanges", false);
  }
}

/* Search */

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
      ? `Searched for: ${response.interpreted_query}`
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
    shell.innerHTML = `
      <div class="empty-state wide">
        <strong>No matches</strong>
        <span>Try fewer words, or clear the category filter.</span>
      </div>`;
    return;
  }

  results.forEach((item) => {
    const card = document.createElement("article");
    card.className = "result-card";
    const path = item.current_path || item.original_path || "";
    card.innerHTML = `
      <img src="${item.thumbnail_url}" alt="" loading="lazy">
      <div>
        <h3>${escapeHtml(item.new_filename || item.original_filename || "")}</h3>
        <p>${escapeHtml(item.caption || "")}</p>
        <div class="tag-row">${(item.tags || []).slice(0, 6).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</div>
        <button class="link-button" type="button">Open folder</button>
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

/* Helpers */

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(`Server error (${response.status}). Check the app's terminal output.`);
  }
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

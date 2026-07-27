"use strict";

let dashboardState = null;
let profitLossReports = [];
let activeProfitLossIndex = 0;

const byId = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function money(value, currency = "USD") {
  const amount = Number(value ?? 0);

  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
  }).format(amount);
}

function formatDate(value) {
  if (!value) {
    return "—";
  }

  return new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "short",
    day: "2-digit",
    timeZone: "UTC",
  }).format(new Date(`${value}T00:00:00Z`));
}

function titleCase(value) {
  return String(value ?? "unclassified")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function showToast(message, error = false) {
  const toast = byId("toast");

  toast.textContent = message;
  toast.classList.toggle("error", error);
  toast.classList.add("visible");

  window.setTimeout(() => {
    toast.classList.remove("visible");
  }, 3500);
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  let payload;

  try {
    payload = await response.json();
  } catch {
    payload = {
      detail: `Request failed with HTTP ${response.status}`,
    };
  }

  if (!response.ok) {
    const detail =
      typeof payload.detail === "string"
        ? payload.detail
        : payload.detail?.message ??
          payload.message ??
          JSON.stringify(payload.detail ?? payload);

    throw new Error(detail);
  }

  return payload;
}

function setBusy(button, busy, label = "Working…") {
  if (!button.dataset.originalLabel) {
    button.dataset.originalLabel = button.textContent.trim();
  }

  button.disabled = busy;
  button.textContent = busy
    ? label
    : button.dataset.originalLabel;
}

function activateSection(sectionId) {
  document.querySelectorAll(".page-section").forEach((section) => {
    section.classList.toggle(
      "active",
      section.id === sectionId,
    );
  });

  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle(
      "active",
      item.dataset.target === sectionId,
    );
  });

  window.scrollTo({
    top: 0,
    behavior: "smooth",
  });
}

function setupNavigation() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => {
      activateSection(button.dataset.target);
    });
  });

  document.querySelectorAll("[data-jump]").forEach((button) => {
    button.addEventListener("click", () => {
      activateSection(button.dataset.jump);
    });
  });
}

async function refreshState() {
  const refreshButton = byId("refresh-button");

  setBusy(refreshButton, true, "Refreshing…");

  try {
    dashboardState = await requestJson("/ui/api/state");
    renderDashboardState();
  } catch (error) {
    byId("mongo-status").textContent = "Data unavailable";
    byId("mongo-status").classList.remove("success");
    showToast(error.message, true);
  } finally {
    setBusy(refreshButton, false);
  }
}

function renderDashboardState() {
  const counts = dashboardState.counts;
  const syncCounts = dashboardState.sync_counts;

  byId("metric-raw").textContent = counts.raw_records;
  byId("metric-canonical").textContent =
    counts.canonical_transactions;
  byId("metric-classified").textContent =
    counts.classifications;
  byId("metric-synced").textContent =
    syncCounts.succeeded ?? 0;

  const mongoStatus = byId("mongo-status");
  mongoStatus.innerHTML = "<i></i> MongoDB connected";
  mongoStatus.classList.add("success");

  const geminiStatus = byId("gemini-status");
  geminiStatus.innerHTML = `<i></i> Gemini ${
    dashboardState.gemini_enabled ? "enabled" : "disabled"
  }`;
  geminiStatus.classList.toggle(
    "success",
    dashboardState.gemini_enabled,
  );

  byId("qbo-connection").textContent =
    counts.quickbooks_connections > 0
      ? "Connected"
      : "Not connected";

  renderPipeline();
  renderTransactions();
  renderReviewQueue();
}

function renderPipeline() {
  const counts = dashboardState.counts;
  const syncCounts = dashboardState.sync_counts;

  const steps = [
    {
      title: "Source ingestion",
      detail: `${counts.raw_records} raw rows preserved`,
      complete: counts.raw_records > 0,
    },
    {
      title: "Normalization and duplicates",
      detail:
        `${counts.canonical_transactions} canonical · ` +
        `${counts.duplicates} duplicates · ${counts.invalid} invalid`,
      complete: counts.normalized_transactions > 0,
    },
    {
      title: "Classification and review",
      detail:
        `${counts.classifications} classified · ` +
        `${counts.approved} approved`,
      complete:
        counts.classifications > 0 &&
        counts.pending_review === 0,
    },
    {
      title: "QuickBooks synchronization",
      detail:
        `${syncCounts.succeeded ?? 0} succeeded · ` +
        `${syncCounts.permanent_error ?? 0} permanent errors`,
      complete: dashboardState.sync_complete,
    },
    {
      title: "P&L reconciliation",
      detail: "April, May, June, and consolidated period",
      complete: dashboardState.sync_complete,
    },
  ];

  byId("pipeline-list").innerHTML = steps
    .map(
      (step) => `
        <div class="pipeline-step">
          <div class="step-icon">
            ${step.complete ? "✓" : "•"}
          </div>
          <div>
            <strong>${escapeHtml(step.title)}</strong>
            <span>${escapeHtml(step.detail)}</span>
          </div>
          <small>${step.complete ? "Complete" : "Pending"}</small>
        </div>
      `,
    )
    .join("");
}

function transactionMatchesFilters(transaction) {
  const search = byId("transaction-search")
    .value.trim()
    .toLowerCase();
  const statusFilter = byId(
    "transaction-status-filter",
  ).value;

  const searchable = [
    transaction.description,
    transaction.bank_account,
    transaction.classification?.transaction_type,
    transaction.classification?.account_number,
    transaction.classification?.account_name,
    transaction.classification?.counterparty,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();

  if (search && !searchable.includes(search)) {
    return false;
  }

  if (!statusFilter) {
    return true;
  }

  if (statusFilter === "duplicate") {
    return transaction.duplicate;
  }

  if (statusFilter === "invalid") {
    return transaction.record_status === "invalid";
  }

  return (
    transaction.classification?.review_status === statusFilter
  );
}

function renderTransactions() {
  const transactions = dashboardState.transactions.filter(
    transactionMatchesFilters,
  );

  if (!transactions.length) {
    byId("transaction-table").innerHTML = `
      <tr>
        <td colspan="7" class="empty-cell">
          No transactions match the current filters.
        </td>
      </tr>
    `;
    return;
  }

  byId("transaction-table").innerHTML = transactions
    .map((transaction) => {
      const classification = transaction.classification;
      const amountClass =
        Number(transaction.amount ?? 0) >= 0
          ? "amount-positive"
          : "amount-negative";
      const status =
        classification?.review_status ??
        (transaction.duplicate
          ? "duplicate"
          : transaction.record_status);

      return `
        <tr>
          <td>${escapeHtml(formatDate(transaction.transaction_date))}</td>
          <td class="row-description">
            <strong>${escapeHtml(transaction.description)}</strong>
            <span>
              ${escapeHtml(transaction.bank_account ?? "No bank account")}
            </span>
          </td>
          <td class="${amountClass}">
            ${escapeHtml(
              money(
                transaction.amount,
                transaction.currency ?? "USD",
              ),
            )}
          </td>
          <td>
            ${escapeHtml(
              titleCase(classification?.transaction_type),
            )}
          </td>
          <td>
            ${
              classification
                ? `${escapeHtml(classification.account_number)}
                   · ${escapeHtml(classification.account_name)}`
                : "—"
            }
          </td>
          <td>
            ${
              classification
                ? `${escapeHtml(classification.confidence)} ·
                   ${escapeHtml(titleCase(classification.source))}`
                : "—"
            }
          </td>
          <td>
            <span class="status-pill">
              ${escapeHtml(titleCase(status))}
            </span>
          </td>
        </tr>
      `;
    })
    .join("");
}

function renderReviewQueue() {
  const items = dashboardState.review_queue;

  if (!items.length) {
    byId("review-list").innerHTML = `
      <div class="empty-state">
        <strong>Review queue is clear.</strong>
        <p>
          All existing BrightFix classifications have a final
          accounting decision.
        </p>
      </div>
    `;
    return;
  }

  byId("review-list").innerHTML = items
    .map((item) => {
      const transaction = item.transaction;
      const classification = item.classification;
      const decision = classification.decision;

      return `
        <article class="review-item">
          <div class="review-main">
            <h4>${escapeHtml(
              transaction.description_original ??
                transaction.description_normalized,
            )}</h4>
            <div class="review-meta">
              <span>${escapeHtml(
                formatDate(transaction.transaction_date),
              )}</span>
              <span>${escapeHtml(
                money(
                  transaction.amount,
                  transaction.currency ?? "USD",
                ),
              )}</span>
              <span>${escapeHtml(
                titleCase(decision.transaction_type),
              )}</span>
              <span>
                ${escapeHtml(decision.qbo_account.account_number)}
                ·
                ${escapeHtml(decision.qbo_account.account_name)}
              </span>
              <span>
                Confidence ${escapeHtml(decision.confidence_score)}
              </span>
              <span>${escapeHtml(titleCase(decision.source))}</span>
            </div>
            <p class="review-explanation">
              ${escapeHtml(decision.explanation)}
            </p>
          </div>

          <div class="review-actions">
            <button
              class="action-approve"
              data-review-action="approved"
              data-id="${escapeHtml(transaction.id)}"
              data-version="${escapeHtml(classification.version)}"
            >
              Approve
            </button>
            <button
              class="action-correct"
              data-correct-id="${escapeHtml(transaction.id)}"
              data-version="${escapeHtml(classification.version)}"
              data-type="${escapeHtml(decision.transaction_type)}"
              data-account="${escapeHtml(
                decision.qbo_account.account_number,
              )}"
              data-counterparty="${escapeHtml(
                decision.counterparty?.normalized_name ?? "",
              )}"
            >
              Correct
            </button>
            <button
              class="action-reject"
              data-review-action="rejected"
              data-id="${escapeHtml(transaction.id)}"
              data-version="${escapeHtml(classification.version)}"
            >
              Reject
            </button>
          </div>
        </article>
      `;
    })
    .join("");
}

async function submitReview(button) {
  const reviewerId = byId("reviewer-id").value.trim();

  if (!reviewerId) {
    showToast("Reviewer name is required.", true);
    return;
  }

  button.disabled = true;

  try {
    await requestJson(
      `/api/v1/classification/${button.dataset.id}/review`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          expected_version: Number(button.dataset.version),
          outcome: button.dataset.reviewAction,
          reviewer_id: reviewerId,
          notes: "Reviewed from the Finz challenge interface.",
        }),
      },
    );

    showToast(
      `Classification ${button.dataset.reviewAction}.`,
    );
    await refreshState();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function openCorrection(button) {
  byId("correction-id").value =
    button.dataset.correctId;
  byId("correction-version").value =
    button.dataset.version;
  byId("correction-type").value =
    button.dataset.type;
  byId("correction-account").value =
    button.dataset.account;
  byId("correction-counterparty").value =
    button.dataset.counterparty;

  byId("correction-dialog").showModal();
}

async function submitCorrection(event) {
  event.preventDefault();

  const reviewerId = byId("reviewer-id").value.trim();

  if (!reviewerId) {
    showToast("Reviewer name is required.", true);
    return;
  }

  const transactionId = byId("correction-id").value;

  try {
    await requestJson(
      `/api/v1/classification/${transactionId}/correction`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          expected_version: Number(
            byId("correction-version").value,
          ),
          corrected_transaction_type:
            byId("correction-type").value,
          corrected_account_number:
            byId("correction-account").value,
          corrected_counterparty_name:
            byId("correction-counterparty").value.trim() ||
            null,
          reviewer_id: reviewerId,
          reason: byId("correction-reason").value.trim(),
          notes: "Corrected from the Finz challenge interface.",
        }),
      },
    );

    byId("correction-dialog").close();
    showToast("Classification correction saved.");
    await refreshState();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function inspectUpload() {
  const file = byId("source-file").files[0];
  const button = byId("inspect-button");

  if (!file) {
    showToast("Choose a CSV or XLSX file first.", true);
    return;
  }

  const formData = new FormData();
  formData.append("file", file);

  setBusy(button, true, "Inspecting…");
  byId("inspect-status").textContent = "Reading headers…";

  try {
    const payload = await requestJson(
      "/api/v1/ingestion/inspect",
      {
        method: "POST",
        body: formData,
      },
    );

    byId("inspection-output").textContent =
      JSON.stringify(payload, null, 2);
    byId("inspect-status").textContent =
      "Inspection passed";
    showToast("Source inspection passed.");
  } catch (error) {
    byId("inspection-output").textContent = error.message;
    byId("inspect-status").textContent =
      "Inspection failed";
    showToast(error.message, true);
  } finally {
    setBusy(button, false);
  }
}

async function processUpload() {
  const file = byId("source-file").files[0];
  const button = byId("process-button");

  if (!file) {
    showToast("Choose a CSV or XLSX file first.", true);
    return;
  }

  let config;

  try {
    config = JSON.parse(byId("mapping-config").value);
  } catch {
    showToast("The column mapping is not valid JSON.", true);
    return;
  }

  const formData = new FormData();
  formData.append("file", file);
  formData.append("config_json", JSON.stringify(config));

  setBusy(button, true, "Processing…");
  byId("process-status").textContent =
    "Normalizing source data…";

  try {
    const ingestion = await requestJson(
      "/api/v1/ingestion/process",
      {
        method: "POST",
        body: formData,
      },
    );

    byId("process-status").textContent =
      "Classifying transactions…";

    const classification = await requestJson(
      `/api/v1/classification/uploads/${ingestion.upload_id}/classify`,
      {
        method: "POST",
      },
    );

    byId("process-output").textContent =
      JSON.stringify(
        {
          ingestion,
          classification,
        },
        null,
        2,
      );

    byId("process-status").textContent =
      "Upload completed";
    showToast("Upload processed and classified.");
    await refreshState();
  } catch (error) {
    byId("process-output").textContent = error.message;
    byId("process-status").textContent =
      "Processing failed";
    showToast(error.message, true);
  } finally {
    setBusy(button, false);
  }
}

async function loadProfitLoss() {
  const button = byId("load-pnl-button");
  const start = byId("pnl-start").value;
  const end = byId("pnl-end").value;

  setBusy(button, true, "Loading…");

  try {
    const payload = await requestJson(
      `/api/v1/reports/profit-and-loss?` +
        new URLSearchParams({
          start_date: start,
          end_date: end,
          currency: "USD",
        }),
    );

    profitLossReports = [
      ...payload.monthly,
      payload.consolidated,
    ];
    activeProfitLossIndex =
      profitLossReports.length - 1;

    byId("overview-net-profit").textContent = money(
      payload.consolidated.net_profit,
      payload.consolidated.currency,
    );
    byId("overview-pnl-caption").textContent =
      `${formatDate(payload.consolidated.start_date)}–` +
      `${formatDate(payload.consolidated.end_date)} · ` +
      "calculated from approved canonical transactions.";

    renderProfitLossTabs();
    renderProfitLoss();
  } catch (error) {
    byId("pnl-report").innerHTML = `
      <article class="panel">
        ${escapeHtml(error.message)}
      </article>
    `;
    showToast(error.message, true);
  } finally {
    setBusy(button, false);
  }
}

function renderProfitLossTabs() {
  byId("pnl-tabs").innerHTML = profitLossReports
    .map((report, index) => {
      const label =
        index === profitLossReports.length - 1
          ? "Consolidated"
          : new Intl.DateTimeFormat("en-US", {
              month: "long",
              year: "numeric",
              timeZone: "UTC",
            }).format(
              new Date(`${report.start_date}T00:00:00Z`),
            );

      return `
        <button
          class="period-tab ${
            index === activeProfitLossIndex ? "active" : ""
          }"
          data-pnl-index="${index}"
        >
          ${escapeHtml(label)}
        </button>
      `;
    })
    .join("");
}

function renderProfitLoss() {
  const report =
    profitLossReports[activeProfitLossIndex];

  if (!report) {
    return;
  }

  const accountSections = [
    {
      label: "Revenue",
      accounts: report.revenue_accounts,
    },
    {
      label: "Cost of Goods Sold",
      accounts: report.cost_of_goods_sold_accounts,
    },
    {
      label: "Operating Expenses",
      accounts: report.operating_expense_accounts,
    },
  ];

  const rows = accountSections
    .flatMap((section) => [
      `
        <tr>
          <th colspan="3">${escapeHtml(section.label)}</th>
        </tr>
      `,
      ...section.accounts.map(
        (account) => `
          <tr>
            <td>${escapeHtml(account.account_number)}</td>
            <td>
              <details class="account-detail">
                <summary>${escapeHtml(account.account_name)}</summary>
                <div class="account-transactions">
                  ${account.transactions
                    .map(
                      (transaction) => `
                        ${escapeHtml(
                          formatDate(transaction.transaction_date),
                        )}
                        ·
                        ${escapeHtml(transaction.description)}
                        ·
                        ${escapeHtml(
                          money(
                            transaction.report_amount,
                            transaction.currency,
                          ),
                        )}
                        <br>
                      `,
                    )
                    .join("")}
                </div>
              </details>
            </td>
            <td>${escapeHtml(
              money(account.total, report.currency),
            )}</td>
          </tr>
        `,
      ),
    ])
    .join("");

  byId("pnl-report").innerHTML = `
    <div class="pnl-summary">
      <article>
        <span>Total revenue</span>
        <strong>${escapeHtml(
          money(report.total_revenue, report.currency),
        )}</strong>
      </article>
      <article>
        <span>Gross profit</span>
        <strong>${escapeHtml(
          money(report.gross_profit, report.currency),
        )}</strong>
      </article>
      <article>
        <span>Operating expenses</span>
        <strong>${escapeHtml(
          money(
            report.total_operating_expenses,
            report.currency,
          ),
        )}</strong>
      </article>
      <article>
        <span>Net profit</span>
        <strong>${escapeHtml(
          money(report.net_profit, report.currency),
        )}</strong>
      </article>
    </div>

    <article class="panel table-panel">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Account</th>
              <th>P&amp;L line and transactions</th>
              <th>Total</th>
            </tr>
          </thead>
          <tbody>
            ${rows}
            <tr>
              <th colspan="2">Net profit</th>
              <th>${escapeHtml(
                money(report.net_profit, report.currency),
              )}</th>
            </tr>
          </tbody>
        </table>
      </div>
    </article>
  `;
}

async function runSync() {
  const button = byId("sync-button");
  const confirmation =
    byId("sync-confirmation").value.trim();

  if (confirmation !== "BRIGHTFIX-SANDBOX-LIVE-SYNC") {
    showToast(
      "Enter the exact sandbox confirmation phrase.",
      true,
    );
    return;
  }

  const formData = new FormData();
  formData.append("confirmation", confirmation);

  setBusy(button, true, "Synchronizing…");
  byId("sync-output").textContent =
    "Running guarded QuickBooks synchronization…";

  try {
    const result = await requestJson("/ui/api/sync", {
      method: "POST",
      body: formData,
    });

    byId("sync-output").textContent = result.output;

    if (!result.success) {
      throw new Error(
        `QuickBooks sync exited with code ${result.exit_code}`,
      );
    }

    showToast("QuickBooks synchronization passed.");
    await refreshState();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setBusy(button, false);
  }
}

async function runReconciliation() {
  const button = byId("reconcile-button");

  setBusy(button, true, "Reconciling…");
  byId("reconciliation-output").textContent =
    "Retrieving QuickBooks cash-basis reports…";

  try {
    const result = await requestJson(
      "/ui/api/reconcile",
      {
        method: "POST",
      },
    );

    byId("reconciliation-output").textContent =
      result.output;

    if (!result.success) {
      throw new Error(
        `Reconciliation exited with code ${result.exit_code}`,
      );
    }

    byId("reconciliation-icon").textContent = "✓";
    byId("reconciliation-status").textContent =
      "Live reconciliation passed";
    byId("reconciliation-caption").textContent =
      "April, May, June, and the consolidated quarter " +
      "matched every controlled account and required total.";
    showToast("QuickBooks reconciliation passed.");
  } catch (error) {
    byId("reconciliation-icon").textContent = "!";
    byId("reconciliation-status").textContent =
      "Live reconciliation failed";
    byId("reconciliation-caption").textContent =
      "Review the output below; no passing result is claimed.";
    showToast(error.message, true);
  } finally {
    setBusy(button, false);
  }
}

function setupEvents() {
  byId("refresh-button").addEventListener(
    "click",
    refreshState,
  );
  byId("inspect-button").addEventListener(
    "click",
    inspectUpload,
  );
  byId("process-button").addEventListener(
    "click",
    processUpload,
  );
  byId("load-pnl-button").addEventListener(
    "click",
    loadProfitLoss,
  );
  byId("sync-button").addEventListener(
    "click",
    runSync,
  );
  byId("reconcile-button").addEventListener(
    "click",
    runReconciliation,
  );

  byId("transaction-search").addEventListener(
    "input",
    renderTransactions,
  );
  byId("transaction-status-filter").addEventListener(
    "change",
    renderTransactions,
  );

  byId("review-list").addEventListener(
    "click",
    (event) => {
      const reviewButton = event.target.closest(
        "[data-review-action]",
      );
      const correctionButton = event.target.closest(
        "[data-correct-id]",
      );

      if (reviewButton) {
        submitReview(reviewButton);
      }

      if (correctionButton) {
        openCorrection(correctionButton);
      }
    },
  );

  byId("pnl-tabs").addEventListener(
    "click",
    (event) => {
      const button = event.target.closest(
        "[data-pnl-index]",
      );

      if (!button) {
        return;
      }

      activeProfitLossIndex = Number(
        button.dataset.pnlIndex,
      );
      renderProfitLossTabs();
      renderProfitLoss();
    },
  );

  byId("correction-form").addEventListener(
    "submit",
    submitCorrection,
  );

  for (const buttonId of [
    "close-correction",
    "cancel-correction",
  ]) {
    byId(buttonId).addEventListener("click", () => {
      byId("correction-dialog").close();
    });
  }
}

async function start() {
  setupNavigation();
  setupEvents();

  await Promise.all([
    refreshState(),
    loadProfitLoss(),
  ]);
}

document.addEventListener("DOMContentLoaded", start);

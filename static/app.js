/**
 * Tip dashboard frontend — vanilla JS.
 * Fetches Clover-backed payment summaries and posts shifts for allocation.
 */

const EMPLOYEES = window.__EMPLOYEES__ || [];

/** DOM-safe fragment for employee name (used in class names). */
function safeEmp(name) {
  return name.replace(/[^a-zA-Z0-9]/g, "_");
}

/** Last Clover /api/payments response (for manual payment picker). */
let lastTippedPayments = [];

/** Today in local calendar as YYYY-MM-DD (not UTC date). */
function todayLocalISO() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** Local calendar week Mon–Sun: YYYY-MM-DD of the Monday containing this date. */
function mondayOfWeekContaining(isoYmd) {
  if (!isoYmd || !/^\d{4}-\d{2}-\d{2}$/.test(isoYmd)) return isoYmd;
  const [y, m, d] = isoYmd.split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  const dow = dt.getDay();
  const delta = dow === 0 ? -6 : 1 - dow;
  dt.setDate(dt.getDate() + delta);
  const yy = dt.getFullYear();
  const mm = String(dt.getMonth() + 1).padStart(2, "0");
  const dd = String(dt.getDate()).padStart(2, "0");
  return `${yy}-${mm}-${dd}`;
}

const el = (id) => document.getElementById(id);

/** Must match backend ``TIME_GRID_MINUTES`` (15). */
const SHIFT_SLOT_MINUTES = [0, 15, 30, 45];

/**
 * Dropdown for shift start/end — only 15-minute marks (no native time picker gaps).
 * ``selectedRaw`` is ``HH:MM``; invalid or off-grid values show as unset (—).
 */
function shiftTimeSelectHtml(className, selectedRaw) {
  const sel = (selectedRaw || "").trim();
  const parts = sel.split(":");
  const h = parts.length === 2 ? parseInt(parts[0], 10) : NaN;
  const m = parts.length === 2 ? parseInt(parts[1], 10) : NaN;
  const valid =
    !Number.isNaN(h) &&
    !Number.isNaN(m) &&
    h >= 0 &&
    h <= 23 &&
    SHIFT_SLOT_MINUTES.includes(m);
  const effective = valid
    ? `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`
    : "";

  let opts = '<option value="">—</option>';
  for (let hour = 0; hour < 24; hour++) {
    for (const min of SHIFT_SLOT_MINUTES) {
      const hh = String(hour).padStart(2, "0");
      const mm = String(min).padStart(2, "0");
      const v = `${hh}:${mm}`;
      opts += `<option value="${v}"${v === effective ? " selected" : ""}>${v}</option>`;
    }
  }
  return `<select class="${className} shift-time-select">${opts}</select>`;
}

function showBanner(which, message) {
  const err = el("global-error");
  const info = el("global-info");
  if (err) err.classList.add("hidden");
  if (info) info.classList.add("hidden");
  if (!message) return;
  const target = which === "error" ? err : info;
  if (!target) return;
  target.textContent = message;
  target.classList.remove("hidden");
}

function clearResults() {
  el("summary-section")?.classList.add("hidden");
  el("recon-section")?.classList.add("hidden");
  el("employees-section")?.classList.add("hidden");
  el("tx-section")?.classList.add("hidden");
  const be = el("btn-export-employees");
  const bt = el("btn-export-tx");
  if (be) be.disabled = true;
  if (bt) bt.disabled = true;
}

/** Collect shifts from the DOM into API shape. */
function gatherShifts() {
  const shifts = {};
  for (const name of EMPLOYEES) {
    const safe = name.replace(/[^a-zA-Z0-9]/g, "_");
    const container = el(`shifts-${safe}`);
    if (!container) continue;
    const blocks = [];
    container.querySelectorAll(".shift-row").forEach((row) => {
      const s = row.querySelector(".inp-start")?.value?.trim() || "";
      const e = row.querySelector(".inp-end")?.value?.trim() || "";
      if (s && e) blocks.push({ start: s, end: e });
    });
    shifts[name] = blocks;
  }
  return shifts;
}

/** Ensure every employee key exists for API payloads. */
function gatherShiftsFull() {
  const s = gatherShifts();
  const o = {};
  for (const n of EMPLOYEES) o[n] = s[n] || [];
  return o;
}

/** Add one shift row for an employee. */
function addShiftRow(employeeName, startVal = "", endVal = "") {
  const safe = employeeName.replace(/[^a-zA-Z0-9]/g, "_");
  const container = el(`shifts-${safe}`);
  if (!container) return;

  const row = document.createElement("div");
  row.className = "shift-row";
  row.innerHTML = `
    <label class="field"><span class="label">Start</span>
      ${shiftTimeSelectHtml("inp-start", startVal)}
    </label>
    <label class="field"><span class="label">End</span>
      ${shiftTimeSelectHtml("inp-end", endVal)}
    </label>
    <button type="button" class="btn btn-small shift-remove">Remove</button>
  `;
  row.querySelector(".shift-remove").addEventListener("click", () => {
    row.remove();
    if (!container.querySelector(".shift-row")) addShiftRow(employeeName);
  });
  container.appendChild(row);
}

function buildShiftPanels() {
  const host = el("shift-panels");
  host.innerHTML = "";
  for (const name of EMPLOYEES) {
    const safe = name.replace(/[^a-zA-Z0-9]/g, "_");
    const wrap = document.createElement("div");
    wrap.className = "shift-employee";
    wrap.innerHTML = `
      <h3>${name}</h3>
      <div id="shifts-${safe}" class="shift-block-list"></div>
      <button type="button" class="btn btn-small btn-add-block" data-emp="${name}">+ Add block</button>
    `;
    host.appendChild(wrap);
    addShiftRow(name);
    wrap.querySelector(".btn-add-block").addEventListener("click", () => {
      addShiftRow(name);
    });
  }
}

function paymentOptionsHtml() {
  if (!lastTippedPayments.length) {
    return '<option value="">— No tipped payments (refresh Clover for this date) —</option>';
  }
  const opts = lastTippedPayments.map(
    (p) =>
      `<option value="${p.payment_id}">$${(p.tip_amount_cents / 100).toFixed(2)} tip · ${p.payment_id} · ${p.created_at_local_iso}</option>`
  );
  return '<option value="">Select payment…</option>' + opts.join("");
}

/** Re-fill payment dropdowns after a new Clover pull (keep selection if still present). */
function repopulateManualPaymentSelects() {
  const html = paymentOptionsHtml();
  document.querySelectorAll(".manual-pay-select").forEach((sel) => {
    const cur = sel.value;
    sel.innerHTML = html;
    if (cur && [...sel.options].some((o) => o.value === cur)) sel.value = cur;
  });
  const hint = el("manual-refresh-hint");
  if (hint) hint.classList.toggle("hidden", lastTippedPayments.length > 0);
}

function addManualRuleRow() {
  const host = el("manual-rules-host");
  if (!host) return;
  const row = document.createElement("div");
  row.className = "manual-rule-row";
  const fracs = EMPLOYEES.map((name) => {
    const s = safeEmp(name);
    return `<label>${name}<input type="number" class="frac-inp frac-${s}" min="0" step="0.01" placeholder="0" /></label>`;
  }).join("");
  row.innerHTML = `
    <div class="manual-rule-head">
      <label class="field"><span class="label">Tipped payment</span>
        <select class="manual-pay-select">${paymentOptionsHtml()}</select>
      </label>
      <button type="button" class="btn btn-small manual-remove">Remove rule</button>
    </div>
    <div class="manual-fractions-grid">${fracs}</div>
  `;
  row.querySelector(".manual-remove").addEventListener("click", () => row.remove());
  host.appendChild(row);
}

/** Build manual_rules[] for the API. Rows without a selected payment are skipped. */
function gatherManualRules() {
  const rules = [];
  document.querySelectorAll(".manual-rule-row").forEach((row) => {
    const pid = row.querySelector(".manual-pay-select")?.value?.trim();
    if (!pid) return;
    const fractions = {};
    for (const name of EMPLOYEES) {
      const inp = row.querySelector(`.frac-${safeEmp(name)}`);
      const v = parseFloat(inp?.value);
      if (!Number.isNaN(v) && v > 0) fractions[name] = v;
    }
    rules.push({ payment_id: pid, fractions });
  });
  return rules;
}

function validateManualRulesLocal(rules) {
  for (const r of rules) {
    if (!r.fractions || Object.keys(r.fractions).length === 0) {
      return `Manual rule for payment ${r.payment_id}: enter at least one positive share (weight).`;
    }
  }
  return null;
}

async function refreshClover() {
  clearResults();
  showBanner("error", "");
  showBanner("info", "");
  const d = el("pick-date").value;
  if (!d) {
    showBanner("error", "Pick a date first.");
    return;
  }
  try {
    const res = await fetch(`/api/payments?date=${encodeURIComponent(d)}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showBanner("error", data.detail || res.statusText || "Failed to load Clover data.");
      lastTippedPayments = [];
      repopulateManualPaymentSelects();
      return;
    }
    lastTippedPayments = (data.payments || []).filter((p) => p.tip_amount_cents > 0);
    repopulateManualPaymentSelects();
    showBanner(
      "info",
      `Loaded ${data.count} payments (${data.count_with_tips} with tips). ` +
        `Sales $${data.total_sales_dollars?.toFixed?.(2) ?? data.total_sales_dollars} · ` +
        `Tips $${data.total_tips_dollars?.toFixed?.(2) ?? data.total_tips_dollars}.`
    );
  } catch (e) {
    showBanner("error", String(e));
  }
}

function renderSummary(result) {
  const grid = el("summary-cards");
  const metrics = [
    ["Selected date", result.date],
    ["Payments (all)", result.payments_count_all],
    ["Payments (with tips)", result.payments_count_with_tips],
    ["Tips: time-based rows", result.tip_transactions_time_based ?? "—"],
    ["Tips: manual rows", result.tip_transactions_manual ?? "—"],
    ["Total sales", `$${result.total_sales_dollars.toFixed(2)}`],
    ["Clover tips (day)", `$${result.clover_total_tips_dollars.toFixed(2)}`],
    ["Allocated tips", `$${result.allocated_employee_total_dollars.toFixed(2)}`],
    ["Unassigned tips", `$${result.unassigned_total_dollars.toFixed(2)}`],
    ["Reconciliation Δ", `$${result.reconciliation_difference_dollars.toFixed(2)}`],
  ];
  grid.innerHTML = metrics
    .map(
      ([k, v]) =>
        `<div class="metric"><div class="k">${k}</div><div class="v">${v}</div></div>`
    )
    .join("");
  el("summary-section").classList.remove("hidden");
}

function renderRecon(result) {
  const tbody = el("recon-table").querySelector("tbody");
  const rows = [
    ["Clover total tips (all payments, day)", `$${result.clover_total_tips_dollars.toFixed(2)}`, `(${result.clover_total_tips_cents} ¢)`],
    ["Tip pool (only payments with tip > 0)", `$${result.tip_pool_dollars.toFixed(2)}`, `(${result.tip_pool_cents} ¢)`],
    ["Sum allocated to employees", `$${result.allocated_employee_total_dollars.toFixed(2)}`, `(${result.allocated_employee_total_cents} ¢)`],
    ["Unassigned tips", `$${result.unassigned_total_dollars.toFixed(2)}`, `(${result.unassigned_total_cents} ¢)`],
    ["Allocated + unassigned", `$${result.allocated_plus_unassigned_dollars.toFixed(2)}`, `(${result.allocated_plus_unassigned_cents} ¢)`],
    ["Difference vs tip pool", `$${result.reconciliation_difference_dollars.toFixed(2)}`, `(${result.reconciliation_difference_cents} ¢)`],
  ];
  tbody.innerHTML = rows
    .map(
      ([label, a, b]) =>
        `<tr><td>${label}</td><td class="num">${a}</td><td class="num muted">${b}</td></tr>`
    )
    .join("");
  el("recon-section").classList.remove("hidden");
}

function renderEmployees(result) {
  const tbody = el("employees-table").querySelector("tbody");
  tbody.innerHTML = result.employees
    .map(
      (r) =>
        `<tr>
          <td>${r.employee}</td>
          <td class="num">$${r.allocated_tip_dollars.toFixed(2)}</td>
          <td class="num">${r.transactions_shared}</td>
          <td class="num">${r.scheduled_hours.toFixed(2)}</td>
        </tr>`
    )
    .join("");
  el("employees-section").classList.remove("hidden");
}

function formatSplitCents(map) {
  if (!map || Object.keys(map).length === 0) return "—";
  return Object.entries(map)
    .map(([n, c]) => `${n}: ${c}¢`)
    .join("; ");
}

function formatNormalizedShares(t) {
  const m = t.manual_fractions_normalized;
  if (!m || !Object.keys(m).length) return "";
  return Object.entries(m)
    .map(([k, v]) => `${k} ${(Number(v) * 100).toFixed(1)}%`)
    .join(" · ");
}

function renderTx(result) {
  const tbody = el("tx-table").querySelector("tbody");
  tbody.innerHTML = result.transactions
    .map((t) => {
      const un = t.unassigned ? '<span class="unassigned-yes">Yes</span>' : "No";
      const workers = t.employees_working.length ? t.employees_working.join(", ") : "—";
      const shareLine =
        t.allocation_mode === "manual" && formatNormalizedShares(t)
          ? `<br/><span class="muted">${formatNormalizedShares(t)}</span>`
          : "";
      const mode =
        t.allocation_mode === "manual"
          ? '<span class="mode-manual">manual</span>'
          : '<span class="mode-time">time</span>';
      return `<tr>
        <td>${mode}</td>
        <td>${t.created_at_local}</td>
        <td>${t.payment_id}</td>
        <td class="num">$${t.tip_amount_dollars.toFixed(2)}</td>
        <td>${workers}${shareLine}</td>
        <td class="num">${t.active_employee_count}</td>
        <td class="num">${t.unassigned ? "—" : t.per_person_tip_dollars.toFixed(2)}</td>
        <td>${formatSplitCents(t.per_person_split_cents)}</td>
        <td>${un}</td>
      </tr>`;
    })
    .join("");
  el("tx-section").classList.remove("hidden");
}

let lastCalculatePayload = null;
let lastPreviewData = null;

function buildCalculateBody(forDate) {
  return {
    date: forDate,
    shifts: gatherShiftsFull(),
    manual_rules: gatherManualRules(),
  };
}

function syncConfirmDateFromPicker() {
  const p = el("pick-date");
  const c = el("confirm-date");
  if (p && c) c.value = p.value;
}

function showSettingsSaved(msg) {
  const b = el("settings-saved");
  if (!b) return;
  b.textContent = msg || "";
  b.classList.toggle("hidden", !msg);
  if (msg) setTimeout(() => b.classList.add("hidden"), 4000);
}

function updateTestModeBanner() {
  const b = el("test-mode-banner");
  const cb = el("set-test-mode");
  if (b && cb) b.classList.toggle("hidden", !cb.checked);
}

function buildSettingsGrid() {
  const host = el("settings-employee-grid");
  if (!host) return;
  host.innerHTML = EMPLOYEES.map(
    (name) => `
    <label class="field settings-emp-field">
      <span class="label">${name}</span>
      <input type="email" id="set-email-${safeEmp(name)}" class="inp-wide" placeholder="(optional until go-live)" autocomplete="email" />
    </label>`
  ).join("");
}

async function loadSettings() {
  try {
    const res = await fetch("/api/settings");
    const data = await res.json();
    if (!res.ok) return;
    const mgr = el("set-manager-email");
    if (mgr) mgr.value = data.manager_email || "";
    const tm = el("set-test-mode");
    if (tm) tm.checked = !!data.test_mode;
    for (const row of data.employees || []) {
      const inp = el(`set-email-${safeEmp(row.employee_name)}`);
      if (inp) inp.value = row.employee_email || "";
    }
    updateTestModeBanner();

    const tzHint = el("server-tz-hint");
    if (tzHint && data.timezone_effective) {
      const eff = data.timezone_effective;
      const raw = data.timezone_env;
      if (raw) {
        tzHint.textContent = `Active zone: «${eff}» (APP_TIMEZONE=${raw}). Clover “day” range, tip times, and shift matching all use this zone.`;
      } else if (eff === "UTC") {
        tzHint.textContent = `Active zone: UTC. Set APP_TIMEZONE=America/New_York in .env or Render, restart the app, hard-refresh this page (Cmd+Shift+R).`;
      } else {
        tzHint.textContent = `Active zone: «${eff}» (no APP_TIMEZONE — using the machine’s default zone). On Render this is often UTC; set APP_TIMEZONE=America/New_York there.`;
      }
    }

    const smtpCard = el("smtp-not-ready");
    const smtpBody = el("smtp-not-ready-body");
    if (smtpCard && smtpBody) {
      const ready = !!data.smtp_ready;
      smtpCard.classList.toggle("hidden", ready);
      if (!ready) {
        const miss = Array.isArray(data.smtp_missing) ? data.smtp_missing.join(", ") : "";
        const hint = data.smtp_hint || "";
        smtpBody.textContent = miss
          ? `Missing: ${miss}. ${hint}`
          : hint || "Configure SMTP in .env and restart the server.";
      }
    }
  } catch (_) {
    /* ignore */
  }
}

async function saveSettings() {
  showSettingsSaved("");
  showBanner("error", "");
  const employees = {};
  for (const name of EMPLOYEES) {
    employees[name] = el(`set-email-${safeEmp(name)}`)?.value?.trim() || "";
  }
  const body = {
    manager_email: el("set-manager-email")?.value?.trim() || "",
    test_mode: !!el("set-test-mode")?.checked,
    employees,
  };
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showBanner("error", typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
      return;
    }
    showSettingsSaved(data.message || "Settings saved.");
    updateTestModeBanner();
  } catch (e) {
    showBanner("error", String(e));
  }
}

function renderConfirmPreview(data) {
  const host = el("confirm-preview-body");
  const a = data.allocation;
  const rows = data.preview_employees || [];
  let html = `<p><strong>Date:</strong> ${data.date}</p>`;
  html += `<p><strong>Employees with shifts:</strong> ${rows.length}</p>`;
  html += `<ul class="preview-list">`;
  for (const r of rows) {
    html += `<li><strong>${r.name}</strong> — ${r.shift_label_ampm} — <strong>${r.hours_worked.toFixed(2)}</strong> h — tips <strong>$${r.tip_allocated_dollars.toFixed(2)}</strong></li>`;
  }
  html += `</ul>`;
  html += `<p><strong>Total allocated tips:</strong> $${a.allocated_employee_total_dollars.toFixed(2)}<br/>`;
  html += `<strong>Total unassigned tips:</strong> $${a.unassigned_total_dollars.toFixed(2)}<br/>`;
  html += `<strong>Clover total tips:</strong> $${a.clover_total_tips_dollars.toFixed(2)}<br/>`;
  html += `<strong>Reconciliation difference:</strong> $${a.reconciliation_difference_dollars.toFixed(2)}</p>`;
  if (host) host.innerHTML = html;
  el("confirm-preview-section")?.classList.remove("hidden");
}

async function loadConfirmPreview() {
  showBanner("error", "");
  const cr = el("confirm-result");
  if (cr) cr.textContent = "";
  const d = el("confirm-date")?.value;
  if (!d) {
    showBanner("error", "Pick work date.");
    return;
  }
  const shifts = gatherShiftsFull();
  if (!Object.values(shifts).some((arr) => arr.length > 0)) {
    showBanner("error", "Add shifts on the Daily tab first.");
    return;
  }
  const manual_rules = gatherManualRules();
  const bad = validateManualRulesLocal(manual_rules);
  if (bad) {
    showBanner("error", bad);
    return;
  }
  const body = { date: d, shifts, manual_rules };
  try {
    const res = await fetch("/api/confirm/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showBanner("error", typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
      return;
    }
    lastPreviewData = data;
    renderConfirmPreview(data);
    showBanner("info", "Preview loaded.");
  } catch (e) {
    showBanner("error", String(e));
  }
}

function setConfirmResult(htmlOrText, asHtml) {
  const out = el("confirm-result");
  if (!out) return;
  if (asHtml) out.innerHTML = htmlOrText;
  else out.textContent = htmlOrText;
}

async function confirmSaveOnly(overwrite) {
  showBanner("error", "");
  const cd = el("confirm-date");
  if (!cd) {
    showBanner("error", "Missing work-date field. Hard-refresh the page (Cmd+Shift+R or Ctrl+Shift+R).");
    return;
  }
  const d = cd.value;
  if (!d) {
    showBanner("error", "Pick a work date on the Confirm tab.");
    setConfirmResult("Pick a work date first.", false);
    return;
  }

  const shifts = gatherShiftsFull();
  if (!Object.values(shifts).some((arr) => arr.length > 0)) {
    const msg = "No shift blocks on the Daily tab. Add shifts there, then try again.";
    showBanner("error", msg);
    setConfirmResult(msg, false);
    return;
  }

  const manual_rules = gatherManualRules();
  const bad = validateManualRulesLocal(manual_rules);
  if (bad) {
    showBanner("error", bad);
    setConfirmResult(bad, false);
    return;
  }

  const body = { date: d, shifts, manual_rules, overwrite: !!overwrite };
  setConfirmResult("Saving…", false);
  showBanner("info", "Saving confirmation to database…");

  try {
    const res = await fetch("/api/confirm/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    const det409 = data.detail;
    if (
      res.status === 409 &&
      det409 &&
      typeof det409 === "object" &&
      det409.requires_overwrite
    ) {
      const ok = window.confirm(
        (det409.message || "Already confirmed.") +
          "\n\nOverwrite saved data for this day? (You can send emails afterward.)"
      );
      if (ok) return confirmSaveOnly(true);
      setConfirmResult("Cancelled.", false);
      showBanner("info", "");
      return;
    }
    if (!res.ok) {
      const errText =
        typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || res.statusText);
      setConfirmResult(errText, false);
      showBanner("error", errText);
      return;
    }
    const lines = [data.message, "Next: click “Send emails” when you want messages to go out."];
    setConfirmResult(lines.map((l) => `<div>${l}</div>`).join(""), true);
    showBanner("info", "Day saved. Send emails when ready.");
  } catch (e) {
    const msg = String(e);
    setConfirmResult(msg, false);
    showBanner("error", msg);
  }
}

async function sendEmailsOnly(resend) {
  showBanner("error", "");
  const d = el("confirm-date")?.value;
  if (!d) {
    showBanner("error", "Pick a work date.");
    setConfirmResult("Pick a work date first.", false);
    return;
  }

  setConfirmResult("Sending emails…", false);
  showBanner("info", "Sending emails…");

  try {
    const res = await fetch("/api/confirm/send-emails", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ work_date: d, resend: !!resend }),
    });
    const data = await res.json().catch(() => ({}));
    const det409 = data.detail;
    if (
      res.status === 409 &&
      det409 &&
      typeof det409 === "object" &&
      det409.requires_resend
    ) {
      const ok = window.confirm(
        (det409.message || "Already sent.") + "\n\nSend all emails again?"
      );
      if (ok) return sendEmailsOnly(true);
      setConfirmResult("Cancelled.", false);
      showBanner("info", "");
      return;
    }
    if (!res.ok) {
      const errText =
        typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || res.statusText);
      setConfirmResult(errText, false);
      showBanner("error", errText);
      return;
    }
    const lines = [
      data.message,
      `Employee emails OK: ${data.employee_emails_sent_ok}`,
      `Manager email sent: ${data.manager_email_sent ? "yes" : "no"}`,
    ];
    if (data.manager_email_error) lines.push(`Manager error: ${data.manager_email_error}`);
    if (data.employee_emails) {
      for (const r of data.employee_emails) {
        if (!r.ok) lines.push(`${r.employee} → ${r.to}: FAILED ${r.error}`);
      }
    }
    setConfirmResult(lines.map((l) => `<div>${l}</div>`).join(""), true);
    showBanner("info", "Email batch finished. See details below.");
  } catch (e) {
    const msg = String(e);
    setConfirmResult(msg, false);
    showBanner("error", msg);
  }
}

function renderWeeklyText(data) {
  const host = el("weekly-out");
  let t = `<h3>Week (Mon–Sun) ${data.week_start} → ${data.week_end}</h3>`;
  const lines = data.by_employee_lines || {};
  const totals = data.totals_hours || {};
  for (const name of Object.keys(lines).sort()) {
    t += `<h4>${name}</h4><ul>`;
    for (const line of lines[name]) t += `<li>${line}</li>`;
    t += `</ul>`;
  }
  t += `<h4>Total for this week</h4><ul>`;
  for (const name of Object.keys(totals).sort()) {
    t += `<li>${name}: ${totals[name].toFixed(2)} hours</li>`;
  }
  t += `</ul>`;
  host.innerHTML = t;
}

async function loadWeekly() {
  const ws = el("week-start").value;
  if (!ws) return;
  const res = await fetch(`/api/summary/weekly?week_start=${encodeURIComponent(ws)}`);
  const data = await res.json();
  if (!res.ok) {
    el("weekly-out").textContent =
      typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || "Error");
    return;
  }
  const wkInp = el("week-start");
  if (wkInp && data.week_start) wkInp.value = data.week_start;
  renderWeeklyText(data);
  el("btn-weekly-csv").href = `/api/export/weekly.csv?week_start=${encodeURIComponent(data.week_start)}`;
}

async function loadTwoWeek() {
  const ps = el("twoweek-start").value;
  if (!ps) return;
  const res = await fetch(`/api/summary/two-week?period_start=${encodeURIComponent(ps)}`);
  const data = await res.json();
  const host = el("twoweek-out");
  if (!res.ok) {
    host.textContent =
      typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || "Error");
    return;
  }
  const totals = data.totals_hours || {};
  const twInp = el("twoweek-start");
  if (twInp && data.period_start) twInp.value = data.period_start;
  let html = `<p>Two weeks (Mon–Sun × 2): <strong>${data.period_start}</strong> → <strong>${data.period_end}</strong></p><h4>Total for those two weeks</h4><table class="table"><thead><tr><th>Employee</th><th class="num">Hours</th></tr></thead><tbody>`;
  for (const name of Object.keys(totals).sort()) {
    html += `<tr><td>${name}</td><td class="num">${totals[name].toFixed(2)}</td></tr>`;
  }
  html += `</tbody></table>`;
  host.innerHTML = html;
  el("btn-twoweek-csv").href = `/api/export/two-week.csv?period_start=${encodeURIComponent(data.period_start)}`;
}

async function runCalculate() {
  showBanner("error", "");
  const d = el("pick-date").value;
  if (!d) {
    showBanner("error", "Pick a date.");
    return;
  }
  const shifts = gatherShiftsFull();
  const hasAny = Object.values(shifts).some((arr) => arr.length > 0);
  if (!hasAny) {
    showBanner("error", "Add at least one shift block for someone who worked.");
    return;
  }

  const manual_rules = gatherManualRules();
  const bad = validateManualRulesLocal(manual_rules);
  if (bad) {
    showBanner("error", bad);
    return;
  }

  const body = { date: d, shifts, manual_rules };
  lastCalculatePayload = body;

  try {
    const res = await fetch("/api/calculate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg =
        typeof data.detail === "string"
          ? data.detail
          : JSON.stringify(data.detail || data);
      showBanner("error", msg);
      clearResults();
      return;
    }
    showBanner("info", "Allocation updated.");
    renderSummary(data);
    renderRecon(data);
    renderEmployees(data);
    renderTx(data);
    el("btn-export-employees").disabled = false;
    el("btn-export-tx").disabled = false;
  } catch (e) {
    showBanner("error", String(e));
    clearResults();
  }
}

async function downloadExport(path, filenameHint) {
  if (!lastCalculatePayload) {
    showBanner("error", "Run Calculate first.");
    return;
  }
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(lastCalculatePayload),
    });
    if (!res.ok) {
      const t = await res.text();
      showBanner("error", t || res.statusText);
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filenameHint;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    showBanner("error", String(e));
  }
}

function showTab(tabId) {
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.add("hidden"));
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
  const panel = el(`tab-${tabId}`);
  if (panel) panel.classList.remove("hidden");
  const btn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
  if (btn) btn.classList.add("active");
}

function init() {
  const dateInput = el("pick-date");
  const shiftHost = el("shift-panels");
  if (!dateInput || !shiftHost) {
    const banner = el("global-error");
    if (banner) {
      banner.textContent =
        "Page did not load completely. Try a hard refresh: Cmd+Shift+R (Mac) or Ctrl+Shift+R (Windows).";
      banner.classList.remove("hidden");
    }
    console.error("tip_dashboard: missing #pick-date or #shift-panels — init aborted");
    return;
  }

  dateInput.value = todayLocalISO();

  buildShiftPanels();
  buildSettingsGrid();

  const cdate = el("confirm-date");
  if (cdate) cdate.value = dateInput.value;
  const mon = mondayOfWeekContaining(dateInput.value);
  const wk = el("week-start");
  if (wk) wk.value = mon;
  const tw = el("twoweek-start");
  if (tw) tw.value = mon;

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      showTab(btn.dataset.tab);
      if (btn.dataset.tab === "confirm") syncConfirmDateFromPicker();
    });
  });

  dateInput.addEventListener("change", () => {
    const mr = el("manual-rules-host");
    if (mr) mr.innerHTML = "";
    lastTippedPayments = [];
    repopulateManualPaymentSelects();
    clearResults();
    syncConfirmDateFromPicker();
    const mon = mondayOfWeekContaining(dateInput.value);
    const ws = el("week-start");
    if (ws) ws.value = mon;
    const ts = el("twoweek-start");
    if (ts) ts.value = mon;
  });

  el("btn-refresh")?.addEventListener("click", refreshClover);
  el("btn-add-manual")?.addEventListener("click", () => addManualRuleRow());
  el("btn-calculate")?.addEventListener("click", runCalculate);
  el("btn-export-employees")?.addEventListener("click", () =>
    downloadExport("/api/export/employees", "employee_summary.csv")
  );
  el("btn-export-tx")?.addEventListener("click", () =>
    downloadExport("/api/export/transactions", "tip_transactions.csv")
  );

  el("set-test-mode")?.addEventListener("change", updateTestModeBanner);
  el("btn-save-settings")?.addEventListener("click", saveSettings);
  el("btn-confirm-preview")?.addEventListener("click", loadConfirmPreview);
  el("btn-confirm-save")?.addEventListener("click", () => confirmSaveOnly(false));
  el("btn-confirm-send-emails")?.addEventListener("click", () => sendEmailsOnly(false));
  el("btn-weekly-load")?.addEventListener("click", loadWeekly);
  el("btn-twoweek-load")?.addEventListener("click", loadTwoWeek);

  loadSettings();
  refreshClover();
}

document.addEventListener("DOMContentLoaded", init);

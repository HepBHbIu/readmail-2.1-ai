/* Readmail v2 — app.js v3.2 (realtime, fixed pipeline) */
"use strict";

const $ = id => document.getElementById(id);

/* ──────────────────────── Утилиты ──────────────────────── */

async function api(path, opts = {}) {
  try {
    const fetchOpts = { ...opts };
    if (!(fetchOpts.body instanceof FormData)) {
      fetchOpts.headers = { "Content-Type": "application/json", ...(fetchOpts.headers || {}) };
    }
    const res = await fetch(path, fetchOpts);
    const text = await res.text();
    let data = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (e) {
        data = { ok: false, error: text.slice(0, 500) || String(e) };
      }
    }
    if (!res.ok) {
      data.ok = false;
      data.error = data.error || data.detail || `HTTP ${res.status}`;
    }
    return data;
  } catch (e) { return { ok: false, error: String(e) }; }
}

function toast(msg, type = "info") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  const c = $("toast-container");
  if (c) c.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function badge(text, color) {
  return `<span class="badge badge-${color}">${esc(String(text || ""))}</span>`;
}

function esc(s) {
  return String(s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
// Для строк внутри onclick-атрибутов в одиночных кавычках — дополнительно экранируем ' и \
function escJs(s) {
  return esc(String(s || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'"));
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString("ru", { day: "2-digit", month: "2-digit", year: "2-digit" })
    + " " + d.toLocaleTimeString("ru", { hour: "2-digit", minute: "2-digit" });
}

async function copyToClipboard(text) {
  try { await navigator.clipboard.writeText(text); toast("Скопировано", "success"); }
  catch {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove();
    toast("Скопировано", "success");
  }
}

// Реестр callback'ов для пагинации — не передаём функции через onclick-строки
const _paginationCallbacks = new Map();
let _paginationSeq = 0;

function renderPagination(containerId, total, page, pageSize, onPage) {
  const el = $(containerId);
  if (!el) return;
  const pages = Math.ceil(total / pageSize);
  if (pages <= 1) { el.innerHTML = ""; return; }
  // Сохраняем callback в реестре
  const key = `pg_${containerId}_${++_paginationSeq}`;
  _paginationCallbacks.set(key, onPage);
  let html = "";
  for (let p = 1; p <= pages; p++) {
    if (pages > 10 && Math.abs(p - page) > 2 && p !== 1 && p !== pages) {
      if (p === 2 || p === pages - 1) html += `<span style="color:var(--text-muted)">…</span>`;
      continue;
    }
    html += `<button class="page-btn${p === page ? " active" : ""}" data-cb="${key}" data-p="${p}">${p}</button>`;
  }
  el.innerHTML = html;
  // Event delegation — без inline-кода
  el.querySelectorAll(".page-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const cb = _paginationCallbacks.get(btn.dataset.cb);
      if (cb) cb(parseInt(btn.dataset.p));
    });
  });
}

const KIND_LABELS = {
  defect: "Брак", nonconforming: "Некондиция", number_replacement: "Замена артикула", wrong_item: "Пересорт",
  shortage: "Недовоз", overdelivery: "Излишек", incomplete_set: "Некомплект",
  correction_request: "Корректировка", marking_request: "Маркировка",
  quality_refusal: "Отказ клиента", unknown: "Неизвестно",
};
const PRIORITY_LABELS = {
  critical: "Критичный", high: "Высокий", medium: "Средний", normal: "Обычный", low: "Низкий",
};
const PRIORITY_COLORS = { critical: "red", high: "amber", medium: "blue", normal: "gray", low: "gray" };
const STATE_LABELS = {
  ready_to_1c: "Готов к 1С", needs_review: "На проверке", needs_link: "Нужна ссылка",
  exported: "Отправлен", closed: "Закрыт", ignored_internal: "Внутреннее",
  delivered: "Доставлен", unknown: "Неизвестно",
  linked_event: "Привязано", linking_event: "Привязка",
  ignored_info_only: "Служебное (пропущено)", ignore_info_only: "Служебное (пропущено)",
  info_only: "Информационное", problem_notice: "Уведомление о проблеме",
};
const EVENT_LABELS = {
  new_return: "Новый возврат", followup_reminder: "Напоминалка",
  followup_dialog: "Диалог", supplier_decision: "Решение поставщика", unknown: "Неизвестно",
  correction_request: "Корректировка", info_only: "Инфо/служебное",
  ready_to_ship: "Готов к отгрузке", marking_request: "Маркировка",
  problem_notice: "Уведомление о проблеме",
};

/* ──────────────────────── Табы ──────────────────────── */

function initTabs() {
  document.querySelectorAll(".tab").forEach(btn => {
    btn.addEventListener("click", () => {
      const tabName = btn.dataset.tab;
      if (!tabName) return;
      activateTab(tabName, true);
      // Закрываем выпадашку после выбора пункта.
      document.querySelectorAll(".nav-group.open").forEach(g => g.classList.remove("open"));
    });
  });
  // Клик по заголовку блока — открыть/закрыть его выпадашку (надёжнее hover, работает на тач).
  document.querySelectorAll(".nav-group-head").forEach(head => {
    head.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const grp = head.closest(".nav-group");
      const wasOpen = grp.classList.contains("open");
      document.querySelectorAll(".nav-group.open").forEach(g => g.classList.remove("open"));
      if (!wasOpen) grp.classList.add("open");
    });
  });
  // Клик вне меню — закрыть все выпадашки.
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest(".nav-group")) {
      document.querySelectorAll(".nav-group.open").forEach(g => g.classList.remove("open"));
    }
  });
}

function activateTab(tabName, persist = false) {
  const btn = document.querySelector(`.tab[data-tab="${tabName}"]`);
  const target = $("tab-" + tabName);
  if (!btn || !target) return false;
  document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
  document.querySelectorAll(".tab-content").forEach(t => t.classList.remove("active"));
  btn.classList.add("active");
  target.classList.add("active");
  if (persist) localStorage.setItem("readmail.activeTab", tabName);
  onTabActivated(tabName);
  return true;
}

function loadSavedPage(key, fallback = 1) {
  const n = parseInt(localStorage.getItem("readmail.page." + key) || String(fallback), 10);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}
function savePage(key, page) {
  localStorage.setItem("readmail.page." + key, String(page || 1));
}


function onTabActivated(tab) {
  try {
    if (tab === "dashboard") loadDashboard();
    else if (tab === "emails") { loadEmails(); loadPipelineStatus(); loadMailReconcileSummary(); }
    else if (tab === "review") loadReview();
    // v2.1 AI-only: вкладка «Паттерны» удалена.
    else if (tab === "ai_review") loadAiReview();
    else if (tab === "links") loadLinks();
    else if (tab === "offtopic") loadOfftopic();
    else if (tab === "processed") loadProcessedHidden();
    else if (tab === "pipeline") loadPipeline();
    else if (tab === "unprocessed") loadUnprocessed();
    else if (tab === "clients") loadClients();
    else if (tab === "onec") loadOnec();
    else if (tab === "inbox_sorter") loadInboxSorter();
    else if (tab === "final_sorter") loadFinalSorter();
    else if (tab === "evidence") loadEvidenceDashboard();
    else if (tab === "ai_trace") loadAiTrace();
    else if (tab === "defect_audit") loadDefectAudit();
    else if (tab === "supplier_matrix") loadEvidenceSuppliers();
    else if (tab === "quick_review_pipeline") loadQuickReviewQueue();
    else if (tab === "outbox_staging") loadOutboxStaging();
    else if (tab === "settings") { loadSettings(); loadTokenStats(); loadTokenReport(); loadTrafficStats(); initSettingsCategories(); loadTokenTimeline("day"); }
  } catch (e) { console.warn("Tab error:", tab, e); }
}

/* ── Категории Настроек: под-навигация вместо стены групп ── */
const SETTINGS_CATS = {
  "Почта":        ["почта", "папки imap"],
  "ИИ":           ["ai:", "ии"],
  "Обработка/1С": ["режим обучения", "режим обработки", "автопилот", "экспорт в 1с"],
  "Уведомления":  ["telegram"],
  "Система":      ["выгрузк", "объем", "сброс", "трафик"],
};
function _settingsCatOf(h3) {
  const h = (h3 || "").toLowerCase();
  for (const [name, keys] of Object.entries(SETTINGS_CATS)) {
    if (keys.some(k => h.includes(k))) return name;
  }
  return "Прочее";
}
function initSettingsCategories() {
  const layout = document.querySelector("#tab-settings .settings-layout");
  const nav = $("settings-subnav");
  if (!layout || !nav || layout.dataset.catsReady) return;
  layout.querySelectorAll(".settings-group").forEach(g => {
    g.dataset.cat = _settingsCatOf(g.querySelector("h3")?.textContent);
  });
  const cats = ["Все", ...Object.keys(SETTINGS_CATS)];
  nav.innerHTML = cats.map((c, i) =>
    `<button class="settings-cat-btn${i === 0 ? " active" : ""}" data-cat="${c}" onclick="filterSettingsCat('${c}')">${c}</button>`
  ).join("");
  layout.dataset.catsReady = "1";
  // По умолчанию — первая категория (а не стена «Все»).
  filterSettingsCat(localStorage.getItem("readmail.settingsCat") || "Почта");
}
function filterSettingsCat(cat) {
  localStorage.setItem("readmail.settingsCat", cat);
  document.querySelectorAll("#tab-settings .settings-group").forEach(g => {
    g.style.display = (cat === "Все" || g.dataset.cat === cat) ? "" : "none";
  });
  document.querySelectorAll(".settings-cat-btn").forEach(b => b.classList.toggle("active", b.dataset.cat === cat));
}

/* ══════════════════════════════════════════════════════
   СВЕРКА — визуальный контроль перед 1С
══════════════════════════════════════════════════════ */

let _reviewPage = loadSavedPage("review");
let _reviewSelectedId = null;
const _reviewCache = new Map();

let _rt, _pt, _at; // debounce timers for review / patterns / ai search

async function loadReview() {
  try {
    const filter = $("review-filter")?.value || "all";
    const buyer  = $("review-buyer")?.value  || "";
    const folder = $("review-folder")?.value || "all";
    const kind   = $("review-kind")?.value   || "";
    const q      = $("review-search")?.value || "";
    const miss = Array.from(document.querySelectorAll(".review-miss:checked")).map(el => el.value);
    const res = await api(`/api/review/cases?source=${filter}&buyer=${encodeURIComponent(buyer)}&folder=${encodeURIComponent(folder)}&kind=${encodeURIComponent(kind)}&q=${encodeURIComponent(q)}&missing=${encodeURIComponent(miss.join(","))}&page=${_reviewPage}&limit=50`);
    if (!res.ok) return;
    // Счётчики пустых ячеек на галочках
    const ec = res.empty_counts || {};
    document.querySelectorAll("[data-miss-count]").forEach(el => {
      const n = ec[el.dataset.missCount];
      el.textContent = (n != null) ? `(${n})` : "";
    });
    // Дропдаун причин со счётчиками
    const kSel = $("review-kind");
    if (kSel) {
      const curK = kSel.value;
      kSel.innerHTML = '<option value="">Все причины</option>';
      (res.kind_counts || []).forEach(k => {
        const o = document.createElement("option");
        o.value = k.kind;
        const label = k.kind === "__none__" ? "— без причины —" : (KIND_LABELS[k.kind] || k.kind);
        o.textContent = `${label} (${k.count})`;
        if (k.kind === curK) o.selected = true;
        kSel.appendChild(o);
      });
    }
    const cnt = $("review-count");
    if (cnt) cnt.textContent = `Показано ${res.shown_count ?? (res.cases || []).length} из ${res.total_count ?? res.total ?? 0}`;
    const folderSel = $("review-folder");
    if (folderSel) {
      const currentFolder = folderSel.value;
      Array.from(folderSel.options).forEach(option => {
        const key = option.value;
        const baseName = key === "all" ? "Все папки" : (res.folder_names?.[key] || option.textContent.replace(/\s+\(\d+\)$/, ""));
        const count = res.folder_counts?.[key];
        option.textContent = count == null ? baseName : `${baseName} (${count})`;
        option.selected = key === currentFolder;
      });
    }
    // Badge on tab
    const tabBadge = $("badge-review");
    if (tabBadge) { tabBadge.textContent = res.total > 0 ? res.total : ""; }

    // Populate buyer filter
    const bSel = $("review-buyer");
    if (bSel) {
      const cur = bSel.value;
      bSel.innerHTML = '<option value="">Все клиенты</option>';
      (res.buyers || []).forEach(b => {
        const o = document.createElement("option");
        o.value = b.code; o.textContent = b.name || b.code;
        if (b.code === cur) o.selected = true;
        bSel.appendChild(o);
      });
    }

    _reviewCache.clear();
    const list = $("review-case-list");
    if (!list) return;
    list.innerHTML = (res.cases || []).map(c => {
      _reviewCache.set(c.id, c);
      const f = c.fields || {};
      const kind = KIND_LABELS[c.claim_kind] || c.claim_kind || "—";
      const prColor = PRIORITY_COLORS[c.priority] || "gray";
      const srcBadge = c.source === "ai"
        ? `<span class="badge badge-amber" style="font-size:9px">AI</span>`
        : `<span class="badge badge-blue" style="font-size:9px">Паттерн</span>`;
      const readyBadge = c.ready_for_export
        ? `<span class="badge badge-green" style="font-size:9px">Готов</span>`
        : `<span class="badge badge-gray" style="font-size:9px">${c.can_export ? "Review" : esc(c.state || "Служебное")}</span>`;
      const folderBadge = `<span class="badge badge-gray" style="font-size:9px">${esc(c.folder_name || "Без папки")}</span>`;
      const hasMissing = (c.missing || []).length > 0;
      return `<div class="split-item${c.id === _reviewSelectedId ? " active" : ""}${hasMissing ? " has-issues" : ""}"
               data-id="${c.id}" onclick="selectReviewCase(${c.id})">
        <div class="split-item-top">
          <span class="split-item-buyer">${esc(c.buyer_name || "—")}</span>
          ${srcBadge} ${folderBadge} ${readyBadge}
        </div>
        <div class="split-item-subject">${esc((c.subject || "—").slice(0,60))}</div>
        <div class="split-item-meta">
          ${badge(kind, "blue")}
          ${f.part_number ? `<span style="font-size:10px">${esc(f.part_number)}</span>` : ""}
          ${f.document_number ? `<span style="font-size:10px">${esc(f.document_number)}</span>` : ""}
          <span style="color:var(--text-muted)">${fmtDate(c.received_at)}</span>
        </div>
        ${hasMissing ? `<div style="font-size:10px;color:var(--amber)">Не хватает: ${esc(c.missing.join(", "))}</div>` : ""}
      </div>`;
    }).join("") || '<div class="split-empty">Нет кейсов для сверки</div>';

    renderPagination("review-pagination", res.total || 0, _reviewPage, 50,
      p => { _reviewPage = p; savePage("review", p); loadReview(); });

    if (_reviewSelectedId && _reviewCache.has(_reviewSelectedId)) {
      renderReviewDetail(_reviewCache.get(_reviewSelectedId));
    }
  } catch(e) { console.warn("loadReview error:", e); }
}

async function selectReviewCase(id) {
  _reviewSelectedId = id;
  document.querySelectorAll("#review-case-list .split-item").forEach(el =>
    el.classList.toggle("active", el.dataset.id == id));
  const cached = _reviewCache.get(id);
  if (cached) renderReviewDetail(cached);
  // Load full case data
  const caseData = await api(`/api/cases/${id}`);
  if (caseData && !caseData.error) {
    _reviewCache.set(id, { ...(cached || {}), ...caseData });
    renderReviewDetail(_reviewCache.get(id));
  }
  // Load email body
  const rawId = cached?.raw_email_id || caseData?.raw_email_id;
  if (rawId) {
    const emailData = await api(`/api/emails/${rawId}`);
    if (emailData && !emailData.error) {
      const bodyEl = $("review-email-body");
      if (bodyEl) {
        const body = emailData.visible_text || emailData.body_text || emailData.snippet || "";
        const preview = body.length > 5000 ? body.slice(0, 5000) + "\n\n...(обрезано)" : body;
        bodyEl.innerHTML = `
          <div class="detail-field"><label>От кого</label><div class="val">${esc(emailData.from_addr || "—")}</div></div>
          <div class="detail-field"><label>Тема</label><div class="val">${esc(emailData.subject || "—")}</div></div>
          ${(emailData.attachments || []).length ? `<div style="margin:6px 0">
            <div style="font-size:11px;color:var(--text-muted);margin-bottom:3px">📁 Вложения (${emailData.attachments.length}) — клик открывает в полном размере:</div>
            ${emailData.attachments.map(a => {
              const isImg = /image\//.test(a.content_type||"") || /\.(jpg|jpeg|png|gif|webp|heic|bmp)$/i.test(a.filename||"");
              const url = `/api/attachments/${a.id}/download`;
              const kb = a.size_bytes ? Math.round(a.size_bytes/1024) : 0;
              return `<div style="margin:3px 0;padding:3px 0;border-top:1px solid var(--border)">
                <a href="${url}" target="_blank" rel="noopener" style="font-size:12px">${isImg?"🖼":"📎"} ${esc(a.filename||"файл")}</a>
                <span class="muted" style="font-size:10px">${kb?`· ${kb} КБ`:""}</span>
                ${isImg ? `<div><a href="${url}" target="_blank" rel="noopener"><img src="${url}" loading="lazy" style="max-width:240px;max-height:180px;border-radius:6px;margin-top:4px;border:1px solid var(--border);cursor:zoom-in"></a></div>` : ""}
              </div>`;
            }).join("")}
          </div>` : ""}
          <pre style="font-size:11px;line-height:1.7;white-space:pre-wrap;background:var(--bg);padding:8px;border-radius:6px;max-height:60vh;overflow-y:auto">${esc(preview)}</pre>
        `;
      }
    }
  }
}

function renderReviewDetail(c) {
  const titleEl = $("review-case-title");
  if (titleEl) titleEl.textContent = `#${c.id} — ${c.buyer_name || "?"}`;
  const content = $("review-fields-content");
  if (!content) return;
  // Render editable fields + actions
  content.innerHTML = renderCaseDetail(c) + `
    ${c.claim_kind === "defect" ? `
    <div style="margin-top:10px;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--bg)">
      <div style="font-size:12px;font-weight:600;margin-bottom:4px">📋 Документы брака (установка / снятие / акт сервиса)</div>
      <div id="defect-docs-result" style="font-size:11px;color:var(--text-muted)">${renderDefectFlag(c.defect_doc_flag)}</div>
      <button class="btn-sm" onclick="checkDefectDocs(${c.id})" style="margin-top:6px">🔍 Проверить документы ИИ</button>
    </div>` : ""}
    ${(c.attachments || []).some(a => /\.(xlsx?|xlsm|docx?|csv|zip)$/i.test(a.filename || "")) ? `
    <div style="margin-top:8px">
      <button class="btn-sm" onclick="readAttachmentsAi(${c.id}, this)" title="ИИ дочитает Excel-акт/документы из вложений и добьёт поля (бренд/имя/артикул)">📎 Дочитать вложения ИИ</button>
    </div>` : ""}
    ${c.can_export ? `
      <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap">
        ${c.ready_for_export
          ? `<button class="btn-sm success" onclick="approveReviewCase(${c.id})">Подтвердить → 1С</button>`
          : `<button class="btn-sm" onclick="approveReviewCase(${c.id})">В очередь</button>`}
        <button class="btn-sm danger" onclick="rejectReviewCase(${c.id})">Отклонить</button>
      </div>` : `
      <div style="margin-top:10px;padding:8px;border:1px solid var(--border);border-radius:6px;color:var(--text-muted)">
        Папка: <strong>${esc(c.folder_name || "Служебные")}</strong>. Кейс виден для сверки, экспорт в 1С для этого типа отключён.
      </div>`}
  `;
}

const DOC_LABELS = { install_order: "заказ-наряд установка", removal_order: "заказ-наряд снятие", service_act: "акт/заключение сервиса" };
const DEFECT_STATE_LABELS = { complete: "✅ все документы", partial: "🟡 частично", absent: "🔴 документов нет", present_unverified: "📎 файлы есть (не распознано)" };
function renderDefectFlag(flag) {
  if (!flag) return "Не проверено — нажмите «Проверить документы ИИ»";
  const present = flag.present || {};
  const marks = Object.keys(DOC_LABELS).map(k =>
    `${present[k] ? "✅" : "⬜"} ${DOC_LABELS[k]}`).join(" · ");
  const st = DEFECT_STATE_LABELS[flag.state] || flag.state || "";
  const mode = flag.mode === "ai" ? " (ИИ-скан)" : "";
  return `<b>${st}</b>${mode}<br>${marks}`;
}

async function checkDefectDocs(caseId) {
  const el = $("defect-docs-result");
  if (el) el.innerHTML = "⏳ ИИ читает документы и фото… (может занять до минуты)";
  const res = await api(`/api/cases/${caseId}/check-defect-docs`, { method: "POST" });
  if (!res || res.error) { if (el) el.textContent = "Ошибка: " + (res?.error || "нет ответа"); return; }
  let html = renderDefectFlag(res);
  if ((res.attachments || []).length) {
    html += `<div style="margin-top:5px">${res.attachments.map(a =>
      `<div style="font-size:10px">${a.group === "document" ? "📄" : "🖼"} ${esc(a.filename||"")}: <b>${esc(a.doc_type||a.error||"?")}</b>${a.reason && a.doc_type!=="part_photo" ? ` — ${esc((a.reason||"").slice(0,80))}` : ""}</div>`
    ).join("")}</div>`;
  }
  if (el) el.innerHTML = html;
  const cached = _reviewCache.get(caseId); if (cached) cached.defect_doc_flag = res;
}

async function approveReviewCase(caseId) {
  const res = await api(`/api/review/approve/${caseId}`, { method: "POST" });
  const errorText = res.message || res.error || "";
  toast(res.ok ? "Подтверждено и в очередь 1С" : "Не отправлено: " + errorText, res.ok ? "success" : "error");
  loadReview();
}

async function approveAllReview() {
  const res = await api("/api/review/approve-all", { method: "POST" });
  const blocked = Number(res.blocked || 0);
  toast(
    res.ok ? `${res.queued || 0} кейсов → 1С${blocked ? `, Evidence заблокировал: ${blocked}` : ""}` : "Ошибка",
    res.ok ? (blocked ? "warn" : "success") : "error",
  );
  loadReview();
}

async function rejectReviewCase(caseId) {
  await api(`/api/cases/${caseId}`, { method: "PATCH",
    body: JSON.stringify({ state: "needs_review", ready_for_export: false }) });
  toast("Возвращён на проверку", "warn");
  loadReview();
}

/* ──────────────────────── Пайплайн + Realtime ──────────────────────── */

// Состояние для realtime-polling
let _pipelineState = { importBusy: false, patternsBusy: false, aiBusy: false, autopilot: false };
let _pollFast = null;   // интервал 2s когда что-то работает
let _pollSlow = null;   // интервал 10s когда всё тихо
let _lastPipelineHash = "";

function btnToggle(prefix, busy) {
  const start = $(prefix + "-start");
  const stop = $(prefix + "-stop");
  if (start) start.style.display = busy ? "none" : "";
  if (stop) stop.style.display = busy ? "" : "none";
}

function autoBtnToggle(running, mode) {
  const startBtns = document.querySelectorAll("#pipeline-btn-autopilot, #pipeline-btn-autopilot-ai, #btn-autopilot-start-settings");
  const stopBtns = document.querySelectorAll("#pipeline-btn-autopilot-stop, #btn-autopilot-stop-settings");
  startBtns.forEach(b => { if (b) b.style.display = running ? "none" : ""; });
  stopBtns.forEach(b => { if (b) b.style.display = running ? "" : "none"; });
  const stopMain = $("pipeline-btn-autopilot-stop");
  if (stopMain && running) {
    const lbl = stopMain.querySelector("span:last-child");
    if (lbl) lbl.textContent = mode === "full_ai" ? "Стоп ИИ" : "Стоп";
  }
}

// Переключаем частоту опроса: быстро когда что-то работает
function _adjustPolling(anyBusy) {
  if (anyBusy) {
    if (!_pollFast) {
      _pollFast = setInterval(pollTick, 2000);
      if (_pollSlow) { clearInterval(_pollSlow); _pollSlow = null; }
    }
  } else {
    if (_pollFast) { clearInterval(_pollFast); _pollFast = null; }
    if (!_pollSlow) { _pollSlow = setInterval(pollTick, 10000); }
  }
}

// Главный тик опроса — вызывается каждые 2s (busy) или 10s (idle)
let _processedBadgeTick = 0;
async function pollTick() {
  const res = await api("/api/v2/pipeline/status");
  if (!res.ok) return;
  // число «Обработанных» — фоном (cached), не чаще раза в ~30с, не блокирует poll
  if (_processedBadgeTick++ % 3 === 0) refreshProcessedBadge();
  const wasImportBusy = _pipelineState.importBusy;
  const wasPatternsBusy = _pipelineState.patternsBusy;
  const wasAiBusy = _pipelineState.aiBusy;
  updatePipelineUI(res);
  // Живой остаток при перепрогоне паттернов (в т.ч. запущенном фоном/автопилотом):
  // показываем «осталось N (done/total) %», а не безликое «идёт».
  if (res.patterns_busy) {
    try {
      const pp = await api("/api/patterns/progress");
      const st = pp.state || {};
      const done = st.processed || 0, total = st.total || 0;
      if (total) {
        const left = Math.max(0, total - done);
        const pct = Math.round(done * 100 / total);
        const stEl = $("pipeline-patterns-status");
        if (stEl) { stEl.textContent = `осталось ${left} (${done}/${total})`; stEl.className = "step-status running"; }
        const cntEl = $("pipe-count-patterns");
        if (cntEl) { cntEl.textContent = `${pct}%`; cntEl.className = "pstep-count running"; cntEl.title = `Обработано ${done} из ${total}, осталось ${left}`; }
        const hint = $("cycle-running-hint");
        if (hint) hint.textContent = `Паттерны: осталось ${left} из ${total} (${pct}%)`;
      }
    } catch (e) {}
  }
  // Если операция только что завершилась — обновить активную вкладку
  const activeTab = document.querySelector(".tab.active")?.dataset?.tab;
  if (wasImportBusy && !res.import_busy) { if (activeTab === "emails") loadEmails(); }
  if (wasAiBusy && !res.ai_busy) { if (activeTab === "ai_review") loadAiReview(); }
  // Обновляем счётчики вкладок при любом изменении
  const hash = JSON.stringify([res.total_emails, res.pattern_ready, res.needs_ai, res.ai_ready, res.links_count, res.review_count, res.unprocessed, res.unprocessed_tab, res.processed_hidden, res.offtopic, res.outbox_new]);
  if (hash !== _lastPipelineHash) {
    _lastPipelineHash = hash;
    updateTabBadges(res);
    if (activeTab === "emails") loadEmails();
    // Обновляем данные активной вкладки если что-то изменилось
    if (activeTab === "ai_review") loadAiReview();
    if (activeTab === "links") loadLinks();
    if (activeTab === "offtopic") loadOfftopic();
    if (activeTab === "unprocessed") loadUnprocessed();
    if (activeTab === "onec") loadOnec();
  }
  loadSystemStatus();
}

function updateTabBadges(res) {
  // Обновляем счётчики на вкладках
  const badges = {
    "patterns": res.pattern_ready || 0,
    "ai_review": res.needs_ai || 0,
    "links": res.links_count || 0,
    "review": res.review_count || 0,
    "offtopic": res.offtopic || 0,
    "unprocessed": res.unprocessed_tab ?? res.unprocessed ?? 0,
    "processed": res.processed_hidden || 0,
    "onec": res.outbox_new || 0,
    "pipeline": res.total_emails || 0,   // всего писем (быстрый источник)
  };
  Object.entries(badges).forEach(([tab, count]) => {
    let btn = document.querySelector(`.tab[data-tab="${tab}"]`);
    if (!btn) return;
    // Найти или создать бейдж
    let badge = btn.querySelector(".tab-badge");
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "tab-badge";
      btn.appendChild(badge);
    }
    badge.textContent = count > 0 ? count : "";
    badge.style.display = count > 0 ? "inline-block" : "none";
  });
}

async function loadPipelineStatus() {
  try {
    const res = await api("/api/v2/pipeline/status");
    if (res.error) return;
    updatePipelineUI(res);
    updateTabBadges(res);
    _adjustPolling(res.import_busy || res.patterns_busy || res.ai_busy || res.autopilot_running);
  } catch (e) { /* ignore */ }
}

function updatePipelineUI(res) {
  const setStep = (id, text, cls) => {
    const el = $(id);
    if (el) { el.textContent = text; el.className = "step-status " + cls; }
  };
  const setPipeCount = (id, text, cls, title) => {
    const el = $(id);
    if (!el) return;
    el.textContent = text;
    el.className = "pstep-count " + (cls || "");
    if (title) el.title = title;
  };

  const importBusy = res.import_busy || false;
  const patternsBusy = res.patterns_busy || false;
  const aiBusy = res.ai_busy || false;
  const total = res.total_emails || 0;

  _pipelineState = { importBusy, patternsBusy, aiBusy, autopilot: res.autopilot_running };

  // Не сбрасываем кнопку если запущена вручную (_manualBusy) — иначе pollTick сдвигает её обратно
  if (!_manualBusy.has("import"))   btnToggle("pipeline-btn-import",   importBusy);
  if (!_manualBusy.has("patterns")) btnToggle("pipeline-btn-patterns", patternsBusy);
  if (!_manualBusy.has("ai"))       btnToggle("pipeline-btn-ai",       aiBusy);
  autoBtnToggle(res.autopilot_running || false);

  // Импорт — НЕ трогаем, пока этап крутится вручную (его обновляет runImport),
  // иначе pollTick и runImport дерутся за элемент → мелькание «идёт ↔ цифра».
  if (!_manualBusy.has("import")) {
    const importText = importBusy ? "идет" : total > 0 ? `${total}` : "0";
    setStep("pipeline-import-status",
      importBusy ? "загрузка..." : total > 0 ? `local raw: ${total}` : "ожидание",
      importBusy ? "running" : total > 0 ? "done" : "idle");
    setPipeCount("pipe-count-import", importText, importBusy ? "running" : total > 0 ? "done" : "idle", "Local raw: строк писем в SQLite");
  }

  // Паттерны
  const needsPat = res.needs_pattern || 0;
  const patReady = res.pattern_ready || 0;
  if (!_manualBusy.has("patterns")) {
    const patText = patternsBusy ? "идет" : needsPat > 0 ? `жд ${needsPat}` : `готов ${patReady}`;
    setStep("pipeline-patterns-status",
      patternsBusy ? "обработка..." :
      needsPat > 0 ? `${needsPat} ожидают` :
      patReady > 0 ? `${patReady} готово` : "ожидание",
      patternsBusy ? "running" : needsPat > 0 ? "ready" : patReady > 0 ? "done" : "idle");
    setPipeCount("pipe-count-patterns", patText, patternsBusy ? "running" : needsPat > 0 ? "ready" : patReady > 0 ? "done" : "idle", `Ожидают запуска паттернов: ${needsPat}; готово паттернами: ${patReady}`);
  }

  // AI
  const needsAi = res.needs_ai || 0;
  const aiReady = res.ai_ready || 0;
  if (!_manualBusy.has("ai")) {
    const aiText = aiBusy ? "идет" : needsAi > 0 ? `AI ${needsAi}` : `готов ${aiReady}`;
    setStep("pipeline-ai-status",
      aiBusy ? "AI..." :
      needsAi > 0 ? `${needsAi} в очереди` :
      aiReady > 0 ? `${aiReady} готово` : "готово",
      aiBusy ? "running" : needsAi > 0 ? "ready" : "done");
    setPipeCount("pipe-count-ai", aiText, aiBusy ? "running" : needsAi > 0 ? "ready" : "done", `Ожидают AI-разбора: ${needsAi}; готово после AI: ${aiReady}`);
  }

  setPipeCount("pipe-count-verify", `рев ${res.review_count || 0}`, (res.review_count || 0) > 0 ? "ready" : "idle", "Кейсы на сверке оператором");

  // 1С outbox
  const outboxText = res.outbox_new > 0 ? `${res.outbox_new}` :
    res.outbox_errors > 0 ? `${res.outbox_errors}` :
    res.outbox_sent > 0 ? `${res.outbox_sent}` : "0";
  setStep("pipeline-outbox-status",
    res.outbox_new > 0 ? `${res.outbox_new} новых` :
    res.outbox_errors > 0 ? `${res.outbox_errors} ошибок` :
    res.outbox_sent > 0 ? `${res.outbox_sent} отпр.` : "пусто",
    res.outbox_new > 0 ? "running" : res.outbox_errors > 0 ? "error" : "done");
  setPipeCount("pipe-count-1c", outboxText, res.outbox_errors > 0 ? "error" : res.outbox_new > 0 ? "ready" : "idle", "1С очередь: новые, ошибки или отправленные");

  // Счётчик писем в шапке
  const cnt = $("emails-count");
  if (cnt && total > 0) cnt.textContent = `${total} писем`;

  _adjustPolling(importBusy || patternsBusy || aiBusy || res.autopilot_running);
}

// Флаги ручного запуска — защита от сброса pollTick'ом
const _manualBusy = new Set();

async function runImport() {
  _manualBusy.add("import");
  btnToggle("pipeline-btn-import", true);
  _pipeSet("import", "active", "…");
  _adjustPolling(true);
  toast("Импорт...");
  try {
    const start = await api("/api/import", { method: "POST" });
    if (!start.ok) throw new Error(start.error || start.reason || "Ошибка запуска импорта");

    let progress = start;
    for (let i = 0; i < 360; i++) {
      progress = await api("/api/import/progress");
      const st = progress.state || {};
      const result = st.result || {};
      const imported = st.imported || result.imported || 0;
      const classified = st.classified || result.classified || 0;
      const totalDb = progress.emails_in_db || 0;
      _pipeSet("import", "active", `${totalDb} в базе / +${imported}`);
      await loadPipelineStatus();
      if (!st.running) break;
      await new Promise(r => setTimeout(r, 2000));
    }

    const st = (progress.state || {});
    const result = st.result || {};
    const imported = st.imported || result.imported || 0;
    const skipped = result.skipped || 0;
    const totalServer = result.total_on_server || 0;
    const totalDb = progress.emails_in_db || 0;
    const hasError = st.error || result.ok === false;
    _pipeSet("import", hasError ? "error" : "done", `+${imported}`);
    toast(
      hasError
        ? "Ошибка: " + (st.error || "Ошибка импорта")
        : `Импортировано в этом запуске: +${imported}; пропущено: ${skipped}; local raw: ${totalDb}${totalServer ? `; server total: ${totalServer}` : ""}`,
      hasError ? "error" : "success"
    );
  } catch (e) {
    _pipeSet("import", "error", "!");
    toast("Ошибка: " + (e.message || e), "error");
  } finally {
    _manualBusy.delete("import");
    btnToggle("pipeline-btn-import", false);
    await loadPipelineStatus();
    loadEmails();
    loadReview();
  }
}

function setStep_(id, text, cls) {
  const el = $(id);
  if (el) { el.textContent = text; el.className = "step-status " + cls; }
}

// v2.1 AI-only: функции паттерн-конвейера (runPatterns/Full/stop, runAiBatch/stop) удалены.

async function runFullPipeline() {
  runFullCycle();
}

/* ══════════════════════════════════════════════════════
   ПОЛНЫЙ ЦИКЛ → 1С  (одна кнопка)
══════════════════════════════════════════════════════ */

let _cycleRunning = false;
let _cyclePollTimer = null;
let _cycleStartId = 0;
let _cycleStartTime = 0;

function _stepSet(id, state, val) {
  const el = $(id);
  if (!el) return;
  el.className = "cycle-step " + (state || "");
  const v = el.querySelector(".cstep-val");
  if (v && val !== undefined) v.textContent = val;
}

function _pipeSet(id, state, val) {
  const el = $("pipe-step-" + id);
  if (!el) return;
  el.className = "pstep " + (state || "");
  const cnt = $("pipe-count-" + id);
  if (cnt && val !== undefined) cnt.textContent = val;
}

function _logLine(text, level) {
  const log = $("cycle-log");
  if (!log) return;
  const cls = level === "ok" ? "log-ok" : level === "warn" ? "log-warn" : level === "error" ? "log-err" : "log-info";
  log.innerHTML += `<span class="${cls}">${esc(text)}\n</span>`;
  log.scrollTop = log.scrollHeight;
}

function _elapsedStr() {
  const s = Math.round((Date.now() - _cycleStartTime) / 1000);
  if (s < 60) return `${s}с`;
  return `${Math.floor(s/60)}м ${s%60}с`;
}

async function runFullCycle() {
  if (_cycleRunning) { toast("Цикл уже запущен", "warn"); return; }
  _cycleRunning = true;
  _cycleStartTime = Date.now();

  // Grab last live event ID as anchor
  const liveSnap = await api("/api/live/events?limit=1");
  _cycleStartId = liveSnap?.items?.[0]?.id || 0;

  // Show modal
  const modal = $("cycle-modal");
  if (modal) modal.style.display = "flex";
  const closeBtn = $("cycle-close-btn");
  const runHint  = $("cycle-running-hint");
  const summary  = $("cycle-summary");
  const log      = $("cycle-log");
  const icon     = $("btn-run-all-icon");
  const btn      = $("btn-run-all");
  if (closeBtn) closeBtn.style.display = "none";
  if (runHint)  runHint.style.display = "inline";
  if (summary)  { summary.style.display = "none"; summary.innerHTML = ""; }
  if (log)      log.innerHTML = "";
  if (btn)      btn.disabled = true;
  if (icon)     icon.textContent = "→";

  // Reset step indicators
  ["import","patterns","ai","queue","deliver"].forEach(s => _stepSet("cstep-"+s, "", ""));
  ["import","patterns","ai","verify","1c"].forEach(s => _pipeSet(s, "", "—"));
  _pipeSet("import","active","…");
  _stepSet("cstep-import","active","");

  _logLine("Старт полного цикла: Импорт → Паттерны → AI → Очередь → 1С", "info");

  // Start polling live events
  _startCyclePoll();

  // Fire the cycle (synchronous, may take a while)
  const limit = 200, aiLimit = 50;
  let result = null;
  try {
    result = await api(`/api/autopilot/cycle?import_limit=${limit}&ai_limit=${aiLimit}&deliver=true`, { method: "POST" });
  } catch(e) {
    result = { ok: false, error: String(e) };
  }

  // Stop poll, do one final poll
  _stopCyclePoll();
  await _pollCycleEvents();

  // Update step indicators from result
  if (result) {
    const imp   = result.import   || {};
    const ai    = result.ai       || {};
    const queue = result.queue    || {};
    const del   = result.delivery || {};
    const stats = result.stats    || {};

    const imported = imp.imported || 0;
    const aiApplied = ai.applied || 0;
    const queued = queue.queued || 0;
    const sent   = del.sent   || 0;

    _stepSet("cstep-import",   imported?"done":"",  imported ? `+${imported}` : "0");
    _stepSet("cstep-ai",       aiApplied?"done":"", aiApplied ? `+${aiApplied}` : "0");
    _stepSet("cstep-queue",    queued?"done":"",    `${queued} в очередь`);
    _stepSet("cstep-deliver",  del.ok?"done":"error", sent ? `+${sent} в 1С` : (del.skipped ? "выкл" : "0"));

    _pipeSet("import",   "done", `+${imported}`);
    _pipeSet("patterns", "done", "✓");
    _pipeSet("ai",       "done", aiApplied?`+${aiApplied}`:"0");
    _pipeSet("verify",   "done", stats.outbox_new || "—");
    _pipeSet("1c",       sent?"done":"", sent?`+${sent}`:"0");

    // Summary block
    const ready  = stats.outbox_new  || 0;
    const errors = stats.outbox_error|| 0;
    const unresolved = (stats.cases||0) - (stats.outbox_sent||0) - (stats.outbox_new||0);

    const summaryHtml = `
      <div style="display:flex;flex-wrap:wrap;gap:12px">
        <div><b>${imported}</b> новых писем</div>
        <div><b>${sent}</b> отправлено в 1С</div>
        <div><b>${ready}</b> в очереди</div>
        ${errors ? `<div style="color:var(--red)"><b>${errors}</b> ошибок</div>` : ""}
        ${unresolved > 0 ? `<div style="color:var(--amber)"><b>${unresolved > 0 ? unresolved : 0}</b> неразобранных</div>` : ""}
      </div>
    `;
    if (summary) { summary.innerHTML = summaryHtml; summary.style.display = "block"; }

    _logLine(result.ok ? `Цикл завершён за ${_elapsedStr()}` : `Завершён с предупреждениями за ${_elapsedStr()}`, result.ok ? "ok" : "warn");
    if (!result.ok && result.error) _logLine("Ошибка: " + result.error, "error");
  }

  // Restore UI
  if (icon)    icon.textContent = "→";
  if (btn)     btn.disabled = false;
  if (closeBtn){ closeBtn.style.display = "inline-block"; }
  if (runHint)  runHint.style.display = "none";
  if ($("cycle-elapsed")) $("cycle-elapsed").textContent = _elapsedStr();
  _cycleRunning = false;

  // Refresh all data
  loadSystemStatus(); loadEmails(); loadPipelineStatus();
  setTimeout(refreshProcessedBadge, 2000);  // фоновое (не блокирует загрузку) число «Обработанных»
}

function closeCycleModal() {
  const modal = $("cycle-modal");
  if (modal) modal.style.display = "none";
}

function _startCyclePoll() {
  _cyclePollTimer = setInterval(_pollCycleEvents, 2000);
  // Elapsed timer
  setInterval(() => {
    if (_cycleRunning && $("cycle-elapsed"))
      $("cycle-elapsed").textContent = _elapsedStr();
  }, 1000);
}
function _stopCyclePoll() {
  if (_cyclePollTimer) { clearInterval(_cyclePollTimer); _cyclePollTimer = null; }
}

async function _pollCycleEvents() {
  try {
    const res = await api(`/api/live/events?limit=50&since_id=${_cycleStartId}`);
    const items = res?.items || [];
    for (const ev of items) {
      if (ev.id > _cycleStartId) _cycleStartId = ev.id;
      const stage = ev.stage || "";
      const msg   = ev.message || "";
      const level = ev.level  || "info";
      // Update step indicators based on stage
      if (stage === "import" && _cycleRunning) { _stepSet("cstep-import","active",""); _pipeSet("import","active","…"); }
      if (stage === "ai"     && _cycleRunning) { _stepSet("cstep-ai","active",""); _pipeSet("ai","active","…"); }
      if (stage === "outbox" && _cycleRunning) { _stepSet("cstep-queue","active",""); }
      // Log the event
      const ts = (ev.created_at||"").substring(11,19);
      _logLine(`[${ts}] [${stage}] ${msg}`, level);
    }
  } catch(e) { /* ignore */ }
}

async function deliverOutbox() {
  toast("Доставка в 1С...");
  const res = await api("/api/outbox/deliver", { method: "POST" });
  toast(res.ok ? `Отправлено: ${res.sent || 0}` : "Ошибка: " + (res.error || ""), res.ok ? "success" : "error");
  loadSystemStatus();
}

async function queueReadyOnce() {
  const btn = $("pipeline-btn-verify-start");
  if (btn) btn.disabled = true;
  const res = await api("/api/export/queue-ready?limit=1000", { method: "POST" });
  const queued = res.queued ?? res.inserted ?? res.created ?? res.count ?? 0;
  toast(res.ok === false ? "Ошибка: " + (res.error || "") : `Сверка: отправлено в очередь 1С ${queued}`, res.ok === false ? "error" : "success");
  if (btn) btn.disabled = false;
  loadPipelineStatus();
  loadOnec();
}

async function stopImport() {
  await api("/api/v2/import/stop", { method: "POST" });
  btnToggle("pipeline-btn-import", false);
  toast("Импорт остановлен");
}

async function startAutopilot(mode = "full_ai") {
  const interval = parseInt($("settings-ap-interval")?.value || "300");
  const deliver  = $("settings-ap-deliver")?.checked || false;
  const res = await api(`/api/autopilot/start?interval_seconds=${interval}&ai_limit=20&deliver=${deliver}&mode=${mode}`, { method: "POST" });
  const label = mode === "full_ai" ? "Автопилот ИИ" : "Автопилот";
  toast(res.ok ? `${label} запущен` : "Ошибка: " + (res.error || ""), res.ok ? "success" : "error");
  autoBtnToggle(true, res.mode || mode);
  // Hide manual step buttons when autopilot is running
  const flow = $("pipeline-manual-flow");
  if (flow) flow.querySelectorAll(".btn-xs").forEach(b => b.style.opacity = "0.4");
}
function startAutopilotAi() { return startAutopilot("full_ai"); }

/* ── Режим обучения: ручные блоки на главной (паттерн-конвейер + ИИ-конвейер) ── */
function applyTrainingMode(on) {
  const blocks = $("manual-blocks");
  if (blocks) blocks.style.display = on ? "" : "none";
  const log = $("ai-live-log");
  if (log) log.style.display = "";   // AI-лог виден во ВСЕХ режимах (паттерн+ИИ, автопилот, полный ИИ)
  const cb = $("s-training-mode");
  if (cb) cb.checked = on;
}

/* ── Аналитика токенов: дни/недели/месяцы, режим × тип ── */
async function purgeJunkAttachments() {
  let dry;
  try { dry = await api("/api/system/purge-junk-attachments?dry_run=true", { method: "POST" }); } catch (e) { toast("Ошибка", "error"); return; }
  if (!dry || !dry.ok) { toast("Ошибка проверки", "error"); return; }
  if (!dry.count) { toast("Помойки нет — вложения чистые", "info"); return; }
  showConfirmModal(
    "Очистить помойку",
    `Удалить ${dry.count} вложений служебных писем (прайс-листы info_only) и освободить ${dry.freed_mb} МБ? Сами письма останутся — удалятся только тяжёлые файлы.`,
    async () => {
      const r = await api("/api/system/purge-junk-attachments?confirm=PURGE&dry_run=false", { method: "POST" });
      if (r && r.ok) { toast(`Очищено: ${r.deleted} вложений, ${r.freed_mb} МБ`, "success"); loadTrafficStats(); }
      else toast("Ошибка: " + (r?.error || ""), "error");
    }
  );
}

async function readCaseLinks(caseId, btn) {
  const out = $(`links-result-${caseId}`);
  if (btn) { btn.disabled = true; btn.textContent = "Читаю…"; }
  let r;
  try { r = await api(`/api/cases/${caseId}/read-links`, { method: "POST" }); } catch (e) { r = { ok: false, error: String(e) }; }
  if (btn) { btn.disabled = false; btn.textContent = "🔗 Прочитать ссылки"; }
  if (!out) return;
  const f = r && r.fields || {};
  const photos = (r && r.photos) || [], docs = (r && r.documents) || [];
  let html = "";
  if (r && r.blocked && !r.processed) html += `<div style="color:var(--amber)">⚠ ${esc(r.hint || "IP сервера забанен источником (403) — нужен прокси/VPN-выход")}</div>`;
  else if (!r || (!r.ok && !photos.length)) { out.innerHTML = `<span style="color:var(--red)">${esc((r && (r.error || r.hint)) || "не удалось прочитать")}</span>`; return; }
  const fstr = Object.entries(f).map(([k, v]) => `${k}: <b>${esc(String(v))}</b>`).join(" · ");
  if (fstr) html += `<div>Со страницы: ${fstr}</div>`;
  if (docs.length) html += `<div>📄 документы: ${docs.map(d => `<a href="${esc(d)}" target="_blank" rel="noopener">скан</a>`).join(" · ")}</div>`;
  if (photos.length) html += `<div style="margin-top:4px">📷 фото (${photos.length}): ${photos.slice(0, 16).map(p => `<a href="${esc(p)}" target="_blank" rel="noopener"><img src="${esc(p)}" style="width:52px;height:52px;object-fit:cover;border-radius:5px;margin:2px;border:1px solid var(--border)" loading="lazy"></a>`).join("")}</div>`;
  out.innerHTML = html || "Ссылки прочитаны, данных не извлечено.";
}

async function readAttachmentsAi(caseId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "ИИ читает акт…"; }
  try {
    const r = await api(`/api/cases/${caseId}/ai-read-attachments`, { method: "POST" });
    if (r && r.ok) {
      toast(`Дочитка вложений: ${r.applied ? "поля обновлены" : "без изменений"}`, "success");
      if (_reviewSelectedId === caseId) selectReviewCase(caseId);
      loadReview();
    } else {
      toast(r?.error || "Не удалось дочитать", "warning");
    }
  } catch (e) { toast("Ошибка: " + e, "error"); }
  if (btn) { btn.disabled = false; btn.textContent = "📎 Дочитать вложения ИИ"; }
}

async function loadTokenTimeline(period) {
  document.querySelectorAll(".tok-period-btn").forEach(b => b.classList.toggle("active", b.dataset.period === period));
  const host = $("tok-timeline");
  if (!host) return;
  let res;
  try { res = await api(`/api/ai/usage-timeline?period=${period}`); } catch (e) { return; }
  const ps = (res && res.periods) || [];
  if (!ps.length) { host.innerHTML = `<div class="muted" style="padding:8px">Данных пока нет.</div>`; return; }
  const k = (o) => { o = o || {}; return `${((o.pt || 0) / 1000).toFixed(1)}k↓ ${((o.ct || 0) / 1000).toFixed(1)}k↑`; };
  host.innerHTML = `<table class="tok-table">
    <thead>
      <tr><th rowspan="2">Период</th><th colspan="2" class="th-pat">Паттерн</th><th colspan="2" class="th-ai">Полный ИИ</th></tr>
      <tr><th class="th-pat">текст</th><th class="th-pat">визуал</th><th class="th-ai">текст</th><th class="th-ai">визуал</th></tr>
    </thead>
    <tbody>${ps.map(p => `<tr>
      <td><b>${esc(p.period)}</b></td>
      <td>${k(p.pattern && p.pattern.text)}</td><td>${k(p.pattern && p.pattern.vision)}</td>
      <td>${k(p.full_ai && p.full_ai.text)}</td><td>${k(p.full_ai && p.full_ai.vision)}</td>
    </tr>`).join("")}</tbody></table>`;
}

/* ── AI-лог «от запроса до вывода» ── */
let _aiLogTimer = null;
function toggleAiLog() {
  const body = $("ai-live-log-body"), caret = $("ai-live-log-caret");
  const open = body.style.display === "none";
  body.style.display = open ? "" : "none";
  if (caret) caret.textContent = open ? "▴" : "▾";
  if (open) loadAiLiveLog();
}
function toggleAiLogAuto(on) {
  if (_aiLogTimer) { clearInterval(_aiLogTimer); _aiLogTimer = null; }
  if (on) { loadAiLiveLog(); _aiLogTimer = setInterval(loadAiLiveLog, 3000); }
}
async function loadAiLiveLog() {
  const body = $("ai-live-log-body");
  if (!body) return;
  let res;
  try { res = await api("/api/ai/live-log?limit=30"); } catch (e) { return; }
  const items = (res && res.items) || [];
  if (!items.length) { body.innerHTML = `<div class="ai-log-empty">Пока пусто — запусти ИИ-прогон или Автопилот ИИ.</div>`; return; }
  body.innerHTML = items.map((it, i) => {
    const ok = it.ok;
    const tok = `вход ${it.prompt_tokens || 0} · выход ${it.completion_tokens || 0}`;
    const ms = it.ms != null ? `${it.ms} мс` : "";
    const head = `<div class="ai-log-row-head">
        <span class="ai-log-dot ${ok ? "ok" : "err"}"></span>
        <b>${esc(it.model || "?")}</b>
        <span class="muted">${esc((it.at || "").replace("T", " ").replace(/\+.*$/, ""))}</span>
        <span class="ai-log-tok">${tok} · ${ms}</span>
      </div>`;
    const reqText = it.request || "";
    const req = `<div class="ai-log-block"><div class="ai-log-label">↗ запрос${reqText.length ? " · " + reqText.length + " симв." : ""}</div><pre class="ai-log-pre req">${esc(reqText || "(пусто)")}</pre></div>`;
    const resp = ok
      ? `<div class="ai-log-block"><div class="ai-log-label">↘ ответ</div><pre class="ai-log-pre">${esc(it.response || "(пусто)")}</pre></div>`
      : `<div class="ai-log-block"><div class="ai-log-label err">✖ ошибка</div><pre class="ai-log-pre err">${esc(it.error || "ошибка")}</pre></div>`;
    return `<div class="ai-log-entry">${head}${req}${resp}</div>`;
  }).join("");
}
function setTrainingMode(on) {
  localStorage.setItem("readmail.trainingMode", on ? "1" : "0");
  applyTrainingMode(!!on);
}
function initTrainingMode() {
  applyTrainingMode(localStorage.getItem("readmail.trainingMode") === "1");
}
async function runFullAiBatch() {
  // По конкретному кейсу — если введён номер.
  const caseId = ($("fullai-caseid")?.value || "").trim();
  if (caseId) {
    const runBtn = $("btn-fullai-run");
    if (runBtn) { runBtn.disabled = true; runBtn.textContent = "ИИ…"; }
    try {
      const r = await api(`/api/ai/run-one?case_id=${encodeURIComponent(caseId)}`, { method: "POST" });
      if (r && r.ok) { toast(`ИИ по кейсу #${caseId}: ${r.applied ? "применено" : "без изменений"}${r.ready_for_export ? ", готов к 1С" : ""}`, "success"); selectReviewCase && _reviewSelectedId === +caseId && selectReviewCase(+caseId); loadReview(); }
      else toast(`Кейс #${caseId}: ${r?.error || "ошибка"}`, "error");
    } catch (e) { toast("Ошибка: " + e, "error"); }
    if (runBtn) { runBtn.disabled = false; runBtn.textContent = "Прогон"; }
    return;
  }
  const limit = parseInt($("fullai-limit")?.value || "20");
  const order = $("fullai-scope")?.value || "new";
  const start = await api(`/api/ai/run-batch?limit=${limit}&target=returns&order=${order}`, { method: "POST" });
  if (!start.ok) { toast("Ошибка запуска ИИ: " + (start.error || ""), "error"); return; }
  const runBtn = $("btn-fullai-run"), stopBtn = $("btn-fullai-stop");
  if (runBtn) runBtn.style.display = "none";
  if (stopBtn) stopBtn.style.display = "";
  toast("ИИ-прогон запущен в фоне…");
  const poll = async () => {
    const p = await api("/api/ai/batch-progress");
    const st = p.state || {};
    const done = st.processed || 0, total = st.total || 0;
    const c = $("pipe-count-fullai"); if (c) c.textContent = total ? `${done}/${total}` : "…";
    if (st.running) { setTimeout(poll, 2500); return; }
    if (runBtn) runBtn.style.display = "";
    if (stopBtn) stopBtn.style.display = "none";
    if (st.error) toast("ИИ: " + st.error, "error");
    else if (total === 0) toast("ИИ: свежих возвратов нет (сначала Импорт для создания кейсов)", "info");
    else toast(`ИИ-прогон: ${done}/${total}, готово к 1С: ${st.resolved || 0}`, "success");
    loadReview();
  };
  setTimeout(poll, 1500);
}
async function stopFullAiBatch() {
  await api("/api/ai/stop-batch", { method: "POST" });
  const runBtn = $("btn-fullai-run"), stopBtn = $("btn-fullai-stop");
  if (stopBtn) stopBtn.style.display = "none";
  if (runBtn) runBtn.style.display = "";
  toast("ИИ-прогон остановлен");
}

/* ── Авто-скан почты: периодически подгружать новые письма ── */
let _autoScanTimer = null;
function toggleAutoScan(on) {
  localStorage.setItem("readmail.autoScan", on ? "1" : "0");
  if (_autoScanTimer) { clearInterval(_autoScanTimer); _autoScanTimer = null; }
  if (on) {
    toast("Авто-скан почты включён (каждые 3 мин)", "success");
    _autoScanTimer = setInterval(() => { try { runImport(); } catch (e) {} }, 180000);
  } else {
    toast("Авто-скан почты выключен");
  }
}
function initAutoScan() {
  const on = localStorage.getItem("readmail.autoScan") === "1";
  const cb = $("s-auto-scan");
  if (cb) cb.checked = on;
  if (on) toggleAutoScan(true);
}

async function stopAutopilot() {
  await api("/api/autopilot/stop", { method: "POST" });
  autoBtnToggle(false);
  toast("Автопилот остановлен");
  // Restore manual step buttons
  const flow = $("pipeline-manual-flow");
  if (flow) flow.querySelectorAll(".btn-xs").forEach(b => b.style.opacity = "1");
}

/* ──────────────────────── Системный статус ──────────────────────── */

async function loadSystemStatus() {
  try {
    const [sysRes, tokenRes, pipeRes, importHealth, trafficRes, serverCount, modeRes, reportRes] = await Promise.all([
      api("/api/v2/system/status"),
      api("/api/ai/token-stats").catch(() => ({})),
      api("/api/v2/pipeline/status").catch(() => ({})),
      api("/api/v2/import/health").catch(() => ({})),
      api("/api/system/traffic-stats").catch(() => ({})),
      api("/api/import/server-total").catch(() => ({})),
      api("/api/ai/usage-by-mode").catch(() => ({})),
      api("/api/ai/token-report").catch(() => ({})),
    ]);
    if (sysRes.error) return;
    const sb = $("system-status-bar");
    if (!sb) return;
    const mailOk = (sysRes.mail || {}).configured;
    const total = (sysRes.stats || {}).total_emails || 0;
    const cases = (sysRes.stats || {}).total_cases || 0;
    const outboxNew = (sysRes.outbox || {}).new || 0;
    const outboxErr = (sysRes.outbox || {}).error || 0;
    const outboxSent = (sysRes.outbox || {}).sent || 0;
    const todayTokens = (tokenRes?.today?.tokens_approx) || 0;
    const trafficMb = Number(trafficRes?.total?.traffic_mb || 0);
    const storageMb = Number(trafficRes?.total?.storage_mb || 0);

    // live-индикатор
    const importBusy = pipeRes.import_busy || _pipelineState.importBusy || false;
    const patternsBusy = pipeRes.patterns_busy || _pipelineState.patternsBusy || false;
    const aiBusy = pipeRes.ai_busy || _pipelineState.aiBusy || false;
    const autopilotBusy = pipeRes.autopilot_running || _pipelineState.autopilot || false;
    const anyBusy = importBusy || patternsBusy || aiBusy || autopilotBusy;
    const activityText = importBusy ? "почта" : patternsBusy ? "паттерны" : aiBusy ? "AI" : autopilotBusy ? "авто" : "стоит";
    const activityClass = anyBusy ? "busy" : "idle";

    sb.innerHTML = `
      <span class="status-chip activity ${activityClass}" title="Лампочка активности: жёлтая, когда работает импорт, ИИ или автопилот">
        <span class="status-lamp ${activityClass}"></span>${activityText}
      </span>
      ${(() => {
        const tot = (reportRes && reportRes.total) || {};
        const t = tot.total || {in:0,out:0};
        const avg = Math.round(tot.avg_tokens_per_email||0).toLocaleString("ru");
        const fk = (n)=>`${((n||0)/1000).toFixed(1)}k`;
        return `<span class="status-chip token-chip token-chip-total" title="Итого по всем прогонам: вход ${t.in||0} ↓ / выход ${t.out||0} ↑. Среднее на письмо ${avg} ток. (${tot.emails||0} писем)"><span class="status-label">Σ</span><b>${fk(t.in)}↓ ${fk(t.out)}↑</b><span class="muted">·${avg}/пис</span></span>`;
      })()}
      <span class="status-chip optional" title="Мегабайты трафика / локального хранилища: почта, вложения, AI API, 1С JSON"><span class="status-label">MB</span><b>${formatMbShort(trafficMb)}</b><span class="muted">/${formatMbShort(storageMb)}</span></span>
      <span class="status-chip optional" title="Писем в базе"><span class="status-label">Пис</span><b>${total}</b></span>
      <span class="status-chip optional" title="Кейсов после паттернов/AI"><span class="status-label">Кейс</span><b>${cases}</b></span>
      <span class="status-chip optional" title="Очередь 1С: новые / ошибки / отправлено"><span class="status-label">1С</span><b>${outboxNew}</b><span class="muted">/${outboxErr}/${outboxSent}</span></span>
      <span class="status-chip import-health ${esc(((serverCount.failed_or_stuck || serverCount.stuck || 0) > 0 ? "error" : null) || importHealth.level || "ok")}" title="${esc((importHealth.message || "Импорт без явных проблем") + (serverCount.server_total != null ? ` · server total ${serverCount.server_total}, local raw ${serverCount.local_raw_total}` + ((serverCount.failed_or_stuck || 0) > 0 ? `, failed/stuck ${serverCount.failed_or_stuck}` : "") + (serverCount.count_gap_estimate ? ` · разница счётчиков ${serverCount.count_gap_estimate}, требуется UID-сверка` : "") + (serverCount.stale ? " · обновляется…" : "") : ""))}"><span class="status-dot-sm ${((serverCount.failed_or_stuck || serverCount.stuck || 0) > 0 || importHealth.level === "error") ? "error" : importHealth.level === "warn" ? "busy" : "ok"}"></span>Импорт${serverCount.server_total != null ? ` · S ${serverCount.server_total} / L ${serverCount.local_raw_total}` : ""}</span>
      <span class="status-chip optional-wide" title="Сервер приложения отвечает"><span class="status-dot-sm ok"></span>Сервер</span>
      <span class="status-chip optional-wide" title="${mailOk ? "Почта настроена" : "Почта не настроена"}"><span class="status-dot-sm ${mailOk ? "ok" : "idle"}"></span>Почта</span>
    `;
    if ($("token-count")) $("token-count").textContent = todayTokens.toLocaleString("ru");
  } catch (e) { /* ignore */ }
}

function downloadCompareJson() {
  toast("Готовлю JSON выгрузку...");
  window.location.href = "/api/export/compare-json/download";
}

function formatMbShort(v) {
  const n = Number(v || 0);
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n < 10) return n.toFixed(2);
  if (n < 100) return n.toFixed(1);
  return Math.round(n).toLocaleString("ru");
}

function formatMb(v) {
  return `${formatMbShort(v)} MB`;
}

async function loadTrafficStats() {
  const res = await api("/api/system/traffic-stats");
  if (!res || res.error) {
    setInner("tr-total", "ошибка");
    return;
  }
  setInner("tr-total", `${formatMb(res?.total?.traffic_mb)} трафик / ${formatMb(res?.total?.storage_mb)} хранение`);
  setInner("tr-mail", `${formatMb(res?.mail?.transfer_mb)} (${res.total_emails || 0} писем)`);
  setInner("tr-ai", `${formatMb(res?.ai?.total_mb)} (${res?.ai?.requests || 0} запросов)`);
  setInner("tr-1c", formatMb(res?.one_c?.total_mb));
  setInner("tr-storage", `${formatMb(res?.storage?.total_mb)}: БД ${formatMb((res?.storage?.db_file_bytes || 0) / 1024 / 1024)}, вложения ${formatMb((res?.storage?.attachments_disk_bytes || 0) / 1024 / 1024)}`);
}

async function loadMailHealth() {
  const el = $("mail-health-status");
  if (!el) return;
  el.className = "mail-health-card idle";
  el.textContent = "Проверяю импорт...";
  const res = await api("/api/v2/import/health");
  if (!res.ok) {
    el.className = "mail-health-card error";
    el.textContent = "Не удалось проверить импорт: " + (res.error || "ошибка");
    return;
  }
  const level = res.level || "ok";
  const limits = res.limits || {};
  const counts = res.counts || {};
  const job = res.last_job || {};
  const err = res.recent_error || null;
  const rows = [
    `<div class="mail-health-title"><span class="status-dot-sm ${level === "error" ? "error" : level === "warn" ? "busy" : "ok"}"></span>${esc(res.message || "Импорт без явных проблем")}</div>`,
    `<div class="mail-health-grid">
       <span>Письмо до</span><b>${esc(limits.imap_max_raw_email_mb || "—")} MB</b>
       <span>Вложение до</span><b>${esc(limits.import_max_attachment_mb || "—")} MB</b>
       <span>Batch</span><b>${esc(limits.imap_batch_size || "—")}</b>
       <span>Карантин</span><b>${esc(counts.quarantine || 0)}</b>
       <span>Oversized</span><b>${esc(counts.oversized || 0)}</b>
       <span>Повтор</span><b>${esc(counts.retry_pending || 0)}</b>
     </div>`,
  ];
  if (job.job_id) {
    rows.push(`<div class="mail-health-note">Последний job: ${esc(job.status || "—")} · ${esc(job.stage || "—")} · импортировано ${esc(job.imported || 0)} / обработано ${esc(job.processed || 0)}</div>`);
  }
  if (err?.type) {
    rows.push(`<div class="mail-health-note">Последняя ошибка: ${esc(err.type)} ${err.uid ? "UID " + esc(err.uid) : ""} · ${esc(err.mailbox || "")}</div>`);
  }
  el.className = "mail-health-card " + (level === "error" ? "error" : level === "warn" ? "warn" : "ok");
  el.innerHTML = rows.join("");
}

/* ──────────────────────── ПИСЬМА ──────────────────────── */

let emailsPage = loadSavedPage("emails");

async function loadMailReconcileSummary() {
  const card = $("mail-reconcile-card");
  if (!card) return;
  const data = await api("/api/import/reconcile-summary");
  const summary = data.summary || {};
  if (!data.ok || !summary.checked_at) {
    card.className = "evidence-band";
    card.innerHTML = `<div><span class="evidence-band-label">Сверка почты</span><strong>Нет готового UID-отчёта</strong></div>`;
    return;
  }
  const missing = Number(summary.missing_local_total || 0);
  const errors = Number(summary.fetch_failed_total || 0);
  // read-only сводка карантина (необязательная — не ломаем карточку, если endpoint недоступен)
  const q = await api("/api/import/quarantine/summary").catch(() => ({}));
  const quarantined = Number(q.quarantined || 0);
  const needsBackfill = missing > 0 || errors > 0 || quarantined > 0;
  card.className = "evidence-band" + (missing > 0 ? " reconcile-danger" : "");
  card.innerHTML = `
    <div><span class="evidence-band-label">Server</span><strong>${Number(summary.server_total || 0).toLocaleString("ru-RU")}</strong></div>
    <div><span class="evidence-band-label">Local raw</span><strong>${Number(summary.local_raw_total || 0).toLocaleString("ru-RU")}</strong></div>
    <div><span class="evidence-band-label">Missing</span><strong>${missing}</strong></div>
    <div><span class="evidence-band-label">Duplicates linked</span><strong>${Number(summary.duplicate_linked_total || 0)}</strong></div>
    <div><span class="evidence-band-label">Fetch failed</span><strong>${errors}</strong></div>
    <div><span class="evidence-band-label">Quarantine</span><strong>${quarantined}</strong></div>
    ${needsBackfill ? `<div class="reconcile-alert">Есть письма на сервере без локальной raw. Targeted backfill (CLI, read-only-safe): <code>python3 scripts/backfill_missing_imap_uids.py --from-missing audit_out/imap_reconcile_missing_server_uids.jsonl --apply</code></div>` : ""}
  `;
}

async function loadEmails() {
  try {
    const filter = $("emails-filter")?.value || "all";
    const buyer = $("emails-buyer")?.value || "";
    const search = $("emails-search")?.value || "";
    const sort = $("emails-sort")?.value || "date_desc";
    const pageSize = parseInt($("emails-page-size")?.value || "50");
    const res = await api(`/api/emails?filter=${filter}&buyer=${encodeURIComponent(buyer)}&q=${encodeURIComponent(search)}&sort=${sort}&page=${emailsPage}&limit=${pageSize}`);
    if (res.error) { toast("Ошибка загрузки писем", "error"); return; }
    const cnt = $("emails-count");
    if (cnt) cnt.textContent = `${res.total || 0} писем`;
    const tbody = $("emails-tbody");
    if (!tbody) return;
    const list = res.emails || [];
    // Защита: если на текущей странице пусто, но всего писем больше нуля — значит
    // страница вышла за диапазон (после фильтра/импорта). Сбрасываем на 1-ю и перезагружаем.
    if (list.length === 0 && (res.total || 0) > 0 && emailsPage > 1) {
      emailsPage = 1;
      return loadEmails();
    }
    tbody.innerHTML = "";
    list.forEach(e => {
     try {
      const kind = KIND_LABELS[e.claim_kind] || "";
      const prColor = PRIORITY_COLORS[e.priority] || "gray";
      const stateLabel = STATE_LABELS[e.state || e.status] || e.state || e.status || "—";
      const detailParts = [];
      if (e.part_number) detailParts.push(e.part_number);
      if (e.brand) detailParts.push(e.brand);
      if (e.document_number) detailParts.push(e.document_number);
      const detailStr = detailParts.length ? `<br><span style="font-size:11px;color:var(--text-muted)">${detailParts.join(" · ")}</span>` : "";
      // follow-up индикатор
      const isFollowup = e.event_type === "followup_reminder" || e.event_type === "followup_dialog" || e.event_type === "supplier_decision";
      const followupBadge = isFollowup
        ? `<span class="badge badge-amber" style="font-size:9px" title="Продолжение диалога">${EVENT_LABELS[e.event_type] || e.event_type}</span> `
        : "";
      const tr = document.createElement("tr");
      tr.className = "email-row" + (isFollowup ? " row-followup" : "");
      tr.onclick = () => openEmailDetail(e.id);
      tr.innerHTML = `
        <td>${e.has_attachments ? "📎" : ""}${isFollowup ? "→" : ""}</td>
        <td><b>${esc(e.buyer_name || "—")}</b><br><span style="font-size:11px;color:var(--text-muted)">${esc(e.from_addr || "")}</span></td>
        <td>${followupBadge}${esc(e.subject || "—")}${detailStr}</td>
        <td>${kind ? badge(kind, "blue") : ""}</td>
        <td>${badge(stateLabel, prColor)}</td>
        <td style="white-space:nowrap">${fmtDate(e.received_at)}</td>
      `;
      tbody.appendChild(tr);
     } catch (rowErr) { console.warn("email row render error id=", e && e.id, rowErr); }
    });
    renderPagination("emails-pagination", res.total || 0, emailsPage, pageSize,
      function(p) { emailsPage = p; savePage("emails", p); loadEmails(); });
    populateBuyerFilter(res.buyers || []);
  } catch (e) { console.warn("loadEmails error:", e); }
}

function populateBuyerFilter(buyers) {
  const sel = $("emails-buyer");
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">Все клиенты</option>';
  buyers.forEach(b => {
    const opt = document.createElement("option");
    opt.value = b.code; opt.textContent = b.name;
    if (b.code === current) opt.selected = true;
    sel.appendChild(opt);
  });
}

/* ──────────────────────── ПАТТЕРНЫ (split view) ──────────────────────── */

let _selectedPatternId = null;
// Кэши данных — чтобы не передавать JSON через onclick (ломает парсер с русским текстом)
const _patternCache = new Map();
const _aiCache = new Map();


async function selectSplitCase(tab, id) {
  if (tab === "pattern") {
    _selectedPatternId = id;
    document.querySelectorAll("#patterns-case-list .split-item").forEach(el =>
      el.classList.toggle("active", el.dataset.id == id)
    );
  } else {
    _selectedAiId = id;
    document.querySelectorAll("#ai-case-list .split-item").forEach(el =>
      el.classList.toggle("active", el.dataset.id == id)
    );
  }
  // Берём из кэша для быстрой отрисовки, потом грузим полные данные с body_text
  const cached = tab === "pattern" ? _patternCache.get(id) : _aiCache.get(id);
  if (cached) renderSplitCase(tab, cached);
  const caseData = await api(`/api/cases/${id}`);
  if (caseData && !caseData.error) renderSplitCase(tab, caseData);
}

// Очистка HTML письма: убираем скрипты/стили/обработчики, оставляем таблицы и форматирование
function sanitizeEmailHtml(html) {
  let s = String(html || "");
  // Удаляем опасные блоки
  s = s.replace(/<script[\s\S]*?<\/script>/gi, "");
  s = s.replace(/<style[\s\S]*?<\/style>/gi, "");
  s = s.replace(/<head[\s\S]*?<\/head>/gi, "");
  s = s.replace(/<!--[\s\S]*?-->/g, "");
  // Убираем on*-атрибуты (onclick и т.п.) и javascript: ссылки
  s = s.replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, "");
  s = s.replace(/javascript:/gi, "");
  // Убираем внешние картинки-трекеры по желанию оставляем — они помогают видеть письмо
  return s;
}

// Переключатель вида письма HTML <-> текст
function toggleEmailView(caseId, mode) {
  const htmlEl = $("email-html-" + caseId);
  const textEl = $("email-text-" + caseId);
  const hb = $("vbtn-html-" + caseId);
  const tb = $("vbtn-text-" + caseId);
  if (!htmlEl || !textEl) return;
  if (mode === "text") {
    htmlEl.style.display = "none"; textEl.style.display = "";
    tb?.classList.add("active"); hb?.classList.remove("active");
  } else {
    htmlEl.style.display = ""; textEl.style.display = "none";
    hb?.classList.add("active"); tb?.classList.remove("active");
  }
}

function renderSplitCase(tab, c) {
  const fieldsEl = $(tab === "pattern" ? "patterns-fields-content" : "ai-fields-content");
  const emailEl = $(tab === "pattern" ? "patterns-email-content" : "ai-email-content");
  const titleEl = $(tab === "pattern" ? "patterns-case-title" : "ai-case-title");
  const actionsEl = $(tab === "pattern" ? "patterns-case-actions" : "ai-case-actions");
  if (!fieldsEl || !emailEl) return;

  if (titleEl) titleEl.textContent = `— ${c.buyer_name || ""}`;

  const f = c.fields || {};
  const quality = c.quality || [];
  const missing = c.missing || [];
  const evidenceGate = c.evidence_gate || c.payload?.evidence_gate || {};
  const buyerEvidenceMeta = evidenceGate.field_audit?.buyer_code?.evidence_meta || {};
  const buyerMismatchHtml = (buyerEvidenceMeta.mismatch_classifications || []).map((item) =>
    `<div style="color:${item.severity === "error" ? "var(--red)" : "var(--amber)"};margin-top:3px">
      Контрагент подтверждён по профилю. В тексте найдено: ${esc(item.detected_name || item.detected_code || "—")}.
      Класс расхождения: ${esc(item.mismatch_class || "unknown_mismatch")}
    </div>`
  ).join("");

  const validateField = (key, val) => {
    if (!val && val !== 0) return "missing";
    if (["price", "quantity", "sum"].includes(key)) {
      const num = parseFloat(String(val).replace(/\s/g, "").replace(",", "."));
      if (isNaN(num)) return "error";
    }
    return "ok";
  };

  const FIELD_LABELS = {
    document_number: "№ документа",
    document_date: "Дата документа",
    part_number: "Артикул",
    quantity: "Количество",
    price: "Цена",
    product_name: "Наименование",
    brand: "Бренд",
    claim_kind: "Причина возврата",
    buyer_name: "Клиент",
    sum: "Сумма",
    claim_number: "№ претензии",
    comment: "Комментарий",
  };

  const allFields = { ...f };
  if (c.claim_kind) allFields.claim_kind = KIND_LABELS[c.claim_kind] || c.claim_kind;
  if (c.buyer_name) allFields.buyer_name = c.buyer_name;

  const rows = Object.entries(FIELD_LABELS).map(([key, label]) => {
    const val = allFields[key];
    const status = validateField(key, val);
    const req = ["document_number", "part_number"].includes(key);
    const statusIcon = status === "ok" ? `<span style="color:var(--green)">✓</span>`
      : status === "error" ? `<span style="color:var(--red)">не число</span>`
      : req ? `<span style="color:var(--red)">✗ нет</span>`
      : `<span style="color:var(--text-muted)">—</span>`;
    return `<tr class="${status === "error" ? "row-error" : ""}">
      <td>${label}${req ? ' <span class="required-star">*</span>' : ""}</td>
      <td class="field-val">${esc(String(val || ""))}</td>
      <td>${statusIcon}</td>
    </tr>`;
  }).join("");

  const qualityHtml = quality.length
    ? `<div style="margin-top:10px">${quality.map(q =>
        `<div style="font-size:12px;color:${q.level === "error" ? "var(--red)" : "var(--amber)"};padding:1px 0">${esc(q.message || q.code || "")}</div>`
      ).join("")}</div>` : "";

  const missingHtml = missing.length
    ? `<div style="margin-top:6px;font-size:12px;color:var(--amber)">Не хватает: ${missing.map(esc).join(", ")}</div>` : "";
  const evidenceHtml = Object.keys(evidenceGate).length
    ? `<div style="margin-top:8px;padding:7px;border:1px solid ${evidenceGate.passed ? "var(--green)" : "var(--amber)"};border-radius:4px;font-size:11px">
        <b style="color:${evidenceGate.passed ? "var(--green)" : "var(--amber)"}">Evidence: ${evidenceGate.passed ? "пройден" : "нужна сверка"}</b>
        <div style="margin-top:4px">${Object.entries(evidenceGate.field_statuses || {}).map(([key, value]) => `${esc(key)}: ${esc(value)}`).join(" · ")}</div>
        ${(evidenceGate.repairs || []).length ? `<div style="color:var(--green);margin-top:3px">Восстановлено: ${(evidenceGate.repairs || []).map((item) => `${esc(item.field)} (${esc(item.repair_method)})`).join(", ")}</div>` : ""}
        ${(evidenceGate.blocking_errors || []).length ? `<div style="color:var(--red);margin-top:3px">${(evidenceGate.blocking_errors || []).map(esc).join(", ")}</div>` : ""}
        ${(evidenceGate.blocking_warnings || []).length ? `<div style="color:var(--amber);margin-top:3px">${(evidenceGate.blocking_warnings || []).map(esc).join(", ")}</div>` : ""}
        ${(evidenceGate.non_blocking_warnings || []).length ? `<div style="color:var(--amber);margin-top:3px">${(evidenceGate.non_blocking_warnings || []).map(esc).join(", ")}</div>` : ""}
        ${buyerMismatchHtml}
      </div>`
    : "";

  const priorityLabel = PRIORITY_LABELS[c.priority] || c.priority || "";
  const stateLabel = STATE_LABELS[c.state] || c.state || "?";

  // Строим редактируемые поля с подсветкой missing/ok
  const EDIT_FIELDS = [
    ["document_number", "№ документа", true],
    ["document_date",   "Дата",        true],
    ["part_number",     "Артикул" + (c.claim_kind === "wrong_item" ? " (заказан)" : ""), true],
    // Пересорт: фактически приехавший артикул — отдельным окном.
    ...(c.claim_kind === "wrong_item" ? [["received_part_number", "Факт: приехал арт.", false]] : []),
    ["brand",           "Бренд",       false],
    ["product_name",    "Наименование",false],
    ["quantity",        "Количество",  false],
    ["price",           "Цена",        false],
    ["claim_number",    "№ претензии", false],
    ["comment",         "Комментарий", false],
  ];
  const missingSet = new Set(c.missing || []);
  const editRows = EDIT_FIELDS.map(([key, label, required]) => {
    const val = f[key] || "";
    const isMissing = missingSet.has(key) || (required && !val);
    const hasVal = !!val;
    const statusIcon = hasVal
      ? `<span style="color:var(--green);font-size:13px">✓</span>`
      : required
        ? `<span style="color:var(--red);font-size:13px">✗</span>`
        : `<span style="color:var(--text-muted)">—</span>`;
    return `<div class="edit-field-row ${isMissing ? 'edit-field-missing' : hasVal ? 'edit-field-ok' : ''}">
      <label class="edit-field-label">${esc(label)}${required ? ' <span style="color:var(--red)">*</span>' : ''}</label>
      <div style="display:flex;align-items:center;gap:4px;flex:1">
        <input class="train-input edit-field-input" data-field="${key}"
          value="${esc(val)}" placeholder="${esc(label)}"
          oninput="this.closest('.edit-field-row').classList.toggle('edit-field-ok', !!this.value); this.closest('.edit-field-row').classList.toggle('edit-field-missing', !this.value && ${required})">
        ${statusIcon}
      </div>
    </div>`;
  }).join("");

  const trainArea = `
    <div class="split-train-area">
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <button class="pstep-btn run" style="flex:1" onclick="saveTrainCase(${c.id}, false)">
          Сохранить
        </button>
        <button class="pstep-btn run" style="flex:2;border-color:var(--accent);color:var(--accent)"
          onclick="saveTrainCase(${c.id}, true)" title="Сохранить поля и обучить AI по этому письму">
          Сохранить и обучить AI
        </button>
        ${c.state === 'ready_to_1c' || !missingSet.size
          ? `<button class="pstep-btn run" style="border-color:var(--green);color:var(--green)" onclick="approveReviewCase(${c.id})">→ 1С</button>`
          : ''}
      </div>
      <div id="train-result-${c.id}" style="font-size:12px;margin-top:6px"></div>
    </div>
  `;

  fieldsEl.innerHTML = `
    <div style="padding:8px">
      <div style="font-size:11px;color:var(--text-muted);margin-bottom:8px;display:flex;gap:6px;flex-wrap:wrap">
        <span>Кейс #${c.id}</span>·
        <span>Увер.: <b>${Math.round((c.confidence || 0) * 100)}%</b></span>
        ${c.claim_kind ? badge(KIND_LABELS[c.claim_kind] || c.claim_kind, "blue") : ""}
        ${badge(stateLabel, c.state === "ready_to_1c" ? "green" : c.state === "needs_review" ? "amber" : "gray")}
      </div>
      <div class="edit-fields-block">${editRows}</div>
      ${qualityHtml}${missingHtml}${evidenceHtml}
    </div>
    ${trainArea}
  `;

  // Email body — HTML с таблицами (по умолчанию) + переключатель на текст
  const body = c.visible_text || c.body_text || c.snippet || "";
  const rawHtml = c.body_html || "";
  const hasHtml = rawHtml && rawHtml.length > 20;
  const safeHtml = hasHtml ? sanitizeEmailHtml(rawHtml) : "";
  const plainBlock = body
    ? `<pre class="email-pre">${esc(body.length > 12000 ? body.slice(0, 12000) + "\n\n... (обрезано)" : body)}</pre>`
    : `<div style="color:var(--text-muted);padding:20px">Текст письма недоступен</div>`;
  const htmlBlock = hasHtml
    ? `<div class="email-html-view">${safeHtml}</div>`
    : plainBlock;
  // По умолчанию показываем HTML (с таблицами), если он есть
  const bodyHtml = hasHtml
    ? `<div class="email-view-toggle">
         <button class="btn-sm active" id="vbtn-html-${c.id}" onclick="toggleEmailView(${c.id},'html')">Как в письме (таблицы)</button>
         <button class="btn-sm" id="vbtn-text-${c.id}" onclick="toggleEmailView(${c.id},'text')">Чистый текст</button>
       </div>
       <div id="email-html-${c.id}">${htmlBlock}</div>
       <div id="email-text-${c.id}" style="display:none">${plainBlock}</div>`
    : plainBlock;

  // Вложения с кнопками
  const attachments = c.attachments || [];
  const attachHtml = attachments.length ? `
    <div style="margin-bottom:10px" id="att-block-${c.id}">
      <div style="font-size:11px;font-weight:600;color:var(--text-muted);margin-bottom:4px">ВЛОЖЕНИЯ (${attachments.length})</div>
      ${attachments.map(a => {
        const ext = (a.filename || "").split(".").pop().toLowerCase();
        const icon = ["xlsx","xls","xlsm"].includes(ext) ? "📊" : ext === "pdf" ? "📄" : ext === "csv" ? "📑" : ["jpg","jpeg","png","gif","webp","bmp"].includes(ext) ? "🖼" : ext === "zip" ? "📦" : "📎";
        const canPreview = ["xlsx","xls","xlsm","csv","zip"].includes(ext);
        const canVision = ["jpg","jpeg","png","gif","pdf"].includes(ext);
        const size = a.size_bytes ? ` <span style="color:var(--text-muted)">(${Math.round(a.size_bytes/1024)} KB)</span>` : "";
        const attId = a.id || a._db_id || "";
        return `<div style="font-size:12px;padding:4px 0;display:flex;align-items:center;gap:6px;flex-wrap:wrap">
          <span>${icon}</span>
          <b>${esc(a.filename || "—")}</b>${size}
          ${attId && canPreview ? `<button class="btn-sm" style="padding:1px 8px;font-size:11px" onclick="previewAttachment(${attId},'${escJs(a.filename || "")}')">Открыть</button>` : ""}
          ${attId && canVision ? `<button class="btn-sm" style="padding:1px 8px;font-size:11px;color:var(--accent)" onclick="visionAttachment(${attId},'${escJs(a.filename || "")}')">Vision AI</button>` : ""}
          ${attId ? `<a href="/api/attachments/${attId}/download" target="_blank" class="btn-sm" style="padding:1px 8px;font-size:11px;text-decoration:none">D</a>` : ""}
        </div>`;
      }).join("")}
      <div id="att-preview-${c.id}" style="margin-top:8px"></div>
    </div>` : "";

  const linksHtml = (() => {
    const urls = (body.match(/https?:\/\/[^\s<>"]+/g) || []).slice(0, 8);
    return urls.length ? `<div style="margin-bottom:8px">
      <div style="font-size:11px;font-weight:600;color:var(--text-muted);margin-bottom:3px">ССЫЛКИ</div>
      ${urls.map(u => `<div style="font-size:11px"><a href="${esc(u)}" target="_blank" style="color:var(--accent)">${esc(u.slice(0, 90))}</a></div>`).join("")}
    </div>` : "";
  })();

  emailEl.innerHTML = `
    <div style="font-size:13px;font-weight:600;margin-bottom:3px">${esc(c.subject || "—")}</div>
    <div style="font-size:11px;color:var(--text-muted);margin-bottom:10px">
      <b>От:</b> ${esc(c.from_addr || "—")} &nbsp;·&nbsp; ${fmtDate(c.received_at)}
    </div>
    ${attachHtml}${linksHtml}
    <div style="font-size:11px;font-weight:600;color:var(--text-muted);margin-bottom:6px">ТЕКСТ ПИСЬМА</div>
    ${bodyHtml}
  `;

  // Если есть тред — загружаем и показываем историю
  if (c.thread_key && fieldsEl) {
    api(`/api/v2/thread/${encodeURIComponent(c.thread_key)}`).then(threadRes => {
      if (!threadRes.ok || threadRes.count <= 1) return;
      const followups = (threadRes.items || []).filter(t =>
        t.event_type === "followup_reminder" || t.event_type === "followup_dialog" || t.event_type === "supplier_decision"
      );
      if (!followups.length) return;
      const threadHtml = `
        <div style="margin-top:14px;border-top:1px solid var(--border);padding-top:10px">
          <div style="font-size:11px;font-weight:600;color:var(--amber);margin-bottom:6px">
            ПРОДОЛЖЕНИЯ ДИАЛОГА (${followups.length})
          </div>
          ${followups.map(t => `
            <div style="font-size:12px;background:var(--amber-light);border:1px solid var(--amber);border-radius:4px;padding:6px 8px;margin-bottom:4px">
              <div style="font-weight:500">${esc(EVENT_LABELS[t.event_type] || t.event_type)} · ${fmtDate(t.received_at)}</div>
              <div style="color:var(--text-muted);margin-top:2px">${esc((t.snippet || "").slice(0, 120))}</div>
            </div>`).join("")}
        </div>`;
      fieldsEl.innerHTML += threadHtml;
    }).catch(() => {});
  }

  if (actionsEl) {
    actionsEl.style.display = "flex";
    const caseId = c.id;
    if (tab === "pattern") {
      const btnExport = $("patterns-btn-export");
      const btnAi = $("patterns-btn-ai");
      const btnClose = $("patterns-btn-close");
      if (btnExport) btnExport.onclick = () => exportCase(caseId);
      if (btnAi) btnAi.onclick = async () => {
        toast("AI улучшает паттерн...");
        const r = await api(`/api/cases/${caseId}/ai_apply`, { method: "POST" });
        toast(r.ok ? "AI применён" : "Ошибка: " + (r.error || ""), r.ok ? "success" : "error");
        selectSplitCase("pattern", caseId, {});
      };
      if (btnClose) btnClose.onclick = async () => {
        await api(`/api/cases/${caseId}/close`, { method: "POST" });
        toast("Закрыт");
      };
    } else {
      const btnConfirm = $("ai-btn-confirm");
      const btnRerun = $("ai-btn-rerun");
      const btnClose = $("ai-btn-close");
      if (btnConfirm) btnConfirm.onclick = () => confirmAiCase(caseId);
      if (btnRerun) btnRerun.onclick = async () => {
        toast("Перезапуск AI...");
        const r = await api(`/api/cases/${caseId}/ai_apply`, { method: "POST" });
        toast(r.ok ? "Готово" : "Ошибка: " + (r.error || ""), r.ok ? "success" : "error");
        selectSplitCase("ai", caseId, {});
      };
      if (btnClose) btnClose.onclick = async () => {
        await api(`/api/cases/${caseId}/close`, { method: "POST" });
        toast("Закрыт"); loadAiReview();
      };
    }
  }
}


/* ──────────────────────── AI-РАЗБОР (split view) ──────────────────────── */

let _aiPage = loadSavedPage("ai");
let _selectedAiId = null;

async function loadAiReview() {
  try {
    const q = $("ai-search")?.value || "";
    const res = await api(`/api/v2/cases/by-method?method=ai&limit=50&page=${_aiPage}&q=${encodeURIComponent(q)}`);
    const cnt = $("ai-review-count");
    if (cnt) cnt.textContent = res.total || 0;
    const list = $("ai-case-list");
    if (!list) return;
    _aiCache.clear();
    (res.cases || []).forEach(c => _aiCache.set(c.id, c));
    list.innerHTML = (res.cases || []).map(c => {
      const kind = KIND_LABELS[c.claim_kind] || c.claim_kind || "—";
      const prColor = PRIORITY_COLORS[c.priority] || "gray";
      const isActive = c.id === _selectedAiId;
      return `<div class="split-item${isActive ? " active" : ""}" data-id="${c.id}" onclick="selectSplitCase('ai',${c.id})">
        <div class="split-item-top">
          <span class="split-item-buyer">${esc(c.buyer_name || "—")}</span>
          <span class="badge badge-${prColor}" style="font-size:9px">${PRIORITY_LABELS[c.priority] || c.priority || ""}</span>
        </div>
        <div class="split-item-subject">${esc((c.subject || "—").slice(0, 60))}</div>
        <div class="split-item-meta">${badge(kind, "blue")} <span style="color:var(--text-muted)">${fmtDate(c.received_at)}</span></div>
      </div>`;
    }).join("");
    if (!res.cases?.length) {
      list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-muted)">Нет писем, обработанных AI</div>';
    }
    renderPagination("ai-pagination", res.total || 0, _aiPage, 50,
      function(p) { _aiPage = p; savePage("ai", p); loadAiReview(); });
  } catch (e) { console.warn("loadAiReview error:", e); }
}

async function runAiOnReviewBatch() {
  toast("Запускаю AI на необработанных...");
  const res = await api("/api/ai/run-batch", { method: "POST", body: JSON.stringify({ limit: 30 }) });
  toast(res.ok ? `AI обработал ${res.processed || 0}` : "Ошибка: " + (res.error || ""), res.ok ? "success" : "error");
  loadAiReview(); loadPipelineStatus();
}

async function confirmAiCase(id) {
  showConfirmModal("Подтвердить AI-результат?",
    `Кейс #${id} будет подтверждён и отправлен в 1С.`,
    async () => {
      const res = await api(`/api/cases/${id}/confirm`, { method: "POST" });
      toast(res.ok ? "Подтверждён" : "Ошибка: " + (res.error || res.detail || ""), res.ok ? "success" : "error");
      loadAiReview(); loadPipelineStatus();
    }
  );
}

/* ──────────────────────── НЕРАЗОБРАННЫЕ ──────────────────────── */

let _unprocessedPage = loadSavedPage("unprocessed");
let _offtopicPage = loadSavedPage("offtopic");
let _linksPage = loadSavedPage("links");

function renderLooseCaseCards(res, listId, paginationId, page, onPage, emptyText) {
  const list = $(listId);
  if (!list) return;
  if (!res.cases?.length) {
    list.innerHTML = `<div style="text-align:center;padding:40px;color:var(--text-muted)">${esc(emptyText)}</div>`;
    return;
  }
  list.innerHTML = res.cases.map(c => {
    const body = c.visible_text || c.body_text || c.snippet || "";
    const preview = body.slice(0, 300).replace(/\n+/g, " ").trim();
    const kind = KIND_LABELS[c.claim_kind] || c.claim_kind || c.event_type || "—";
    return `<div class="unprocessed-card">
      <div class="unprocessed-header">
        <div>
          <span class="split-item-buyer">${esc(c.buyer_name || c.from_addr || "Неизвестный клиент")}</span>
          <span style="font-size:11px;color:var(--text-muted);margin-left:6px">${esc(kind)}</span>
        </div>
        <span style="font-size:11px;color:var(--text-muted)">${fmtDate(c.received_at)}</span>
      </div>
      <div class="unprocessed-subject" style="cursor:pointer" title="Открыть письмо" onclick="openCaseDetail(${c.id})">${esc(c.subject || "—")}</div>
      ${c.has_att ? `<span style="font-size:11px">есть вложение</span>` : ""}
      <div class="unprocessed-preview" style="cursor:pointer" title="Открыть письмо" onclick="openCaseDetail(${c.id})">${esc(preview)}</div>
      <div class="unprocessed-actions">
        <button class="btn-sm" onclick="runAiOnUnprocessedCase(${c.id})">AI</button>
        <button class="btn-sm" onclick="openCaseDetail(${c.id})">Детали</button>
        <button class="btn-sm danger" onclick="closeUnprocessedCase(${c.id})">Закрыть</button>
      </div>
    </div>`;
  }).join("");
  renderPagination(paginationId, res.total || 0, page, 30, onPage);
}

async function loadOfftopic() {
  try {
    const q = $("offtopic-search")?.value || "";
    const res = await api(`/api/v2/cases/by-method?method=offtopic&limit=30&page=${_offtopicPage}&q=${encodeURIComponent(q)}`);
    const cnt = $("offtopic-count");
    if (cnt) cnt.textContent = res.total || 0;
    renderLooseCaseCards(res, "offtopic-list", "offtopic-pagination", _offtopicPage,
      function(p) { _offtopicPage = p; savePage("offtopic", p); loadOfftopic(); },
      "Писем не по теме нет");
  } catch (e) { console.warn("loadOfftopic error:", e); }
}

async function loadLinks() {
  try {
    const q = $("links-search")?.value || "";
    const res = await api(`/api/v2/cases/by-method?method=links&limit=30&page=${_linksPage}&q=${encodeURIComponent(q)}`);
    const cnt = $("links-count");
    if (cnt) cnt.textContent = res.total || 0;
    renderLooseCaseCards(res, "links-list", "links-pagination", _linksPage,
      function(p) { _linksPage = p; savePage("links", p); loadLinks(); },
      "Писем для связывания нет");
  } catch (e) { console.warn("loadLinks error:", e); }
}

async function loadUnprocessed() {
  try {
    const res = await api(`/api/v2/cases/by-method?method=unprocessed&limit=30&page=${_unprocessedPage}`);
    const cnt = $("unprocessed-count");
    if (cnt) cnt.textContent = res.total || 0;
    renderLooseCaseCards(res, "unprocessed-list", "unprocessed-pagination", _unprocessedPage,
      function(p) { _unprocessedPage = p; savePage("unprocessed", p); loadUnprocessed(); },
      "Неразобранных писем нет");
  } catch (e) { console.warn("loadUnprocessed error:", e); }
}

async function runAiOnUnprocessed() {
  toast("AI обрабатывает неразобранные...");
  const res = await api("/api/ai/run-batch", { method: "POST", body: JSON.stringify({ limit: 20, target: "unknown" }) });
  toast(res.ok ? `Обработано: ${res.processed || 0}` : "Ошибка: " + (res.error || ""), res.ok ? "success" : "error");
  loadUnprocessed(); loadPipelineStatus();
}

async function runAiOnUnprocessedCase(id) {
  toast(`AI анализирует кейс #${id}...`);
  const res = await api(`/api/cases/${id}/ai_apply`, { method: "POST" });
  toast(res.ok ? "AI применён" : "Ошибка: " + (res.error || ""), res.ok ? "success" : "error");
  loadUnprocessed();
}

async function closeUnprocessedCase(id) {
  await api(`/api/cases/${id}/close`, { method: "POST" });
  toast("Закрыт"); loadUnprocessed();
}

/* ──────────────────────── КЛИЕНТЫ ──────────────────────── */

let _allClients = [];

async function loadClients() {
  try {
    const res = await api("/api/v2/clients");
    const cnt = $("clients-count");
    if (cnt) cnt.textContent = res.count || 0;
    _allClients = res.items || [];
    renderClients(_allClients);
  } catch (e) { console.warn("loadClients error:", e); }
}

function filterClients(q) {
  const lq = (q || "").toLowerCase();
  renderClients(_allClients.filter(c =>
    c.name.toLowerCase().includes(lq) ||
    c.code.toLowerCase().includes(lq) ||
    (c.domains || []).some(d => d.includes(lq))
  ));
}

function renderClients(clients) {
  const grid = $("clients-grid");
  if (!grid) return;
  if (!clients.length) {
    grid.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text-muted)">Клиенты не найдены</div>';
    return;
  }
  grid.innerHTML = clients.map(c => {
    const s = c.stats || {};
    const total = s.total || 0;
    const ready = s.ready || 0;
    const review = s.review || 0;
    const done = s.done || 0;
    const aiApplied = s.ai_applied || 0;
    const patternPct = total > 0 ? Math.round(((total - review - aiApplied) / total) * 100) : 0;
    const statusClass = c.enabled ? "" : "client-disabled";
    return `<div class="client-card ${statusClass}">
      <div class="client-card-header">
        <span class="client-name">${esc(c.name)}</span>
        <span class="client-code">${esc(c.code)}</span>
        ${c.unknown ? `<span class="badge badge-amber" style="font-size:9px">Новый</span>` : ""}
        ${!c.enabled ? `<span class="badge badge-gray" style="font-size:9px">Откл.</span>` : ""}
      </div>
      ${c.domains?.length ? `<div class="client-domains">${c.domains.map(d => `<span class="domain-tag">${esc(d)}</span>`).join("")}</div>` : ""}
      ${c.folders?.length ? `<div class="client-domains">${c.folders.slice(0,3).map(d => `<span class="domain-tag">${esc(d)}</span>`).join("")}</div>` : ""}
      <div class="client-stats">
        <div class="client-stat"><span>Всего писем</span><b>${total}</b></div>
        <div class="client-stat"><span>Готово</span><b style="color:var(--green)">${ready}</b></div>
        <div class="client-stat"><span>На проверке</span><b style="color:var(--amber)">${review}</b></div>
        <div class="client-stat"><span>Выполнено</span><b>${done}</b></div>
        <div class="client-stat"><span>AI использован</span><b>${aiApplied}</b></div>
      </div>
      ${total > 0 ? `<div class="client-bar-wrap" title="Паттерны / AI / На проверке">
        <div class="client-bar" style="width:${patternPct}%;background:var(--green)" title="Паттерны ${patternPct}%"></div>
        <div class="client-bar" style="width:${Math.round(aiApplied/total*100)}%;background:var(--accent)" title="AI ${Math.round(aiApplied/total*100)}%"></div>
      </div>` : ""}
      <div class="client-deadline-row">
        <span>Срок возврата:</span>
        <input type="number" class="deadline-input" value="${c.return_deadline_days || 45}" min="1" max="365"
          onchange="saveClientDeadline('${escJs(c.code)}', this.value, this)">
        <span>дней</span>
      </div>
    </div>`;
  }).join("");
}

async function saveClientDeadline(code, days, inputEl) {
  const d = parseInt(days);
  if (!d || d < 1) return;
  const res = await api(`/api/v2/clients/${encodeURIComponent(code)}/deadline`, {
    method: "PATCH",
    body: JSON.stringify({ return_deadline_days: d }),
  });
  if (res.ok) {
    if (inputEl) { inputEl.style.borderColor = "var(--green)"; setTimeout(() => inputEl.style.borderColor = "", 1500); }
    toast(`Срок для ${code}: ${d} дней`, "success");
  } else {
    toast("Ошибка: " + (res.error || "Ошибка"), "error");
  }
}

/* ──────────────────────── 1С ──────────────────────── */

let _onecItems = [];
let _onecPage = 1;
const _onecCache = new Map(); // id -> item

const ONEC_PAGE_SIZE = 100;
async function loadOnec() {
  try {
    const status = $("onec-filter")?.value || "new";
    const res = await api(`/api/export/outbox?limit=${ONEC_PAGE_SIZE}&page=${_onecPage}&status=${status === "all" ? "" : status}`);
    _onecItems = res.items || res.outbox || [];
    _onecCache.clear();
    _onecItems.forEach(item => { const id = item.id || item.case_id; if (id) _onecCache.set(id, item); });
    $("onec-count").textContent = res.total || _onecItems.length;
    renderOnecSummary(res, status);
    renderOnecTable(_onecItems);
    renderPagination("onec-pagination", res.total || 0, res.page || _onecPage, res.page_size || ONEC_PAGE_SIZE,
                     (p) => { _onecPage = p; loadOnec(); });
  } catch (e) { console.warn("loadOnec error:", e); }
}

function renderOnecSummary(res, status) {
  const el = $("onec-summary");
  if (!el) return;
  const total = res.total || _onecItems.length || 0;
  const visible = _onecItems.length || 0;
  const label = status === "new" ? "новых JSON" : status === "error" ? "ошибок" : status === "sent" ? "отправленных" : "записей";
  el.innerHTML = `
    <span class="onec-summary-chip"><b>${total}</b> ${esc(label)}</span>
    <span class="onec-summary-chip">на экране ${visible}</span>
    <span class="onec-summary-note">Это локальная очередь JSON для 1С: проверка и доставка управляются кнопками справа.</span>
  `;
}

function filterOnecTable(q) {
  const lq = (q || "").toLowerCase();
  const filtered = _onecItems.filter(item => {
    const p = item.payload || {};
    const r = (p.return || p.items?.[0]) || {};
    const buyer = (p.buyer?.name || p.buyer?.code || "").toLowerCase();
    const part = (r.part_number || "").toLowerCase();
    const doc = (r.document_number || "").toLowerCase();
    return buyer.includes(lq) || part.includes(lq) || doc.includes(lq);
  });
  renderOnecTable(filtered);
}

function renderOnecTable(items) {
  const tbody = $("onec-tbody");
  if (!tbody) return;
  // Event delegation — кликаем по строке, не по onclick-атрибуту
  tbody.onclick = (e) => {
    const tr = e.target.closest("tr[data-onec-id]");
    if (!tr) return;
    const id = parseInt(tr.dataset.onecId);
    const item = _onecCache.get(id);
    if (item) showOnecJson(id, item);
  };
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="11" style="text-align:center;padding:20px;color:var(--text-muted)">Нет данных</td></tr>`;
    return;
  }
  tbody.innerHTML = items.map(item => {
    const p = item.payload || {};
    const r = (p.return || p.items?.[0]) || {};
    const doc = p.document || {};
    const buyer = p.buyer?.name || p.buyer?.code || "—";
    const statusColor = item.status === "sent" ? "green" : item.status === "error" ? "red" : "gray";
    const jsonStr = JSON.stringify(item, null, 2);

    // Field validation
    const vPart = r.part_number || doc.part_number;
    const vDocNum = r.document_number || doc.number;
    const vDocDate = r.document_date || doc.date;
    const vQty = r.quantity;
    const vPrice = r.price_total || r.price;
    const vName = r.product_name;
    const vBrand = r.brand;
    const vReason = r.claim_kind || r.reason;

    const fv = (val, isNum = false) => {
      if (!val && val !== 0) return `<span class="field-empty">—</span>`;
      const s = String(val);
      if (isNum) {
        const n = parseFloat(s.replace(/\s/g, "").replace(",", "."));
        if (isNaN(n)) return `<span class="field-error">${esc(s)}</span>`;
      }
      return `<span class="field-ok">${esc(s)}</span>`;
    };

    return `<tr data-onec-id="${item.id || item.case_id || 0}" style="cursor:pointer">
      <td>${item.status === "error" ? "!" : item.status === "sent" ? "✓" : "·"}</td>
      <td style="font-size:12px"><b>${esc(buyer)}</b></td>
      <td>${fv(vDocNum)}</td>
      <td style="font-size:11px">${fv(vDocDate)}</td>
      <td>${fv(vPart)}</td>
      <td>${fv(vQty, true)}</td>
      <td>${fv(vPrice, true)}</td>
      <td style="font-size:12px">${fv(vName)}</td>
      <td style="font-size:12px">${fv(vBrand)}</td>
      <td>${fv(vReason)}</td>
      <td>${badge(item.status || "?", statusColor)}</td>
    </tr>`;
  }).join("");
}

async function validateOnec() {
  const res = await api("/api/outbox/validate");
  const el = $("onec-validate-result");
  if (!el) return;
  if (res.issues_count === 0) {
    el.style.display = "block";
    el.style.background = "var(--green-light)";
    el.innerHTML = `Всё проверено: ${res.total_checked} записей — ошибок нет`;
  } else {
    el.style.display = "block";
    el.style.background = "var(--red-light)";
    el.innerHTML = `Найдено проблем: ${res.issues_count} из ${res.total_checked}<br>` +
      res.issues.slice(0, 5).map(i =>
        `<div>· Outbox #${i.outbox_id}: ${i.issues.join(", ")}</div>`
      ).join("");
  }
}

async function deliverOnec() {
  toast("Доставка в 1С...");
  const res = await api("/api/outbox/deliver", { method: "POST" });
  toast(res.ok ? `Доставлено: ${res.delivered || 0}` : "Ошибка: " + (res.error || ""), res.ok ? "success" : "error");
  loadOnec(); loadPipelineStatus();
}

async function cleanupOnec() {
  showConfirmModal(
    "Удалить записи без ready_to_1c?",
    "Удалит из outbox записи, кейсы которых ещё не прошли обработку (state ≠ ready_to_1c).",
    async () => {
      const res = await api("/api/v2/outbox/cleanup-non-ready", { method: "POST" });
      toast(res.ok ? `${res.message}` : "Ошибка: " + (res.error || ""), res.ok ? "success" : "error");
      loadOnec(); loadPipelineStatus();
    }
  );
}

async function cleanupEmptyOnec() {
  showConfirmModal(
    "Удалить записи без обязательных полей?",
    "Удалит из 1С записи, у которых нет артикула и/или номера документа. Кейсы вернутся на доработку (AI-разбор).",
    async () => {
      const res = await api("/api/v2/outbox/cleanup-empty-fields", { method: "POST" });
      toast(res.ok ? `${res.message}` : "Ошибка: " + (res.error || ""), res.ok ? "success" : "error");
      loadOnec(); loadPipelineStatus();
    }
  );
}

function showOnecJson(id, itemObj) {
  const modal = $("outbox-json-modal");
  const title = $("outbox-json-title");
  const body = $("outbox-json-body");
  if (!modal) return;
  if (title) title.textContent = `JSON #${id}`;
  const pretty = JSON.stringify(itemObj, null, 2);
  if (body) {
    body.innerHTML = `<pre style="margin:0;white-space:pre-wrap;font-size:11px">${esc(pretty)}</pre>`;
    body.dataset.json = pretty;
  }
  modal.classList.remove("hidden");
}

function closeOutboxJsonModal() {
  const modal = $("outbox-json-modal");
  if (modal) modal.classList.add("hidden");
}

function copyOutboxJson() {
  const body = $("outbox-json-body");
  if (body && body.dataset.json) copyToClipboard(body.dataset.json);
}

/* ──────────────────────── ДЕТАЛЬНАЯ ПАНЕЛЬ ──────────────────────── */

async function openEmailDetail(id) {
  const data = await api(`/api/emails/${id}`);
  if (!data || data.error) return;
  showDetailPanel("Письмо #" + id, renderEmailDetail(data), [
    { label: "AI", action: () => runAiOnEmail(id) },
  ]);
}

async function openCaseDetail(id) {
  const data = await api(`/api/cases/${id}`);
  if (!data || data.error) return;
  showDetailPanel("Кейс #" + id, renderCaseDetail(data), buildCaseActions(data));
}

function renderEmailDetail(e) {
  const body = e.visible_text || e.body_text || e.snippet || "";
  const preview = body.length > 6000 ? body.slice(0, 6000) + "\n\n... (обрезано)" : body;
  return `
    <div class="detail-field"><label>От кого</label><div class="val">${esc(e.from_addr || "—")}</div></div>
    <div class="detail-field"><label>Тема</label><div class="val">${esc(e.subject || "—")}</div></div>
    <div class="detail-field"><label>Получено</label><div class="val">${fmtDate(e.received_at)}</div></div>
    <div class="detail-field"><label>Папка</label><div class="val">${esc(e.mailbox || "—")}</div></div>
    ${(e.attachments || []).length ? `<div class="detail-section">Вложения (${e.attachments.length})</div>
    ${e.attachments.map(a => {
      const ext = (a.filename || "").split(".").pop().toLowerCase();
      const icon = ["xlsx","xls","xlsm"].includes(ext) ? "📊" : ext === "pdf" ? "📄" : ext === "csv" ? "📑" : ["jpg","jpeg","png","gif","webp","bmp"].includes(ext) ? "🖼" : ext === "zip" ? "📦" : "📎";
      const canPreview = ["xlsx","xls","xlsm","csv","zip"].includes(ext);
      const canVision = ["jpg","jpeg","png","gif","pdf","webp"].includes(ext);
      const kb = a.size_bytes ? ` <span style="color:var(--text-muted)">(${Math.round(a.size_bytes/1024)} КБ)</span>` : "";
      const attId = a.id || "";
      return `<div style="font-size:12px;padding:3px 0;display:flex;align-items:center;gap:6px;flex-wrap:wrap">
        <span>${icon}</span><b>${esc(a.filename || "—")}</b>${kb}
        ${attId && canPreview ? `<button class="btn-sm" style="padding:1px 8px;font-size:11px" onclick="previewAttachment(${attId},'${escJs(a.filename || "")}')">Открыть</button>` : ""}
        ${attId && canVision ? `<button class="btn-sm" style="padding:1px 8px;font-size:11px;color:var(--accent)" onclick="visionAttachment(${attId},'${escJs(a.filename || "")}')">Vision AI</button>` : ""}
        ${attId ? `<a href="/api/attachments/${attId}/download" target="_blank" class="btn-sm" style="padding:1px 8px;font-size:11px;text-decoration:none">Скачать</a>` : ""}
      </div>`;
    }).join("")}
    <div id="att-preview-email-${e.id}" style="margin-top:6px"></div>` : ""}
    <div class="detail-section">Текст письма</div>
    <pre style="font-size:12px;line-height:1.7;max-height:45vh;overflow-y:auto;white-space:pre-wrap;background:var(--bg);padding:8px;border-radius:6px">${esc(preview)}</pre>
  `;
}

/* ── Карточки позиций (мультипозиция в Сверке) ── */
const POS_FIELDS = [
  ["part_number", "Артикул", "напр. HNQ2495GQ"],
  ["brand", "Бренд", "напр. Krauf"],
  ["product_name", "Наименование", "напр. Рулевая тяга"],
  ["quantity", "Кол-во", "1"],
  ["price", "Цена", ""],
];
function posCardHtml(item, idx) {
  const it = item || {};
  const inputs = POS_FIELDS.map(([key, label, ph]) =>
    `<div class="pos-field">
       <label>${label}</label>
       <input class="pos-input" data-pos-field="${key}" value="${esc(it[key] != null ? String(it[key]) : "")}" placeholder="${esc(ph)}">
     </div>`
  ).join("");
  return `<div class="pos-card" data-pos>
    <div class="pos-card-head">
      <span class="pos-card-num">Позиция ${idx + 1}</span>
      <button class="pos-del" title="Удалить позицию" onclick="this.closest('.pos-card').remove(); renumberPositions(this)">×</button>
    </div>
    <div class="pos-grid">${inputs}</div>
  </div>`;
}
function positionsBlockHtml(items) {
  const list = (items && items.length) ? items : [{}];
  return `<div class="pos-list" id="pos-list">${list.map((it, i) => posCardHtml(it, i)).join("")}</div>
    <button class="btn-sm" style="margin-top:4px" onclick="addPositionCard(this)">+ позиция</button>`;
}
function renumberPositions(node) {
  const list = node.closest(".pos-list, .split-half-body, .detail-body, body") || document;
  (list.querySelectorAll(".pos-card") || []).forEach((card, i) => {
    const n = card.querySelector(".pos-card-num");
    if (n) n.textContent = "Позиция " + (i + 1);
  });
}
function addPositionCard(btn) {
  const list = btn.parentElement.querySelector("#pos-list") || btn.closest(".split-half-body, .detail-body")?.querySelector(".pos-list");
  if (!list) return;
  const idx = list.querySelectorAll(".pos-card").length;
  list.insertAdjacentHTML("beforeend", posCardHtml({}, idx));
}
function collectPositions(root) {
  const cards = (root || document).querySelectorAll(".pos-card");
  const items = [];
  cards.forEach(card => {
    const it = {};
    card.querySelectorAll(".pos-input").forEach(inp => {
      const v = (inp.value || "").trim();
      if (v) it[inp.dataset.posField] = v;
    });
    if (it.part_number || it.product_name) items.push(it);
  });
  return items;
}

function renderCaseDetail(c) {
  const f = c.fields || {};
  const evidenceGate = c.evidence_gate || c.payload?.evidence_gate || {};
  const buyerEvidenceMeta = evidenceGate.field_audit?.buyer_code?.evidence_meta || {};
  const buyerMismatchHtml = (buyerEvidenceMeta.mismatch_classifications || []).map((item) =>
    `<div style="color:${item.severity === "error" ? "var(--red)" : "var(--amber)"};margin-top:3px">
      Контрагент подтверждён по профилю. В тексте найдено: ${esc(item.detected_name || item.detected_code || "—")}.
      Класс расхождения: ${esc(item.mismatch_class || "unknown_mismatch")}
    </div>`
  ).join("");
  const posItems = (c.export && Array.isArray(c.export.items) && c.export.items.length)
    ? c.export.items
    : [{ part_number: f.part_number, brand: f.brand, product_name: f.product_name, quantity: f.quantity, price: f.price }];
  const body = c.visible_text || c.body_text || c.snippet || "";
  const bodyPreview = body.length > 8000 ? body.slice(0, 8000) + "\n\n... (обрезано)" : body;
  const issues = (c.quality || []).map(q =>
    `<div style="font-size:12px;color:${q.level === "error" ? "var(--red)" : "var(--amber)"}">${esc(q.message || q.code || "")}</div>`
  ).join("");
  const evidenceBlock = Object.keys(evidenceGate).length
    ? `<div class="detail-section">Evidence</div>
       <div style="font-size:12px;color:${evidenceGate.passed ? "var(--green)" : "var(--amber)"}">
         <b>${evidenceGate.passed ? "Пройден" : "Заблокирован до сверки"}</b>
         <div style="color:var(--text-muted);margin-top:3px">${Object.entries(evidenceGate.field_statuses || {}).map(([key, value]) => `${esc(key)}: ${esc(value)}`).join(" · ")}</div>
         ${(evidenceGate.repairs || []).length ? `<div style="color:var(--green);margin-top:3px">Восстановлено: ${(evidenceGate.repairs || []).map((item) => `${esc(item.field)} (${esc(item.repair_method)})`).join(", ")}</div>` : ""}
         ${(evidenceGate.blocking_errors || []).length ? `<div style="color:var(--red);margin-top:3px">${(evidenceGate.blocking_errors || []).map(esc).join(", ")}</div>` : ""}
         ${(evidenceGate.blocking_warnings || []).length ? `<div style="color:var(--amber);margin-top:3px">${(evidenceGate.blocking_warnings || []).map(esc).join(", ")}</div>` : ""}
         ${(evidenceGate.non_blocking_warnings || []).length ? `<div style="color:var(--amber);margin-top:3px">${(evidenceGate.non_blocking_warnings || []).map(esc).join(", ")}</div>` : ""}
         ${buyerMismatchHtml}
       </div>`
    : "";
  const missingSet = new Set(c.missing || []);
  const fieldRow = (key, label, placeholder = "") => {
    const val = f[key] || "";
    const isMissing = missingSet.has(key);
    return `<div class="detail-field train-field" style="${isMissing ? "background:rgba(255,100,0,.06);border-radius:4px;padding:2px 4px" : ""}">
      <label style="${isMissing ? "color:var(--amber)" : ""}">${label}${isMissing ? "" : ""}</label>
      <input class="train-input" data-field="${key}" value="${esc(val)}" placeholder="${placeholder || label}"
        style="width:100%;border:1px solid var(--border);border-radius:4px;padding:3px 6px;font-size:13px;background:var(--bg)">
    </div>`;
  };
  return `
    <div class="detail-field"><label>Клиент</label><div class="val">${esc(c.buyer_name || "—")} <span style="color:var(--text-muted)">(${esc(c.buyer_code || "?")})</span></div></div>
    <div class="detail-field"><label>Тип претензии</label><div class="val">${badge(KIND_LABELS[c.claim_kind] || "—", "blue")}</div></div>
    <div class="detail-field"><label>Статус</label><div class="val">${badge(STATE_LABELS[c.state] || c.state || "—", PRIORITY_COLORS[c.priority] || "gray")}</div></div>
    <div class="detail-field"><label>Уверенность</label><div class="val">${Math.round((c.confidence || 0) * 100)}%</div></div>
    <div class="detail-section" style="display:flex;align-items:center;gap:8px">
      <span>Исправить поля</span>
      <span style="font-size:11px;color:var(--text-muted)">(изменения обучат AI)</span>
    </div>
    ${fieldRow("document_number", "№ Документа", "напр. 82676")}
    ${fieldRow("document_date", "Дата", "ДД.ММ.ГГГГ")}
    ${fieldRow("claim_number", "№ Претензии", "")}
    <div class="detail-section" style="display:flex;align-items:center;gap:8px">
      <span>Позиции${posItems.length > 1 ? ` (${posItems.length})` : ""}</span>
      <span style="font-size:11px;color:var(--text-muted)">артикул · бренд · наименование · кол-во · цена</span>
    </div>
    ${positionsBlockHtml(posItems)}
    <div style="display:flex;gap:6px;margin-top:8px">
      <button class="btn-sm" onclick="saveTrainCase(${c.id}, false)" style="flex:1">Сохранить</button>
      <button class="btn-sm success" onclick="saveTrainCase(${c.id}, true)" style="flex:2" title="Сохранить поля и попросить AI сгенерировать паттерны">Сохранить и обучить AI</button>
    </div>
    <div id="train-result-${c.id}" style="margin-top:6px;font-size:12px"></div>
    ${evidenceBlock}
    ${issues ? `<div class="detail-section">Проблемы</div>${issues}` : ""}
    ${(() => {
      const urls = ((body || "") + " " + (c.subject || "")).match(/https?:\/\/[^\s<>"')\]]+/g) || [];
      const uniq = urls.map(u => u.replace(/[.,;)\]}"']+$/, "")).filter((u, i, a) => a.indexOf(u) === i).slice(0, 12);
      if (!uniq.length) return "";
      return `<div class="detail-section" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span>Ссылки (${uniq.length})</span>
          <button class="btn-sm" onclick="readCaseLinks(${c.id}, this)" title="Загрузить страницы рекламации: поля, фото дефекта, документы">🔗 Прочитать ссылки</button>
        </div>
        ${uniq.map(u => `<div style="font-size:11px;padding:1px 0"><a href="${esc(u)}" target="_blank" rel="noopener" style="color:var(--accent);word-break:break-all">${esc(u.length > 72 ? u.slice(0, 72) + "…" : u)}</a></div>`).join("")}
        <div id="links-result-${c.id}" style="font-size:11px;margin-top:5px"></div>`;
    })()}
    <div class="detail-section">Письмо</div>
    <div class="detail-field"><label>От кого</label><div class="val">${esc(c.from_addr || "—")}</div></div>
    <div class="detail-field"><label>Тема</label><div class="val">${esc(c.subject || "—")}</div></div>
    <div class="detail-field"><label>Получено</label><div class="val">${fmtDate(c.received_at)}</div></div>
    <pre style="font-size:12px;line-height:1.7;max-height:45vh;overflow-y:auto;white-space:pre-wrap;background:var(--bg);padding:8px;border-radius:6px">${esc(bodyPreview || "Текст письма не сохранен")}</pre>
  `;
}

async function saveTrainCase(caseId, aiGenerate) {
  const resultEl = $(`train-result-${caseId}`);
  const root = resultEl ? (resultEl.closest(".detail-panel, .split-half, .split-detail-col, body") || document) : document;
  const inputs = root.querySelectorAll(`.train-input`);
  const fields = {};
  inputs.forEach(inp => {
    const val = (inp.value || "").trim();
    if (val) fields[inp.dataset.field] = val;
  });
  // Позиции (карточки мультипозиции). Если блок есть — собираем и шлём items.
  const hasPositions = root.querySelector(".pos-card");
  const items = hasPositions ? collectPositions(root) : null;
  if (resultEl) resultEl.innerHTML = `<span style="color:var(--text-muted)">Сохраняем${aiGenerate ? " и обучаем AI" : ""}…</span>`;
  let res;
  try {
    res = await api(`/api/cases/${caseId}/train`, {
      method: "POST",
      body: JSON.stringify({ fields, items, ai_generate_patterns: aiGenerate }),
    });
  } catch (e) {
    res = { ok: false, error: String(e) };
  }
  if (!res || res.error || res.ok === false) {
    const msg = res?.error || res?.detail || "Ошибка";
    if (resultEl) resultEl.innerHTML = `<span style="color:var(--red)">${esc(msg)}</span>`;
    toast("Ошибка сохранения: " + msg, "error");
    return;
  }

  // Формируем детальный отчёт об обучении
  const lines = [];
  // ── Превью намерения: куда система отправит письмо и что блокирует ──
  const MISS_LBL = {
    part_number: "артикул", document_number: "№ документа", document_date: "дата документа",
    strong_key: "надёжный ключ (№+артикул)", claim_kind: "тип возврата", buyer: "клиент",
    client_request_number: "№ заявки клиента", quantity: "количество",
    valid_quantity: "корректное количество", event_type: "тип письма",
    photo_evidence: "фото", service_document: "акт/заказ-наряд (брак)",
  };
  if (res.state === "ready_to_1c") {
    lines.push(`<b style="color:var(--green)">→ Готов к 1С</b> · появится в Сверке, отправка — кнопкой или автопилотом`);
  } else {
    const miss = (res.missing || []).map(m => MISS_LBL[m] || m).join(", ");
    lines.push(`<b style="color:var(--amber)">→ Останется в разборе</b>${miss ? ` · не хватает: <b>${esc(miss)}</b>` : ""}`);
  }

  if (res.rule_patterns_generated > 0)
    lines.push(`Паттернов из текста: <b>${res.rule_patterns_generated}</b>`);

  const ai = res.ai || {};
  if (ai.ok) {
    lines.push(`AI сгенерировал паттернов: <b>${ai.patterns_generated || 0}</b>`);
    if (ai.usage) {
      const chars = (ai.usage.prompt_chars || 0) + (ai.usage.response_chars || 0);
      lines.push(`Токенов ≈ <b>${Math.round(chars/4)}</b> (~${Math.round(chars/4*0.0003*100)/100}₽)`);
    }
    if (ai.analysis) lines.push(`<i style="color:var(--text-muted)">${esc(ai.analysis.slice(0,150))}</i>`);
    if (ai.found_in_text) {
      const found = Object.entries(ai.found_in_text).map(([k,v]) => `${k}: "${esc(String(v||"").slice(0,30))}"`).join(", ");
      if (found) lines.push(`Найдено в письме: ${found}`);
    }
  } else if (ai.skipped) {
    lines.push(`AI: ${esc(ai.skipped)}`);
  } else if (!ai.ok && ai.error) {
    lines.push(`AI ошибка: ${esc(ai.error)}`);
  }

  if (res.auto_promoted > 0)
    lines.push(`Паттерны записаны в конфиг клиента: <b>${res.auto_promoted}</b>`);
  if (res.promote && res.promote.ok === false)
    lines.push(`YAML не обновился: ${esc(res.promote.error || "ошибка записи")}`);
  if (res.similar_rechecked > 0)
    lines.push(`Перепроверено писем клиента: <b>${res.similar_rechecked}</b>, из них стало готово к 1С: <b>${res.similar_reapplied || 0}</b>`);

  if (resultEl) resultEl.innerHTML = `<div style="font-size:12px;line-height:1.7">${lines.join("<br>")}</div>`;
  toast(res.state === "ready_to_1c" ? "Готово к 1С!" : "Сохранено и обучено", "success");
  loadEmails();
  loadReview();
}

async function promotePatterns(caseId) {
  const res = await api(`/api/cases/${caseId}/promote-patterns?min_seen=1`, { method: "POST" });
  if (res.ok) {
    toast(`Промоутировано ${res.promoted} паттернов в конфиг`, "success");
  } else {
    toast(`Ошибка: ${res.error || "Ошибка"}`, "error");
  }
}

function buildCaseActions(c) {
  const actions = [];
  if (c.state === "needs_review" || c.state === "needs_link") {
    actions.push({ label: "AI", action: () => runAiOnCase(c.id) });
  }
  if (c.state === "ready_to_1c") {
    actions.push({ label: "В 1С", action: () => exportCase(c.id) });
  }
  return actions;
}

function showDetailPanel(title, body, actions = []) {
  const titleEl = $("detail-title");
  const bodyEl = $("detail-body");
  const actDiv = $("detail-actions");
  const panel = $("detail-panel");
  const overlay = $("detail-overlay");
  if (titleEl) titleEl.textContent = title;
  if (bodyEl) bodyEl.innerHTML = body;
  if (actDiv) {
    actDiv.innerHTML = "";
    actions.forEach(a => {
      const btn = document.createElement("button");
      btn.className = "btn-sm";
      btn.textContent = a.label;
      btn.onclick = a.action;
      actDiv.appendChild(btn);
    });
  }
  if (panel) panel.classList.remove("hidden");
  if (overlay) overlay.classList.remove("hidden");
}

function closeDetailPanel() {
  $("detail-panel")?.classList.add("hidden");
  $("detail-overlay")?.classList.add("hidden");
}

async function confirmCase(id, caseData) {
  showConfirmModal("Подтвердить кейс?",
    `Кейс #${id} будет подтверждён.\nКлиент: ${caseData.buyer_name || "?"}\nАртикул: ${(caseData.fields || {}).part_number || "—"}`,
    async () => {
      const res = await api(`/api/cases/${id}/confirm`, { method: "POST" });
      toast(res.ok ? "Подтверждён" : "Ошибка: " + (res.error || ""), res.ok ? "success" : "error");
      loadAiReview();
    }
  );
}

function showConfirmModal(title, body, onOk) {
  const modal = $("confirm-modal");
  const titleEl = $("confirm-title");
  const bodyEl = $("confirm-body");
  if (!modal || !titleEl || !bodyEl) return;
  titleEl.textContent = title;
  bodyEl.textContent = body;
  modal.classList.remove("hidden");
  const okBtn = $("confirm-ok");
  const cancelBtn = $("confirm-cancel");
  if (okBtn) okBtn.onclick = () => { modal.classList.add("hidden"); onOk(); };
  if (cancelBtn) cancelBtn.onclick = () => modal.classList.add("hidden");
}

async function runAiOnCase(id) {
  toast("AI анализирует кейс...");
  const res = await api(`/api/cases/${id}/ai_apply`, { method: "POST" });
  toast(res.ok ? "Готово" : "Ошибка: " + (res.error || ""), res.ok ? "success" : "error");
  openCaseDetail(id);
}

async function runAiOnEmail(id) {
  toast("AI анализирует письмо...");
  const res = await api(`/api/emails/${id}/ai`, { method: "POST" });
  toast(res.ok ? "Готово" : "Ошибка: " + (res.error || ""), res.ok ? "success" : "error");
  openEmailDetail(id);
}

async function exportCase(id) {
  const res = await api(`/api/cases/${id}/export`, { method: "POST" });
  toast(res.ok ? "Готово в 1С" : "Ошибка: " + (res.error || ""), res.ok ? "success" : "error");
}

/* ──────────────────────── НАСТРОЙКИ ──────────────────────── */

let _checkedFolders = new Set();

async function loadSettings() {
  try {
    const res = await api("/api/settings");
    if (res.error) return;
    const s = res.settings || {};
    setVal("s-imap-host", s.imap_host || "");
    setVal("s-imap-port", s.imap_port || 993);
    setVal("s-imap-user", s.imap_username || "");
    setVal("s-imap-limit", s.imap_limit || 200);
    setVal("s-imap-total-limit", s.imap_total_limit || 2000);
    setVal("s-imap-batch-size", s.imap_batch_size || 20);
    setVal("s-imap-max-raw-mb", s.imap_max_raw_email_mb || 25);
    setVal("s-import-max-attachment-mb", s.import_max_attachment_mb || 10);
    setChecked("s-import-download-attachments", s.import_download_attachments !== false);
    setChecked("s-imap-date-from-en", !!s.imap_date_from_enabled);
    setVal("s-imap-date-from", s.imap_date_from || "");
    setChecked("s-imap-date-to-en", !!s.imap_date_to_enabled);
    setVal("s-imap-date-to", s.imap_date_to || "");

    const passEl = $("s-imap-pass");
    if (passEl) {
      passEl.value = "";
      passEl.placeholder = s.imap_password === "__configured__" ? "сохранён" : "пароль приложения";
    }
    const imapStatus = $("s-imap-pass-status");
    if (imapStatus) {
      imapStatus.textContent = s.imap_password === "__configured__" ? "✓" : "✗";
      imapStatus.className = "secret-status " + (s.imap_password === "__configured__" ? "saved" : "missing");
    }

    // Инициализируем папки из сохранённых настроек
    const savedFolders = (s.imap_folders || s.imap_folder || "").split(",").map(f => f.trim()).filter(Boolean);
    _checkedFolders = new Set(savedFolders);

    const mode = s.processing_mode || "manual";
    setVal("s-processing-mode", mode);
    updateProcessingModeUI(mode);
    setChecked("s-auto-import", !!s.auto_import_enabled);
    setChecked("s-auto-process", !!s.auto_process_enabled);
    setVal("s-confidence-threshold", s.confidence_threshold || 0.85);

    setVal("s-1c-mode", s.one_c_export_mode || "file");
    setVal("s-1c-dir", s.one_c_file_dir || "");
    setVal("s-1c-url", s.one_c_http_url || "");
    setChecked("s-auto-deliver", !!s.auto_deliver_outbox);
    // Поля для 1С (тумблеры v2) — по умолчанию ВКЛ.
    setChecked("s-1c-inc-price", s.one_c_include_price !== false);
    setChecked("s-1c-inc-comment", s.one_c_include_comment !== false);
    setChecked("s-1c-inc-flags", s.one_c_include_defect_flags !== false);
    setChecked("s-1c-inc-status", s.one_c_include_status !== false);
    setChecked("s-1c-inc-text", s.one_c_include_text !== false);
    setChecked("s-1c-inc-attachments", s.one_c_include_attachments !== false);
    setChecked("s-1c-inc-source", s.one_c_include_source !== false);

    const provider = s.ai_provider || "routerai";
    setVal("ai-provider", provider);
    setChecked("ai-enabled", !!s.enable_ai);
    switchAiProvider(provider);

    const keyEl = $("ai-api-key");
    if (keyEl) {
      keyEl.value = "";
      keyEl.placeholder = s.ai_api_key === "__configured__" ? "сохранён" : "API key";
    }
    setSecretStatus("ai-api-key-status", s.ai_api_key === "__configured__");
    setVal("ai-model", s.ai_model || "");
    setVal("ai-price-rules", s.ai_price_rules_json || "");
    renderAiPriceRows(parseAiPriceRules(s.ai_price_rules_json || ""));

    const statusEl = $("settings-status");
    if (statusEl) statusEl.textContent = s.imap_username ? "Почта настроена: " + s.imap_username : "Почта не настроена";
    loadMailHealth();
    loadTrafficStats();

    // Vision AI
    setChecked("s-vision-enabled", !!s.ai_vision_enabled);
    setVal("s-vision-provider", s.ai_vision_provider || "routerai");
    setVal("s-vision-model", s.ai_vision_model || "qwen/qwen2.5-vl-7b-instruct");
    const vkEl = $("s-vision-key");
    if (vkEl) { vkEl.value = ""; vkEl.placeholder = s.ai_vision_api_key === "__configured__" ? "сохранён" : "отдельный ключ (если нужен)"; }
    setSecretStatus("s-vision-key-status", s.ai_vision_api_key === "__configured__");

    // Telegram
    const tgToken = $("s-tg-token");
    if (tgToken) { tgToken.value = ""; tgToken.placeholder = s.tg_bot_token === "__configured__" ? "токен сохранён" : "123456789:ABC..."; }
    const tgStatus = $("s-tg-token-status");
    if (tgStatus) { tgStatus.textContent = s.tg_bot_token === "__configured__" ? "✓" : "✗"; tgStatus.className = "secret-status " + (s.tg_bot_token === "__configured__" ? "saved" : "missing"); }
    setVal("s-tg-chats", s.tg_chat_ids || "");
    setChecked("s-tg-whitelist", s.tg_whitelist_enabled !== false);
    setChecked("s-tg-cycle",     s.tg_notify_on_cycle !== false);
    setChecked("s-tg-unresolved",s.tg_notify_unresolved !== false);
    setChecked("s-tg-errors",    s.tg_notify_errors !== false);
    setChecked("s-tg-ready",     !!s.tg_notify_ready);
    setChecked("s-tg-reasons",   s.tg_report_include_reasons !== false);
    setVal("s-tg-report-interval", s.tg_report_interval_minutes || 60);
    setVal("s-tg-unresolved-min", s.tg_unresolved_min || 1);

    // Отобразить выбранные папки
    if (savedFolders.length) {
      renderFolderList(savedFolders.map(f => ({ raw: f, display: f, selected: true })));
    }
  } catch (e) { console.warn("loadSettings error:", e); }
}

function setVal(id, val) { const el = $(id); if (el) el.value = String(val ?? ""); }
function setChecked(id, val) { const el = $(id); if (el) el.checked = !!val; }

async function purgeOutsidePeriod() {
  try { await saveSettings(); } catch (e) {}   // период должен быть сохранён до расчёта
  let dry;
  try { dry = await api("/api/import/purge-outside-period", { method: "POST" }); }
  catch (e) { toast("Ошибка: " + e, "error"); return; }
  if (!dry || !dry.ok) { toast((dry && dry.error) || "Период не задан", "error"); return; }
  const n = dry.outside_period || 0, total = dry.total || 0, p = dry.period || {};
  if (!n) { toast(`Вне периода писем нет (всего ${total})`, "info"); return; }
  if (!confirm(`Удалить ${n} писем вне периода [${p.from || "…"} … ${p.to || "…"}] из ${total}? Останется ${total - n}. Необратимо.`)) return;
  let res;
  try { res = await api("/api/import/purge-outside-period?confirm=PURGE&dry_run=false", { method: "POST" }); }
  catch (e) { toast("Ошибка: " + e, "error"); return; }
  if (res && res.ok) { toast(`Удалено ${res.deleted}, осталось ${res.remaining}`, "success"); if (window.loadSystemStatus) loadSystemStatus(); }
  else toast((res && res.error) || "Ошибка удаления", "error");
}
function setSecretStatus(elId, configured) {
  const el = $(elId);
  if (!el) return;
  el.textContent = configured ? "✓" : "✗";
  el.className = "secret-status " + (configured ? "saved" : "missing");
}

function renderFolderList(folders) {
  const container = $("folders-list-container");
  if (!container) return;
  if (!folders.length) {
    container.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:8px">Нет папок. Нажмите "Загрузить папки".</div>';
    return;
  }
  container.innerHTML = folders.map(f => {
    const raw = f.raw || f.name || f;
    const display = f.display || f.display_name || raw;
    const checked = _checkedFolders.has(raw);
    return `<label class="folder-item">
      <input type="checkbox" ${checked ? "checked" : ""} onchange="toggleFolder('${raw.replace(/'/g, "\\'")}', this.checked)">
      <span class="folder-name">${esc(display)}</span>
      ${f.count != null ? `<span class="folder-count">${f.count}</span>` : ""}
    </label>`;
  }).join("");
}

function toggleFolder(raw, checked) {
  if (checked) _checkedFolders.add(raw);
  else _checkedFolders.delete(raw);
}

async function loadServerCounts() {
  const list = $("server-counts-list");
  const tot = $("server-counts-total");
  if (list) list.innerHTML = '<div style="padding:8px;color:var(--text-muted)">Опрашиваю сервер...</div>';
  if (tot) tot.textContent = "";
  const res = await api("/api/import/server-counts");
  if (!res.ok) {
    if (list) list.innerHTML = `<div style="padding:8px;color:var(--red)">${esc(res.error || "Ошибка")}</div>`;
    return;
  }
  if (tot) {
    const gap = res.total_gap || 0;
    tot.innerHTML = `Сервер <b>${res.total_server}</b> / База <b>${res.total_db}</b> · ` +
      (gap > 0 ? `<span style="color:var(--red)">дыра ${gap}</span>` : `<span style="color:var(--green)">всё на месте ✓</span>`);
  }
  let rows = (res.folders || []).map(f => {
    const g = f.gap;
    const color = g === null ? "var(--text-muted)" : g > 0 ? "var(--red)" : "var(--green)";
    const gtxt = g === null ? "ошибка" : g > 0 ? `−${g}` : "✓";
    return `<tr><td style="padding:3px 8px">${esc(f.name)}</td>` +
      `<td style="padding:3px 8px;text-align:right">${f.server < 0 ? "—" : f.server}</td>` +
      `<td style="padding:3px 8px;text-align:right">${f.db}</td>` +
      `<td style="padding:3px 8px;text-align:right;color:${color};font-weight:600">${gtxt}</td></tr>`;
  }).join("");
  if (list) list.innerHTML =
    `<table style="width:100%;font-size:12px;border-collapse:collapse">` +
    `<tr style="color:var(--text-muted)"><td style="padding:3px 8px">папка</td>` +
    `<td style="padding:3px 8px;text-align:right">сервер</td><td style="padding:3px 8px;text-align:right">база</td>` +
    `<td style="padding:3px 8px;text-align:right">дыра</td></tr>${rows}</table>`;
}

async function refreshFolders() {
  const container = $("folders-list-container");
  if (container) container.innerHTML = '<div style="padding:8px;color:var(--text-muted)">Загрузка...</div>';
  const res = await api("/api/imap/folders");
  if (!res.ok && !res.folders?.length && !res.items?.length) {
    const err = res.error || "Ошибка подключения";
    const testRes = $("imap-test-result");
    if (testRes) { testRes.style.display = "block"; testRes.textContent = err; }
    return;
  }
  const items = (res.items || []).map(f => ({
    raw: f.raw_name || f.name || String(f),
    display: f.display_name || f.name || String(f),
    count: f.count ?? f.messages ?? null,
  }));
  // Сохраняем текущие отмеченные
  const selected = res.selected_folders || [];
  if (selected.length && _checkedFolders.size === 0) {
    _checkedFolders = new Set(selected);
  }
  renderFolderList(items);
  toast(`Найдено папок: ${items.length}`, "success");
}

function saveImapFolders() {
  const folders = [..._checkedFolders].filter(Boolean);
  if (!folders.length) { toast("Выберите хотя бы одну папку", "error"); return; }
  api("/api/setup/folders/save", {
    method: "POST",
    body: JSON.stringify({ folders }),
  }).then(res => {
    if (res.ok) toast(`Папки сохранены: ${folders.length}`, "success");
    else toast("Ошибка: " + (res.error || res.detail || ""), "error");
  });
}

async function testImapConnection() {
  const res = $("imap-test-result");
  if (res) { res.style.display = "block"; res.textContent = "Проверяю..."; }
  const result = await api("/api/v2/mail/test", { method: "POST" });
  if (res) {
    res.textContent = result.ok
      ? `Подключено к ${result.host || ""}\nПапок: ${result.folders_found || 0}\nВыбрано: ${(result.selected_folders || []).join(", ")}`
      : `Ошибка: ${result.error || "Ошибка"}`;
  }
  if (result.ok && result.folders) {
    renderFolderList(result.folders.map(f => ({ raw: f, display: f })));
  }
}

async function clearSecret(key) {
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({ values: { [key]: "" } }),
  });
  toast("Очищено");
  loadSettings();
}

async function saveSettings() {
  const priceRules = serializeAiPriceRules();
  const values = {
    IMAP_HOST: $("s-imap-host")?.value || "",
    IMAP_PORT: parseInt($("s-imap-port")?.value || "993"),
    IMAP_USERNAME: $("s-imap-user")?.value || "",
    IMAP_LIMIT: parseInt($("s-imap-limit")?.value || "200"),
    IMAP_TOTAL_LIMIT: parseInt($("s-imap-total-limit")?.value || "2000"),
    IMAP_BATCH_SIZE: parseInt($("s-imap-batch-size")?.value || "20"),
    IMAP_MAX_RAW_EMAIL_MB: parseInt($("s-imap-max-raw-mb")?.value || "25"),
    IMPORT_MAX_ATTACHMENT_MB: parseInt($("s-import-max-attachment-mb")?.value || "10"),
    IMPORT_DOWNLOAD_ATTACHMENTS: $("s-import-download-attachments")?.checked !== false,
    IMAP_DATE_FROM_ENABLED: !!$("s-imap-date-from-en")?.checked,
    IMAP_DATE_FROM: $("s-imap-date-from")?.value || "",
    IMAP_DATE_TO_ENABLED: !!$("s-imap-date-to-en")?.checked,
    IMAP_DATE_TO: $("s-imap-date-to")?.value || "",
    ENABLE_AI: $("ai-enabled")?.checked || false,
    AI_PROVIDER: $("ai-provider")?.value || "routerai",
    AI_MODEL: $("ai-model")?.value || "",
    AI_PRICE_RULES_JSON: priceRules,
    ONE_C_EXPORT_MODE: $("s-1c-mode")?.value || "file",
    ONE_C_FILE_DIR: $("s-1c-dir")?.value || "",
    ONE_C_HTTP_URL: $("s-1c-url")?.value || "",
    AUTO_DELIVER_OUTBOX: $("s-auto-deliver")?.checked || false,
    ONE_C_INCLUDE_PRICE: $("s-1c-inc-price")?.checked !== false,
    ONE_C_INCLUDE_COMMENT: $("s-1c-inc-comment")?.checked !== false,
    ONE_C_INCLUDE_DEFECT_FLAGS: $("s-1c-inc-flags")?.checked !== false,
    ONE_C_INCLUDE_STATUS: $("s-1c-inc-status")?.checked !== false,
    ONE_C_INCLUDE_TEXT: $("s-1c-inc-text")?.checked !== false,
    ONE_C_INCLUDE_ATTACHMENTS: $("s-1c-inc-attachments")?.checked !== false,
    ONE_C_INCLUDE_SOURCE: $("s-1c-inc-source")?.checked !== false,
    PROCESSING_MODE: $("s-processing-mode")?.value || "manual",
    CONFIDENCE_THRESHOLD: parseFloat($("s-confidence-threshold")?.value || "0.85"),
  };

  const ipassEl = $("s-imap-pass");
  if (ipassEl?.value) values["IMAP_PASSWORD"] = ipassEl.value;
  const akeyEl = $("ai-api-key");
  if (akeyEl?.value) values["AI_API_KEY"] = akeyEl.value;

  // AI provider-specific fields
  const provider = values.AI_PROVIDER;
  if (provider === "gigachat") {
    const gcKey = $("s-gigachat-key");
    if (gcKey?.value) values["GIGACHAT_AUTH_KEY"] = gcKey.value;
  } else if (provider === "openai_compatible") {
    const baseUrl = $("s-ai-base-url");
    if (baseUrl?.value) values["AI_BASE_URL"] = baseUrl.value;
  }

  // Папки
  if (_checkedFolders.size > 0) {
    values["IMAP_FOLDERS"] = [..._checkedFolders].join(",");
  }

  // Vision AI
  values["AI_VISION_ENABLED"]  = $("s-vision-enabled")?.checked || false;
  values["AI_VISION_PROVIDER"] = $("s-vision-provider")?.value || "routerai";
  values["AI_VISION_MODEL"]    = $("s-vision-model")?.value || "qwen/qwen2.5-vl-7b-instruct";
  const vkEl = $("s-vision-key");
  if (vkEl?.value) values["AI_VISION_API_KEY"] = vkEl.value;

  // Telegram
  const tgToken = $("s-tg-token");
  if (tgToken?.value) values["TG_BOT_TOKEN"] = tgToken.value;
  const tgChats = $("s-tg-chats")?.value?.trim();
  if (tgChats) values["TG_CHAT_IDS"] = tgChats;
  values["TG_WHITELIST_ENABLED"]  = $("s-tg-whitelist")?.checked || false;
  values["TG_NOTIFY_ON_CYCLE"]    = $("s-tg-cycle")?.checked || false;
  values["TG_NOTIFY_UNRESOLVED"]  = $("s-tg-unresolved")?.checked || false;
  values["TG_NOTIFY_ERRORS"]      = $("s-tg-errors")?.checked || false;
  values["TG_NOTIFY_READY"]       = $("s-tg-ready")?.checked || false;
  values["TG_REPORT_INCLUDE_REASONS"] = $("s-tg-reasons")?.checked !== false;
  values["TG_REPORT_INTERVAL_MINUTES"] = parseInt($("s-tg-report-interval")?.value || "60");
  values["TG_UNRESOLVED_MIN"] = parseInt($("s-tg-unresolved-min")?.value || "1");

  const res = await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({ values }),
  });
  if (res.error) { toast("Ошибка: " + res.error, "error"); return; }
  toast("Настройки сохранены", "success");
  const hint = $("settings-saved");
  if (hint) { hint.style.display = ""; setTimeout(() => hint.style.display = "none", 2000); }
  loadSystemStatus();
  loadTrafficStats();
}

async function testTelegram() {
  const resultEl = $("tg-test-result");
  if (resultEl) resultEl.innerHTML = "<span style='color:var(--text-muted)'>Отправляем...</span>";
  const res = await api("/api/telegram/test", { method: "POST" });
  if (!resultEl) return;
  if (res.ok) {
    resultEl.innerHTML = "<span style='color:var(--green)'>Сообщение отправлено! Проверь Telegram.</span>";
  } else {
    const err = (res.results || []).map(r => r.error || "?").join(", ") || res.error || "Ошибка";
    resultEl.innerHTML = `<span style='color:var(--red)'>${esc(err)}</span>`;
  }
}

function updateProcessingModeUI(mode) {
  const desc = {
    manual: "Ручной: импорт и AI только по кнопке, подтверждение обязательно",
    semiauto: "Полуавтомат: импорт вручную, AI автоматически",
    auto: "Автомат: импорт + паттерны + AI автоматически",
    auto_trust: "Полный автомат: всё автоматически включая отправку в 1С",
  };
  const el = $("s-processing-mode-desc");
  if (el) el.textContent = desc[mode] || "";
}

const AI_PROVIDER_HELP = {
  routerai: "RouterAI — облачный роутер моделей (рекомендуется).",
  gigachat: "GigaChat (Сбер). Данные в РФ. Требует Auth Key.",
  yandexgpt: "Яндекс GPT. Требует Folder ID.",
  openai_compatible: "Локальная модель (OpenAI-compatible). Укажите URL.",
};

function switchAiProvider(provider) {
  const helpEl = $("ai-provider-help");
  if (helpEl) helpEl.textContent = AI_PROVIDER_HELP[provider] || "";
  const fieldsEl = $("ai-provider-fields");
  if (!fieldsEl) return;
  if (provider === "gigachat") {
    fieldsEl.innerHTML = `<div class="form-row"><label>Auth Key</label><input type="password" id="s-gigachat-key" placeholder="Basic xxx..."></div>`;
  } else if (provider === "openai_compatible") {
    fieldsEl.innerHTML = `<div class="form-row"><label>Base URL</label><input type="text" id="s-ai-base-url" placeholder="http://host:11434/v1"></div>`;
  } else {
    fieldsEl.innerHTML = "";
  }
}

async function loadAiModels(target = "text") {
  const listEl = $("ai-models-list");
  if (listEl) listEl.textContent = target === "vision" ? "Загрузка моделей для фото/PDF..." : "Загрузка текстовых моделей...";
  const res = await api("/api/ai/models", { method: "POST" });
  const models = Array.isArray(res) ? res : (res.models || res.data || []);
  if (listEl) {
    if (models.length) {
      const rows = models.map(m => {
        const id = m.id || m.name || String(m);
        const owner = m.owned_by || m.provider || m.context_length || "";
        const price = modelPriceSummary(id);
        return `<button type="button" class="model-row" data-target="${esc(target)}" data-model="${esc(id)}">
          <span class="model-row-main">${esc(id)}</span>
          <span class="model-row-meta">${esc(String(owner || ""))}${price ? " · " + esc(price) : ""}</span>
        </button>`;
      }).join("");
      const title = target === "vision" ? "Модели для фото/PDF" : "Модели для текста";
      listEl.innerHTML = `<div class="model-list-title">${title} · ${models.length}</div><div class="model-list">${rows}</div>`;
      listEl.querySelectorAll(".model-row").forEach(row => {
        row.addEventListener("click", () => {
          const el = row.dataset.target === "vision" ? $("s-vision-model") : $("ai-model");
          if (el) el.value = row.dataset.model || "";
        });
      });
    } else {
      listEl.textContent = "Нет моделей или ошибка API.";
    }
  }
}

async function loadTokenStats() {
  try {
    const res = await api("/api/ai/token-stats");
    window._lastTokenStats = res;
    const today = res?.today || {};
    const total = res?.total || {};
    setInner("ts-today", `${(today.tokens_approx || 0).toLocaleString("ru")} (${(today.prompt_tokens_approx || 0).toLocaleString("ru")} вход / ${(today.response_tokens_approx || 0).toLocaleString("ru")} выход)`);
    setInner("ts-today-cost", formatRub(today.cost_rub));
    setInner("ts-avg-request", `${(today.avg_tokens_approx || 0).toLocaleString("ru")} ток. / ${formatRub(today.avg_cost_rub)}`);
    setInner("ts-total", `${(total.tokens_approx || 0).toLocaleString("ru")} (${(total.prompt_tokens_approx || 0).toLocaleString("ru")} вход / ${(total.response_tokens_approx || 0).toLocaleString("ru")} выход)`);
    setInner("ts-total-cost", formatRub(total.cost_rub));
    setInner("ts-requests", (today.requests || 0).toString());
    renderAiCostModels(res?.models || []);
  } catch (e) { /* ignore */ }
}

async function loadTokenReport() {
  const el = $("token-report");
  if (!el) return;
  let res;
  try { res = await api("/api/ai/token-report"); } catch (e) { return; }
  if (!res || !res.ok) return;
  const m = res.modes || {};
  const f = (n) => (Number(n) || 0).toLocaleString("ru");
  const io = (o) => `${f(o.in)}<span class="muted">↓</span> / ${f(o.out)}<span class="muted">↑</span>`;
  const row = (label, b, cls) => {
    const t = b.text || { in: 0, out: 0 }, v = b.vision || { in: 0, out: 0 }, tot = b.total || { in: 0, out: 0 };
    const visZero = !(v.in || v.out);
    return `<tr class="${cls || ""}">
      <td>${esc(label)}</td>
      <td>${io(t)}</td>
      <td class="${visZero ? "muted" : ""}">${io(v)}</td>
      <td><b>${io(tot)}</b></td>
      <td>${f(b.emails)}</td>
      <td><b>${f(Math.round(b.avg_tokens_per_email || 0))}</b></td>
    </tr>`;
  };
  let rows = "";
  for (const k of ["pattern", "full_ai", "untagged"]) {
    if (m[k] && (m[k].total.calls > 0)) rows += row(res.labels[k] || k, m[k]);
  }
  rows += row("ВСЕГО", res.total, "token-report-total");
  el.innerHTML = `<table class="token-report-table">
    <thead><tr><th>Режим</th><th>Текст</th><th>Визуал</th><th>Итого вх/вых</th><th>Писем</th><th>Сред/письмо</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <div class="group-desc">Письмо = уникальный кейс. Среднее = (вход+выход) ÷ число писем. ↓ вход · ↑ выход.</div>`;
}

function setInner(id, val) { const el = $(id); if (el) el.textContent = val; }
function formatRub(v) {
  const n = Number(v || 0);
  if (!Number.isFinite(n) || n <= 0) return "—";
  return n.toLocaleString("ru", { maximumFractionDigits: 4 }) + " ₽";
}

function renderAiCostModels(models) {
  const el = $("ai-cost-models");
  if (!el) return;
  if (!models.length) {
    el.innerHTML = '<div class="group-desc">AI-запросов пока нет.</div>';
    return;
  }
  const prices = parseAiPriceRules($("ai-price-rules")?.value || "");
  const withCosts = models.map(m => {
    const rule = prices.find(r => r.model === m.model);
    const input = Number(rule?.input_per_mtok_rub || 0);
    const output = Number(rule?.output_per_mtok_rub || 0);
    const cost = input || output
      ? ((m.prompt_tokens_approx || 0) / 1000000) * input + ((m.response_tokens_approx || 0) / 1000000) * output
      : Number(m.cost_rub || 0);
    return { ...m, priced_now: !!(input || output), cost_now: cost };
  });
  const todayCost = withCosts.reduce((sum, m) => sum + (m.cost_now || 0), 0);
  const todayRequests = Number(window._lastTokenStats?.today?.requests || 0);
  if (todayCost > 0) {
    setInner("ts-today-cost", formatRub(todayCost));
    setInner("ts-avg-request", `${(window._lastTokenStats?.today?.avg_tokens_approx || 0).toLocaleString("ru")} ток. / ${formatRub(todayCost / Math.max(1, todayRequests))}`);
  }
  el.innerHTML = withCosts.map(m => `
    <div class="ai-cost-row">
      <div><b>${esc(m.model || "?")}</b><span>${esc(m.provider || "")} · ${m.requests || 0} запр.</span></div>
      <div>${(m.avg_tokens_approx || 0).toLocaleString("ru")} ток/запр</div>
      <div>${m.priced_now || m.priced ? formatRub(m.cost_now || m.cost_rub) : "цена не задана"}</div>
    </div>
  `).join("");
}

function modelPriceSummary(model) {
  const row = parseAiPriceRules($("ai-price-rules")?.value || "").find(r => (r.model || r.id) === model);
  if (!row) return "";
  const input = Number(row.input_per_mtok_rub || 0);
  const output = Number(row.output_per_mtok_rub || 0);
  if (!input && !output) return "";
  return `${input || 0}/${output || 0} ₽ за 1М`;
}

function insertCurrentAiPriceRule(target = "text") {
  const model = target === "vision"
    ? ($("s-vision-model")?.value?.trim() || "qwen/qwen2.5-vl-7b-instruct")
    : ($("ai-model")?.value?.trim() || "deepseek/deepseek-v3");
  const data = parseAiPriceRules($("ai-price-rules")?.value || "");
  const exists = data.some(r => r && r.model === model && (r.role || "text") === target);
  if (!exists) data.push({ role: target, model, input_per_mtok_rub: 0, output_per_mtok_rub: 0, image_rub: 0 });
  renderAiPriceRows(data);
  toast(exists ? "Такая модель уже есть в ценах" : "Строка цены добавлена", exists ? "info" : "success");
}

function parseMoney(value) {
  const raw = String(value ?? "").trim().replace(/\s+/g, "").replace(",", ".");
  if (!raw) return 0;
  const n = Number(raw);
  return Number.isFinite(n) ? n : 0;
}

function parseAiPriceRules(raw) {
  if (!String(raw || "").trim()) return [];
  try {
    const parsed = JSON.parse(raw);
    const rows = Array.isArray(parsed) ? parsed : (parsed.models || []);
    return Array.isArray(rows) ? rows.map(r => ({
      role: r.role === "vision" ? "vision" : "text",
      model: String(r.model || r.id || "").trim(),
      input_per_mtok_rub: parseMoney(r.input_per_mtok_rub),
      output_per_mtok_rub: parseMoney(r.output_per_mtok_rub),
      image_rub: parseMoney(r.image_rub),
    })).filter(r => r.model) : [];
  } catch {
    return [];
  }
}

// Модель берётся из выбора сверху (Текст писем / Фото и PDF) — здесь не дублируем.
function _priceModelName(role) {
  return ((role === "vision" ? $("s-vision-model")?.value : $("ai-model")?.value) || "").trim();
}
function renderAiPriceRows(rows) {
  const container = $("ai-price-groups");
  const hidden = $("ai-price-rules");
  if (!container || !hidden) return;
  const clean = Array.isArray(rows) ? rows : [];
  const byRole = (role) => clean.find(r => (r.role || "text") === role) || {};
  const block = (role, title) => {
    const r = byRole(role);
    const mdl = _priceModelName(role) || r.model || "";
    return `<div class="ai-price-section" data-role="${role}">
      <div class="ai-price-section-title"><b>${title}</b><span class="muted" style="font-size:11px">${mdl ? esc(mdl) : "модель не выбрана выше"}</span></div>
      <div class="ai-price-fixed">
        <label>Запрос ₽<input class="price-num" data-role="${role}" data-field="image_rub" value="${esc(String(r.image_rub || ""))}" inputmode="decimal" placeholder="—"></label>
        <label>Вход ₽/млн<input class="price-num" data-role="${role}" data-field="input_per_mtok_rub" value="${esc(String(r.input_per_mtok_rub || ""))}" inputmode="decimal" placeholder="0"></label>
        <label>Выход ₽/млн<input class="price-num" data-role="${role}" data-field="output_per_mtok_rub" value="${esc(String(r.output_per_mtok_rub || ""))}" inputmode="decimal" placeholder="0"></label>
      </div>
    </div>`;
  };
  container.innerHTML = block("text", "Текстовая модель") + block("vision", "Визуальная модель");
  syncAiPriceRulesFromTable();
  container.querySelectorAll("input").forEach(inp => inp.addEventListener("input", syncAiPriceRulesFromTable));
}

function syncAiPriceRulesFromTable() {
  const rows = [];
  const container = $("ai-price-groups");
  if (!container) return rows;
  ["text", "vision"].forEach(role => {
    const inputs = container.querySelectorAll(`input[data-role="${role}"]`);
    if (!inputs.length) return;
    const row = { role, model: _priceModelName(role), image_rub: 0, input_per_mtok_rub: 0, output_per_mtok_rub: 0 };
    inputs.forEach(inp => { row[inp.dataset.field] = parseMoney(inp.value); });
    rows.push(row);
  });
  const hidden = $("ai-price-rules");
  if (hidden) hidden.value = JSON.stringify(rows, null, 2);
  if (window._lastTokenStats) renderAiCostModels(window._lastTokenStats.models || []);
  return rows;
}

function serializeAiPriceRules() {
  const rows = syncAiPriceRulesFromTable();
  return JSON.stringify(rows, null, 2);
}

function removeAiPriceRule(idx) {
  const rows = syncAiPriceRulesFromTable();
  rows.splice(idx, 1);
  renderAiPriceRows(rows);
}

/* ── Тест модели (текст: мини-чат · визуал: файл + вопрос) ── */
let _modelTestMode = "text";
function openModelTest(mode) {
  _modelTestMode = mode === "vision" ? "vision" : "text";
  const model = ((_modelTestMode === "vision" ? $("s-vision-model")?.value : $("ai-model")?.value) || "").trim();
  $("model-test-title").textContent = _modelTestMode === "vision" ? "Тест визуальной модели" : "Тест текстовой модели";
  $("model-test-model").textContent = "Модель: " + (model || "(не задана выше)");
  $("model-test-file-row").style.display = _modelTestMode === "vision" ? "" : "none";
  $("model-test-chat").innerHTML = "";
  const p = $("model-test-prompt");
  p.value = _modelTestMode === "vision" ? "Что на изображении? Какой артикул, бренд, причина возврата?" : "";
  $("model-test-modal").classList.remove("hidden");
  setTimeout(() => p.focus(), 50);
}
function closeModelTest() { $("model-test-modal")?.classList.add("hidden"); }
async function sendModelTest() {
  const prompt = ($("model-test-prompt")?.value || "").trim();
  const chat = $("model-test-chat");
  const btn = $("model-test-send");
  if (!prompt && _modelTestMode === "text") { toast("Введите запрос", "error"); return; }
  chat.insertAdjacentHTML("beforeend",
    `<div style="align-self:flex-end;background:var(--accent-light);padding:5px 9px;border-radius:8px;max-width:90%">${esc(prompt || "(вопрос по файлу)")}</div>`);
  btn.disabled = true;
  const wait = document.createElement("div");
  wait.style.cssText = "color:var(--text-muted)"; wait.textContent = "Модель думает…";
  chat.appendChild(wait); chat.scrollTop = chat.scrollHeight;
  try {
    let res;
    if (_modelTestMode === "vision") {
      const f = $("model-test-file")?.files?.[0];
      if (!f) { wait.remove(); btn.disabled = false; toast("Выберите файл", "error"); return; }
      const fd = new FormData(); fd.append("file", f); fd.append("prompt", prompt);
      res = await fetch("/api/ai/test-vision", { method: "POST", body: fd }).then(r => r.json());
    } else {
      res = await api("/api/ai/test-text", { method: "POST", body: JSON.stringify({ prompt }) });
    }
    wait.remove();
    if (res && res.ok) {
      const meta = res.model ? `<div style="font-size:10px;color:var(--text-muted);margin-top:2px">${esc(res.model)}</div>` : "";
      chat.insertAdjacentHTML("beforeend",
        `<div style="align-self:flex-start;background:var(--bg);border:1px solid var(--border);padding:5px 9px;border-radius:8px;max-width:95%;white-space:pre-wrap">${esc(res.response || "(пустой ответ)")}${meta}</div>`);
    } else {
      chat.insertAdjacentHTML("beforeend", `<div style="color:var(--red)">Ошибка: ${esc(res?.error || "нет ответа")}</div>`);
    }
  } catch (e) {
    wait.remove();
    chat.insertAdjacentHTML("beforeend", `<div style="color:var(--red)">Ошибка: ${esc(String(e))}</div>`);
  }
  btn.disabled = false;
  chat.scrollTop = chat.scrollHeight;
}

async function loadAiJournal() {
  const res = await api("/api/ai/journal?limit=20");
  const list = $("ai-journal-list");
  if (!list) return;
  const items = res.items || [];
  if (!items.length) { list.textContent = "Журнал пуст."; return; }
  list.innerHTML = items.map(i => {
    const parsed = i.response_parsed?.response || {};
    return `<div style="border-bottom:1px solid var(--border);padding:4px 0">
      <div>${i.accepted ? "✓" : "○"} ${esc(i.model || "?")} · ${fmtDate(i.created_at)}</div>
      ${parsed.claim_kind ? `<div style="color:var(--text-muted)">→ ${esc(KIND_LABELS[parsed.claim_kind] || parsed.claim_kind)}</div>` : ""}
    </div>`;
  }).join("");
}

/* ──────────────────────── Сброс ──────────────────────── */

function resetWorkOnly() {
  const modal = document.createElement("div");
  modal.className = "modal";
  modal.innerHTML = `<div class="modal-box" style="max-width:430px">
    <div class="modal-title">Обнулить обработку?</div>
    <div class="modal-body">
      Письма и вложения останутся в базе. Будут удалены только кейсы, сверка, AI-результаты, quality-отчеты и очередь 1С.
      <br><br>После этого можно заново запустить «Паттерны».
      <br><br>Введите <strong>обработка</strong>:
      <input type="text" id="reset-work-input" class="input-sm" style="width:100%;padding:8px;font-size:16px;text-align:center;margin-top:8px" autocomplete="off">
    </div>
    <div class="modal-footer">
      <button class="btn-secondary" onclick="this.closest('.modal').remove()">Отмена</button>
      <button class="btn-danger" id="reset-work-ok-btn" disabled>Обнулить</button>
    </div>
  </div>`;
  document.getElementById("app").appendChild(modal);
  const input = modal.querySelector("#reset-work-input");
  const okBtn = modal.querySelector("#reset-work-ok-btn");
  input.addEventListener("input", () => { okBtn.disabled = input.value.trim().toLowerCase() !== "обработка"; });
  okBtn.onclick = async () => {
    if (input.value.trim().toLowerCase() !== "обработка") return;
    okBtn.disabled = true;
    okBtn.textContent = "Обнуляю...";
    const res = await api("/api/v2/pipeline/reset-work", { method: "POST" });
    modal.remove();
    if (!res.ok) {
      toast("Ошибка: " + (res.message || res.error || ""), "error");
      return;
    }
    toast("Обработка обнулена, письма сохранены", "success");
    loadPipelineStatus(); loadSystemStatus(); loadEmails(); loadTrafficStats();
  };
  setTimeout(() => input.focus(), 100);
}

function resetPipeline() {
  showConfirmModal(
    "Сбросить локальные данные?",
    "Все письма, кейсы и outbox будут удалены из локальной БД.\nНастройки сохраняются. Письма на сервере НЕ удаляются.",
    () => showResetConfirm()
  );
}

function showResetConfirm() {
  const modal = document.createElement("div");
  modal.className = "modal";
  modal.innerHTML = `<div class="modal-box" style="max-width:400px">
    <div class="modal-title">Подтверждение сброса</div>
    <div class="modal-body">
      <p style="margin-bottom:12px;font-size:14px">Введите <strong>сбросить</strong>:</p>
      <input type="text" id="reset-input" class="input-sm" style="width:100%;padding:8px;font-size:16px;text-align:center" autocomplete="off">
    </div>
    <div class="modal-footer">
      <button class="btn-secondary" onclick="this.closest('.modal').remove()">Отмена</button>
      <button class="btn-danger" id="reset-ok-btn" disabled>Сбросить</button>
    </div>
  </div>`;
  document.getElementById("app").appendChild(modal);
  const input = modal.querySelector("#reset-input");
  const okBtn = modal.querySelector("#reset-ok-btn");
  input.addEventListener("input", () => { okBtn.disabled = input.value.trim().toLowerCase() !== "сбросить"; });
  okBtn.onclick = async () => {
    if (input.value.trim().toLowerCase() !== "сбросить") return;
    modal.remove();
    const res = await api("/api/v2/pipeline/reset", { method: "POST" });
    toast(res.ok ? "Данные сброшены" : "Ошибка: " + (res.error || ""), res.ok ? "success" : "error");
    loadPipelineStatus(); loadSystemStatus(); loadEmails();
  };
  setTimeout(() => input.focus(), 100);
}

/* ──────────────────────── Инициализация ──────────────────────── */

/* ──────────────────────── ВЛОЖЕНИЯ ──────────────────────── */

async function previewAttachment(attId, filename) {
  // Показываем превью Excel/CSV в панели
  toast("Загружаю таблицу...");
  const res = await api(`/api/attachments/${attId}/preview`);
  // Найдём ближайший контейнер превью
  const containers = document.querySelectorAll(`[id^="att-preview-"]`);
  let container = null;
  // Ищем тот, что рядом с кнопкой (через общий родитель)
  document.querySelectorAll(".split-half-body, .detail-body").forEach(panel => {
    if (panel.querySelector(`[onclick*="${attId}"]`)) container = panel.querySelector(`[id^="att-preview-"]`);
  });
  if (!container) container = document.querySelector(`[id^="att-preview-"]`);
  if (!container) { toast("Не найден контейнер", "error"); return; }

  if (!res.ok) {
    container.innerHTML = `<div style="color:var(--red);font-size:12px">${esc(res.error || "")}</div>`;
    return;
  }

  // ZIP: список файлов внутри + (если есть) парсинг внутреннего Excel/CSV.
  const zipHtml = (res.type === "zip" && (res.entries || []).length) ? `
    <div style="margin-top:6px">
      <div style="font-size:11px;font-weight:600;margin-bottom:3px">📦 Внутри архива (${res.entries.length}):</div>
      ${res.entries.map(en => {
        const e2 = (en.name || "").split(".").pop().toLowerCase();
        const ic = ["xlsx","xls","xlsm","csv"].includes(e2) ? "📊" : e2 === "pdf" ? "📄" : ["jpg","jpeg","png","gif","webp","bmp"].includes(e2) ? "🖼" : "📎";
        return `<div style="font-size:11px;padding:1px 0">${ic} ${esc(en.name)} <span style="color:var(--text-muted)">(${Math.round((en.size||0)/1024)} КБ)</span></div>`;
      }).join("")}
    </div>` : "";

  const sheets = res.sheets || [];
  if (!sheets.length) {
    container.innerHTML = zipHtml || `<div style="color:var(--text-muted)">Таблица пуста</div>`;
    return;
  }

  const sheet = sheets[0];
  const rows = sheet.rows || [];
  const tableHtml = `
    <div style="margin-top:6px">
      <div style="font-size:11px;font-weight:600;margin-bottom:4px">${esc(filename)} — лист: ${esc(sheet.name)}</div>
      <div style="overflow-x:auto;max-height:300px;overflow-y:auto">
        <table style="border-collapse:collapse;font-size:11px;min-width:400px">
          ${rows.map((row, ri) => `<tr style="background:${ri === 0 ? "var(--bg)" : ""}">
            ${row.map(cell => `<td style="border:1px solid var(--border);padding:2px 6px;white-space:nowrap;max-width:200px;overflow:hidden;text-overflow:ellipsis" title="${esc(cell)}">${esc(cell)}</td>`).join("")}
          </tr>`).join("")}
        </table>
      </div>
      ${sheets.length > 1 ? `<div style="font-size:10px;color:var(--text-muted);margin-top:4px">+ ${sheets.length-1} ещё листов</div>` : ""}
    </div>`;
  container.innerHTML = zipHtml + tableHtml;
}

async function visionAttachment(attId, filename) {
  toast("Vision AI анализирует...");
  const res = await api(`/api/attachments/${attId}/vision`, { method: "POST" });
  const containers = document.querySelectorAll(`[id^="att-preview-"]`);
  let container = document.querySelector(`[id^="att-preview-"]`);
  if (!container) { showVisionResult(res, filename); return; }

  if (!res.ok) {
    container.innerHTML = `<div style="color:var(--red);font-size:12px">${esc(res.error || "")}</div>`;
    return;
  }

  const fields = res.response || {};
  const fieldLabels = {
    part_number: "Артикул", brand: "Бренд", product_name: "Наименование",
    quantity: "Количество", document_number: "№ документа",
    document_date: "Дата", claim_kind: "Причина", comment: "Комментарий",
  };
  const rows = Object.entries(fieldLabels).map(([k, l]) =>
    fields[k] ? `<tr><td style="font-size:11px;color:var(--text-muted);padding:2px 6px">${l}</td><td style="font-size:12px;padding:2px 6px;font-weight:500">${esc(String(fields[k]))}</td></tr>` : ""
  ).join("");

  container.innerHTML = `
    <div style="margin-top:8px;background:var(--accent-light);border:1px solid var(--accent);border-radius:6px;padding:10px">
      <div style="font-size:11px;font-weight:600;color:var(--accent);margin-bottom:6px">Vision AI: ${esc(filename)}</div>
      <table style="border-collapse:collapse">${rows}</table>
      ${!rows ? `<div style="color:var(--text-muted);font-size:12px">Поля не распознаны</div>` : ""}
    </div>`;
  toast("Vision AI завершил", "success");
}

function showVisionResult(res, filename) {
  const fields = res.response || {};
  const text = Object.entries(fields).filter(([,v]) => v).map(([k,v]) => `${k}: ${v}`).join("\n");
  alert(`Vision AI (${filename}):\n\n${text || "Не распознано"}`);
}

/* ──────────────────────── ОБУЧЕНИЕ ПАТТЕРНОВ ──────────────────────── */


/* ───────────────── Evidence Pipeline panel ───────────────── */
let _supplierMatrix = [];
let _quickReviewPage = 1;
let _stagingPage = 1;
let _finalSorterPage = 1;
let _inboxSorterPage = 1;
let _aiTracePage = 1;
let _defectAuditPage = 1;
let _quickReviewTimer = null;
const EVIDENCE_PAGE_SIZE = 50;

function evidenceMetric(label, value, tone = "") {
  return `<div class="evidence-metric ${tone}">
    <span class="evidence-metric-label">${esc(label)}</span>
    <strong class="evidence-metric-value">${Number(value || 0).toLocaleString("ru-RU")}</strong>
  </div>`;
}

const SORTER_BUCKET_LABELS = {
  auto_safe_staged: "Safe · staged",
  auto_safe_preview_not_staged: "Safe · не staged",
  auto_warning_candidate: "Warning candidate",
  quick_review_one_click: "Quick · 1 клик",
  quick_review_choice: "Quick · выбор",
  human_review: "Ручная проверка",
  blocked_needs_rule: "Нужно правило",
  needs_link: "Нужна связка",
  terminal_non_export: "Не экспортируется",
  duplicate_or_followup: "Продолжение / дубль",
  unknown_error: "Неизвестная ошибка",
};

function sorterTone(bucket) {
  if (bucket === "auto_safe_staged" || bucket === "auto_safe_preview_not_staged") return "safe";
  if (bucket === "auto_warning_candidate" || bucket.startsWith("quick_review")) return "warning";
  if (bucket === "blocked_needs_rule" || bucket === "unknown_error") return "blocked";
  return "neutral";
}

async function loadFinalSorter(resetPage = false) {
  if (resetPage) _finalSorterPage = 1;
  const summary = await api("/api/control/final-sorting/summary");
  if (!summary.ok) { toast("Сортер: " + (summary.error || "нет карты"), "error"); return; }
  const params = new URLSearchParams({
    limit: String(EVIDENCE_PAGE_SIZE),
    offset: String((_finalSorterPage - 1) * EVIDENCE_PAGE_SIZE),
  });
  const bucket = $("sorter-bucket")?.value;
  const buyer = $("sorter-buyer")?.value;
  if (bucket) params.set("final_bucket", bucket);
  if (buyer) params.set("buyer_code", buyer);
  const data = await api("/api/control/final-sorting?" + params.toString());
  if (!data.ok) { toast("Сортер: " + (data.error || "ошибка"), "error"); return; }
  _fillSelectValues("sorter-bucket", data.facets?.final_buckets || [], "Все buckets");
  _fillSelectValues("sorter-buyer", data.facets?.buyer_codes || [], "Все поставщики");
  [...($("sorter-bucket")?.options || [])].forEach(option => {
    if (option.value) option.textContent = SORTER_BUCKET_LABELS[option.value] || option.value;
  });
  $("sorter-count").textContent = data.total || 0;
  $("sorter-generated").textContent = `Карта: ${fmtDate(summary.generated_at)}`;
  const buckets = summary.by_bucket || {};
  $("sorter-metrics").innerHTML = [
    evidenceMetric("Safe staged", buckets.auto_safe_staged, "safe"),
    evidenceMetric("Warning", buckets.auto_warning_candidate, "warning"),
    evidenceMetric("Quick 1-click", buckets.quick_review_one_click, "warning"),
    evidenceMetric("Quick choice", buckets.quick_review_choice),
    evidenceMetric("Human", buckets.human_review),
    evidenceMetric("Needs rule", buckets.blocked_needs_rule, "blocked"),
    evidenceMetric("Needs link", buckets.needs_link),
  ].join("");
  $("sorter-tbody").innerHTML = (data.items || []).map(renderFinalSorterRow).join("") ||
    `<tr><td colspan="8" class="empty-state">Кейсы не найдены</td></tr>`;
  renderPagination("sorter-pagination", data.total || 0, _finalSorterPage, EVIDENCE_PAGE_SIZE, page => {
    _finalSorterPage = page; loadFinalSorter();
  });
}

function renderFinalSorterRow(item) {
  const reasons = [...(item.blocking_reasons || []), ...(item.warning_reasons || [])].slice(0, 3);
  const firstReview = (item.review_tasks || [])[0];
  const action = firstReview
    ? `<button class="btn-sm" onclick="openQuickReviewItem('${escJs(firstReview.review_id)}')">Quick Review</button>`
    : `<button class="btn-sm" disabled>${esc(item.next_action)}</button>`;
  return `<tr>
    <td><button class="btn-link" onclick="openCaseTimeline('${escJs(String(item.case_id))}')">#${esc(item.case_id)}</button></td>
    <td>${esc(item.buyer_code)}</td>
    <td><span class="status-pill ${sorterTone(item.final_bucket)}">${esc(SORTER_BUCKET_LABELS[item.final_bucket] || item.final_bucket)}</span></td>
    <td>${esc(item.current_state || "—")}</td>
    <td><strong>${esc(item.next_action)}</strong></td>
    <td class="evidence-reasons">${reasons.map(esc).join("<br>") || "—"}</td>
    <td>${item.review_tasks_count || 0}${item.learning_ledger_status?.decisions_count ? ` · ledger ${item.learning_ledger_status.decisions_count}` : ""}</td>
    <td><div class="evidence-candidates">${action}<button class="btn-sm" onclick="openFinalSorterCase('${escJs(String(item.case_id))}')">Карта</button></div></td>
  </tr>`;
}

async function openFinalSorterCase(caseId) {
  const data = await api(`/api/control/final-sorting/case/${encodeURIComponent(caseId)}`);
  if (!data.ok) { toast(data.error || "Кейс не найден", "error"); return; }
  const item = data.item;
  const actions = (item.allowed_actions || []).map(value => `<span class="status-pill safe">${esc(value)}</span>`).join(" ");
  const forbidden = (item.forbidden_actions || []).map(value => `<span class="status-pill blocked">${esc(value)}</span>`).join(" ");
  openEvidenceModal(
    `Final Sorter · #${caseId}`,
    `<div class="evidence-band">
       <div><span class="evidence-band-label">Bucket</span><span class="status-pill ${sorterTone(item.final_bucket)}">${esc(SORTER_BUCKET_LABELS[item.final_bucket] || item.final_bucket)}</span></div>
       <div><span class="evidence-band-label">Next</span><strong>${esc(item.next_action)}</strong></div>
     </div>
     <div class="detail-section">Разрешено</div><div class="evidence-candidates">${actions || "—"}</div>
     <div class="detail-section">Запрещено</div><div class="evidence-candidates">${forbidden || "—"}</div>
     <div class="detail-section">Evidence summary</div><pre class="payload-preview">${esc(JSON.stringify(item.evidence_summary, null, 2))}</pre>
     <div class="detail-section">Маршрут</div><pre class="payload-preview">${esc(JSON.stringify({
       blocking_reasons: item.blocking_reasons,
       warning_reasons: item.warning_reasons,
       staged_status: item.staged_status,
       outbox_status: item.outbox_status,
       learning_ledger_status: item.learning_ledger_status,
     }, null, 2))}</pre>
     <div class="detail-section"><button class="btn-sm" onclick="openCaseTimeline('${escJs(String(caseId))}')">Открыть timeline</button></div>`
  );
}

async function loadInboxSorter(resetPage = false) {
  if (resetPage) _inboxSorterPage = 1;
  const params = new URLSearchParams({
    limit: String(EVIDENCE_PAGE_SIZE),
    offset: String((_inboxSorterPage - 1) * EVIDENCE_PAGE_SIZE),
  });
  const bucket = $("inbox-bucket")?.value;
  if (bucket) params.set("inbox_bucket", bucket);
  if ($("inbox-without-case")?.checked) params.set("has_case", "false");
  const [data, summaryData] = await Promise.all([
    api("/api/inbox-sorting/items?" + params.toString()),
    api("/api/inbox-sorting/summary"),
  ]);
  if (!data.ok) { toast(data.error || "Inbox snapshot не найден", "error"); return; }
  const summary = summaryData.summary || {};
  $("inbox-count").textContent = Number(data.total || 0).toLocaleString("ru-RU");
  $("inbox-metrics").innerHTML = [
    evidenceMetric("Всего raw", summary.total_raw),
    evidenceMetric("Без кейса", summary.raw_without_case, "warn"),
    evidenceMetric("В return pipeline", summary.should_enter_return_pipeline, "safe"),
    evidenceMetric("Отчёты / инфо", summary.non_return_automatic),
    evidenceMetric("Review", summary.unknown_needs_review, "blocked"),
  ].join("");
  _fillSelectValues("inbox-bucket", data.facets?.buckets || [], "Все корзины");
  $("inbox-tbody").innerHTML = (data.items || []).map(item => `<tr>
    <td><button class="btn-link" onclick="openInboxItem(${Number(item.raw_email_id)})">#${esc(item.raw_email_id)}</button></td>
    <td><span class="status-pill neutral">${esc(item.inbox_bucket)}</span></td>
    <td>${esc(item.sender_domain || item.sender || "—")}</td>
    <td>${esc(item.subject || "—")}</td>
    <td>${esc(item.confidence)}%</td>
    <td><strong>${esc(item.next_action)}</strong></td>
    <td class="evidence-reasons">${(item.reasons || []).map(esc).join("<br>") || "—"}</td>
    <td>${item.has_case ? "есть" : '<span class="status-pill warn">нет</span>'}</td>
  </tr>`).join("") || '<tr><td colspan="8" class="empty">Нет данных</td></tr>';
  renderPagination("inbox-pagination", data.total || 0, _inboxSorterPage, EVIDENCE_PAGE_SIZE, page => {
    _inboxSorterPage = page; loadInboxSorter();
  });
}

async function openInboxItem(rawEmailId) {
  const data = await api(`/api/inbox-sorting/item/${encodeURIComponent(rawEmailId)}`);
  if (!data.ok) { toast(data.error || "Raw email не найден", "error"); return; }
  openEvidenceModal(
    `Inbox Sorter · raw #${rawEmailId}`,
    `<div class="evidence-band">
       <div><span class="evidence-band-label">Корзина</span><strong>${esc(data.item.inbox_bucket)}</strong></div>
       <div><span class="evidence-band-label">Следующий шаг</span><strong>${esc(data.item.next_action)}</strong></div>
       <div><span class="evidence-band-label">Уверенность</span><strong>${esc(data.item.confidence)}%</strong></div>
     </div>
     <div class="detail-section">Тема</div><div>${esc(data.item.subject || "—")}</div>
     <div class="detail-section">Причины</div><div>${(data.item.reasons || []).map(esc).join("<br>") || "—"}</div>
     <div class="detail-section">Matched rules</div><pre class="payload-preview">${esc(JSON.stringify(data.item.matched_rules || [], null, 2))}</pre>`
  );
}

async function loadAiTrace(resetPage = false) {
  if (resetPage) _aiTracePage = 1;
  const params = new URLSearchParams({
    limit: String(EVIDENCE_PAGE_SIZE),
    offset: String((_aiTracePage - 1) * EVIDENCE_PAGE_SIZE),
  });
  const mode = $("trace-mode")?.value;
  const buyer = $("trace-buyer")?.value;
  const field = $("trace-field")?.value;
  if (mode) params.set("mode", mode);
  if (buyer) params.set("buyer_code", buyer);
  if (field) params.set("changed_field", field);
  if ($("trace-rejected")?.checked) params.set("rejected", "true");
  const data = await api("/api/ai-trace?" + params.toString());
  if (!data.ok) { toast("AI Trace: " + (data.error || "ошибка"), "error"); return; }
  _fillSelectValues("trace-mode", data.facets?.modes || [], "Все режимы");
  _fillSelectValues("trace-buyer", data.facets?.buyer_codes || [], "Все поставщики");
  _fillSelectValues("trace-field", data.facets?.changed_fields || [], "Все изменённые поля");
  $("trace-count").textContent = data.total || 0;
  $("trace-tbody").innerHTML = (data.items || []).map(item => {
    const changed = Object.entries(item.field_diff || {}).filter(([, diff]) => diff.ai_changed).map(([field]) => field);
    const gate = item.evidence_gate_result || {};
    const tokens = item.cost_tokens || {};
    return `<tr>
      <td><button class="btn-link" onclick="openAiTraceCase('${escJs(String(item.case_id))}')">#${esc(item.case_id || "—")}</button></td>
      <td>${esc(item.buyer_code || "—")}</td><td>${esc(item.mode)}</td>
      <td>${esc(item.ai_model || "—")}<div class="hint-text">${esc(item.ai_provider || "")}</div></td>
      <td>${changed.map(field => `<span class="status-pill neutral">${esc(field)}</span>`).join(" ") || "—"}</td>
      <td><span class="status-pill safe">${(item.accepted_fields || []).length}</span> / <span class="status-pill blocked">${(item.rejected_fields || []).length}</span></td>
      <td><span class="status-pill ${gate.passed ? "safe" : "blocked"}">${gate.passed ? "passed" : "blocked"}</span></td>
      <td>${tokens.input || 0} ↓ / ${tokens.output || 0} ↑</td><td>${esc(item.error || "—")}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="9" class="empty-state">AI trace пуст</td></tr>`;
  renderPagination("trace-pagination", data.total || 0, _aiTracePage, EVIDENCE_PAGE_SIZE, page => {
    _aiTracePage = page; loadAiTrace();
  });
}

async function openAiTraceCase(caseId) {
  const data = await api(`/api/ai-trace/${encodeURIComponent(caseId)}`);
  if (!data.ok) { toast(data.error || "Trace не найден", "error"); return; }
  const item = (data.items || []).slice(-1)[0];
  openEvidenceModal(
    `AI Trace · #${caseId}`,
    `<div class="detail-section">Pattern → AI → Final</div>
     <pre class="payload-preview">${esc(JSON.stringify({
       pattern_result: item.pattern_result,
       ai_result: item.ai_result,
       final_result: item.final_result,
       field_diff: item.field_diff,
     }, null, 2))}</pre>
     <div class="detail-section">Evidence decision</div>
     <pre class="payload-preview">${esc(JSON.stringify({
       accepted_fields: item.accepted_fields,
       rejected_fields: item.rejected_fields,
       evidence_gate_result: item.evidence_gate_result,
     }, null, 2))}</pre>`
  );
}

async function loadDefectAudit(resetPage = false) {
  if (resetPage) _defectAuditPage = 1;
  const params = new URLSearchParams({
    limit: String(EVIDENCE_PAGE_SIZE),
    offset: String((_defectAuditPage - 1) * EVIDENCE_PAGE_SIZE),
  });
  const defectClass = $("defect-class")?.value;
  if (defectClass) params.set("defect_class", defectClass);
  if ($("defect-photo")?.checked) params.set("has_photos", "true");
  if ($("defect-no-reason")?.checked) params.set("no_explicit_reason", "true");
  if ($("defect-ai-proposed")?.checked) params.set("ai_proposed_defect", "true");
  const data = await api("/api/ai-trace/defect-audit?" + params.toString());
  if (!data.ok) { toast("Defect Audit: " + (data.error || "ошибка"), "error"); return; }
  const summary = data.summary || {};
  $("defect-count").textContent = data.total || 0;
  $("defect-metrics").innerHTML = [
    evidenceMetric("Кандидаты", summary.total_defect_candidates),
    evidenceMetric("Подтверждено", summary.confirmed_defect, "safe"),
    evidenceMetric("Слабые", summary.weak_defect, "warning"),
    evidenceMetric("Конфликты", summary.conflict_defect, "blocked"),
    evidenceMetric("С фото", summary.defect_with_photos),
    evidenceMetric("Без причины", summary.defect_without_explicit_reason, "warning"),
    evidenceMetric("AI изменил", summary.defect_where_ai_changed_claim_kind),
  ].join("");
  $("defect-tbody").innerHTML = (data.items || []).map(item => `<tr>
    <td><button class="btn-link" onclick="openCaseTimeline('${escJs(String(item.case_id))}')">#${esc(item.case_id)}</button></td>
    <td>${esc(item.buyer_code || "—")}</td><td>${esc(item.claim_kind || "—")}</td>
    <td><span class="status-pill ${item.defect_class === "confirmed_defect" ? "safe" : item.defect_class === "conflict_defect" ? "blocked" : "warning"}">${esc(item.defect_class)}</span></td>
    <td>${esc(item.status || "—")}</td><td>${item.has_photos ? "Да" : "Нет"}</td>
    <td>${item.ai_proposed_defect ? "Да" : "Нет"}</td><td class="evidence-reasons">${esc(item.evidence_snippet || "—")}</td>
  </tr>`).join("") || `<tr><td colspan="8" class="empty-state">Кейсы не найдены</td></tr>`;
  renderPagination("defect-pagination", data.total || 0, _defectAuditPage, EVIDENCE_PAGE_SIZE, page => {
    _defectAuditPage = page; loadDefectAudit();
  });
}

async function loadEvidenceDashboard() {
  const data = await api("/api/evidence/summary");
  if (!data.ok) { toast("Evidence summary: " + (data.error || "ошибка"), "error"); return; }
  $("evidence-metrics").innerHTML = [
    evidenceMetric("Auto safe", data.auto_export_safe, "safe"),
    evidenceMetric("Auto warning", data.auto_export_with_warning, "warning"),
    evidenceMetric("Quick review", data.quick_review),
    evidenceMetric("Human review", data.human_review),
    evidenceMetric("Staged", data.staging_count, "safe"),
    evidenceMetric("Real outbox", data.real_outbox_count),
    evidenceMetric("Blocked", data.blocked, "blocked"),
  ].join("");
  $("evidence-last-run").textContent = `Последний dry-run: ${fmtDate(data.last_run_at)}`;
  $("evidence-source").textContent = data.source || "—";
  $("evidence-runtime-errors").textContent = data.runtime_errors || 0;
}

async function loadEvidenceSuppliers() {
  const data = await api("/api/evidence/suppliers");
  if (!data.ok) { toast("Матрица поставщиков: " + (data.error || "ошибка"), "error"); return; }
  _supplierMatrix = data.items || [];
  $("supplier-matrix-count").textContent = _supplierMatrix.length;
  renderSupplierMatrix();
}

function renderSupplierMatrix() {
  const query = ($("supplier-matrix-search")?.value || "").trim().toLowerCase();
  const rows = _supplierMatrix.filter(row => !query || String(row.buyer_code).toLowerCase().includes(query));
  $("supplier-matrix-tbody").innerHTML = rows.map(row => {
    const blockers = (row.top_blocking || []).map(x => `${esc(x.reason)} (${x.count})`).join("<br>");
    return `<tr>
      <td><strong>${esc(row.buyer_code)}</strong><div class="hint-text">${row.total} всего</div></td>
      <td>${row.returns}</td><td><strong>${row.auto_percent}%</strong></td>
      <td><span class="status-pill safe">${row.safe}</span></td>
      <td><span class="status-pill warning">${row.warning}</span></td>
      <td>${row.quick}</td><td>${row.human}</td><td>${row.blocked}</td>
      <td class="evidence-reasons">${blockers || "—"}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="9" class="empty-state">Поставщики не найдены</td></tr>`;
}

function _fillSelectFromRows(selectId, rows, field, placeholder) {
  const select = $(selectId);
  if (!select) return;
  const current = select.value;
  const values = [...new Set(rows.map(row => row[field]).filter(Boolean))].sort();
  select.innerHTML = `<option value="">${esc(placeholder)}</option>` +
    values.map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join("");
  if (values.includes(current)) select.value = current;
}

function _fillSelectValues(selectId, values, placeholder) {
  const select = $(selectId);
  if (!select) return;
  const current = select.value;
  select.innerHTML = `<option value="">${esc(placeholder)}</option>` +
    (values || []).map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join("");
  if ((values || []).includes(current)) select.value = current;
}

function scheduleQuickReviewReload() {
  clearTimeout(_quickReviewTimer);
  _quickReviewTimer = setTimeout(() => loadQuickReviewQueue(true), 300);
}

async function loadQuickReviewQueue(resetPage = false) {
  if (resetPage) _quickReviewPage = 1;
  const params = new URLSearchParams({
    limit: String(EVIDENCE_PAGE_SIZE),
    offset: String((_quickReviewPage - 1) * EVIDENCE_PAGE_SIZE),
  });
  const buyer = $("qr-buyer")?.value;
  const field = $("qr-field")?.value;
  const reason = $("qr-reason")?.value?.trim();
  if (buyer) params.set("buyer_code", buyer);
  if (field) params.set("field", field);
  if (reason) params.set("reason", reason);
  if ($("qr-one-click")?.checked) params.set("one_click_only", "true");
  const data = await api("/api/quick-review/queue?" + params.toString());
  if (!data.ok) { toast("Quick Review: " + (data.error || "ошибка"), "error"); return; }
  $("qr-count").textContent = data.total || 0;
  _fillSelectValues("qr-buyer", data.facets?.buyer_codes || [], "Все поставщики");
  $("qr-tbody").innerHTML = (data.items || []).map(renderQuickReviewRow).join("") ||
    `<tr><td colspan="7" class="empty-state">Задач нет</td></tr>`;
  renderPagination("qr-pagination", data.total || 0, _quickReviewPage, EVIDENCE_PAGE_SIZE, page => {
    _quickReviewPage = page; loadQuickReviewQueue();
  });
}

function renderQuickReviewRow(item) {
  const candidates = item.candidates || [];
  const quickButtons = candidates.slice(0, item.one_click ? 1 : 3).map(candidate =>
    `<button class="evidence-candidate" title="${esc(candidate.evidence || "")}"
      onclick="submitQuickReviewDecision('${escJs(item.review_id)}','${escJs(String(candidate.value))}',this)">
      ${esc(candidate.label || candidate.value)}
    </button>`
  ).join("");
  return `<tr data-review-id="${esc(item.review_id)}">
    <td><button class="btn-link" onclick="openCaseTimeline('${escJs(String(item.case_id))}')">#${esc(item.case_id)}</button></td>
    <td>${esc(item.buyer_code)}</td>
    <td>${esc(item.field)}</td>
    <td class="evidence-reasons">${esc(item.reason)}</td>
    <td>${esc(item.current_value ?? "—")}</td>
    <td><div class="evidence-candidates">${quickButtons}</div></td>
    <td><button class="btn-sm" onclick="openQuickReviewItem('${escJs(item.review_id)}')">Открыть</button></td>
  </tr>`;
}

async function submitQuickReviewDecision(reviewId, selectedValue, button = null, comment = "") {
  if (button) button.disabled = true;
  const data = await api("/api/quick-review/decision", {
    method: "POST",
    body: JSON.stringify({ review_id: reviewId, selected_value: selectedValue, operator: "manual/ui", comment }),
  });
  if (!data.ok) {
    if (button) button.disabled = false;
    toast("Решение не записано: " + (data.error || "ошибка"), "error");
    return;
  }
  document.querySelector(`tr[data-review-id="${CSS.escape(reviewId)}"]`)?.classList.add("evidence-row-decided");
  closeEvidenceModal();
  toast("Решение добавлено в Learning Ledger", "success");
}

async function openQuickReviewItem(reviewId) {
  const data = await api("/api/quick-review/item/" + encodeURIComponent(reviewId));
  if (!data.ok) { toast(data.error || "Задача не найдена", "error"); return; }
  const item = data.item;
  const candidates = (item.candidates || []).map(candidate =>
    `<button class="evidence-candidate" onclick="submitQuickReviewDecision('${escJs(item.review_id)}','${escJs(String(candidate.value))}',this)">
      ${esc(candidate.label || candidate.value)} · ${Math.round(Number(candidate.confidence || 0) * 100)}%
    </button>`
  ).join("");
  openEvidenceModal(
    `Quick Review · #${item.case_id}`,
    `<div class="detail-section"><b>${esc(item.subject || "")}</b></div>
     <div class="detail-section"><span class="hint-text">${esc(item.reason)} · ${esc(item.field)}</span></div>
     <div class="detail-section"><div class="evidence-candidates">${candidates}</div></div>
     <div class="detail-section"><pre class="payload-preview">${esc(item.source_snippet || "")}</pre></div>
     <div class="detail-section"><button class="btn-sm" onclick="openCaseTimeline('${escJs(String(item.case_id))}')">Timeline кейса</button></div>`
  );
}

async function loadOutboxStaging(resetPage = false) {
  if (resetPage) _stagingPage = 1;
  const params = new URLSearchParams({
    limit: String(EVIDENCE_PAGE_SIZE),
    offset: String((_stagingPage - 1) * EVIDENCE_PAGE_SIZE),
  });
  const buyer = $("staging-buyer")?.value;
  const kind = $("staging-kind")?.value;
  if (buyer) params.set("buyer_code", buyer);
  if (kind) params.set("claim_kind", kind);
  const data = await api("/api/outbox-staging?" + params.toString());
  if (!data.ok) { toast("Staging: " + (data.error || "ошибка"), "error"); return; }
  $("staging-count").textContent = data.total || 0;
  _fillSelectValues("staging-buyer", data.facets?.buyer_codes || [], "Все поставщики");
  $("staging-tbody").innerHTML = (data.items || []).map(item => {
    const payload = item.one_c_payload_preview || {};
    const doc = payload.document || {};
    const claim = payload.claim || {};
    const product = (payload.items || [])[0] || {};
    return `<tr>
      <td><button class="btn-link" onclick="openCaseTimeline('${escJs(String(item.case_id))}')">#${esc(item.case_id)}</button></td>
      <td>${esc(item.buyer_code)}</td><td>${esc(doc.number)}<div class="hint-text">${esc(doc.date)}</div></td>
      <td>${esc(product.part_number)}</td><td>${esc(product.quantity)}</td><td>${esc(KIND_LABELS[claim.kind] || claim.kind)}</td>
      <td>${fmtDate(item.staged_at)}</td>
      <td><button class="btn-sm" onclick="openStagingItem('${escJs(item.idempotency_key)}')">Payload</button></td>
    </tr>`;
  }).join("") || `<tr><td colspan="8" class="empty-state">Staging пуст</td></tr>`;
  renderPagination("staging-pagination", data.total || 0, _stagingPage, EVIDENCE_PAGE_SIZE, page => {
    _stagingPage = page; loadOutboxStaging();
  });
}

async function openStagingItem(key) {
  const data = await api("/api/outbox-staging/item/" + encodeURIComponent(key));
  if (!data.ok) { toast(data.error || "Staging item не найден", "error"); return; }
  const item = data.item;
  openEvidenceModal(
    `Staging · #${item.case_id}`,
    `<div class="evidence-band">
       <div><span class="evidence-band-label">Статус</span><span class="status-pill safe">${esc(item.status)}</span></div>
       <div><span class="evidence-band-label">Класс</span>${esc(item.safety_class)}</div>
     </div>
     <pre class="payload-preview">${esc(JSON.stringify(item.one_c_payload_preview, null, 2))}</pre>
     <div class="modal-footer"><button class="btn-sm" disabled>Approve: next step</button><button class="btn-sm" disabled>Reject: next step</button></div>`
  );
}

async function openCaseTimeline(caseId) {
  const data = await api(`/api/case/${encodeURIComponent(caseId)}/timeline`);
  if (!data.ok) { toast(data.error || "Timeline не найден", "error"); return; }
  const rows = (data.stages || []).map(stage => `<div class="timeline-row">
    <div class="timeline-stage">${esc(stage.stage)}</div>
    <div><span class="status-pill ${stage.status === "passed" || stage.status === "staged" ? "safe" : stage.status === "blocked" ? "blocked" : "neutral"}">${esc(stage.status)}</span></div>
    <pre class="timeline-details">${esc(JSON.stringify(stage.details || {}, null, 2))}</pre>
  </div>`).join("");
  openEvidenceModal(`Timeline · кейс #${caseId}`, `<div class="timeline-list">${rows}</div>`);
}

function openEvidenceModal(title, body) {
  $("evidence-modal-title").textContent = title;
  $("evidence-modal-body").innerHTML = body;
  $("evidence-modal").classList.remove("hidden");
}

function closeEvidenceModal() {
  $("evidence-modal")?.classList.add("hidden");
}

// ── Operator Control Center (Пульт) ──────────────────────────────────
function _yn(v) { return v ? "да" : "нет"; }
function _card(title, rows, cls) {
  const body = rows.map(r => `<div class="dash-row"><span>${esc(r[0])}</span><strong class="${r[2]||''}">${esc(String(r[1]))}</strong></div>`).join("");
  return `<div class="dash-card ${cls||''}"><div class="dash-card-h">${esc(title)}</div>${body}</div>`;
}

async function loadDashboard() {
  const grid = $("dash-grid");
  if (!grid) return;
  let o;
  try { o = await api("/api/dashboard/overview"); }
  catch (e) { grid.innerHTML = `<div class="dash-loading">Не удалось загрузить пульт: ${esc(String(e))}</div>`; return; }
  const srv = o.server || {}, au = o.auth || {}, mail = o.mail || {}, proc = o.processing || {};
  const wk = (o.workers || {}).workers || {}, ob = o.outbox || {}, ai = o.ai || {};
  const cards = [];
  // Сервер
  cards.push(_card("🖥 Сервер", [
    ["Статус", srv.status || "—", "ok"],
    ["Хост", `${srv.host || "?"}:${srv.port || "?"}`],
    ["LAN", _yn(srv.allow_lan)],
    ["Auth", _yn(au.enforced)],
    ["Developer mode", _yn(srv.developer_mode)],
  ]));
  // Почта
  cards.push(_card(`📬 Почта ${mail.stale ? "· снимок устарел" : ""}`, [
    ["Server total", mail.server_total ?? "—"],
    ["Local raw", mail.local_raw_total ?? "—"],
    ["Missing", mail.missing_local ?? "—", (mail.missing_local ? "warn" : "")],
    ["Fetch failed", mail.fetch_failed ?? "—"],
    ["Quarantine", mail.quarantine ?? 0],
    ["Skipped (до старта)", mail.skipped_before_start ?? 0],
    ["Сверка", mail.checked_at ? `снимок ${mail.mtime || mail.checked_at}` : "нет снимка"],
  ]));
  // Обработка
  cards.push(_card("⚙️ Обработка", [
    ["Cases всего", proc.cases_total ?? "—"],
    ["Raw без кейса", proc.raw_without_case ?? "—", (proc.raw_without_case ? "warn" : "")],
    ["Возвраты (new_return)", proc.return_claim ?? "—"],
    ["Готово к 1С", proc.ready_to_1c ?? 0, "ok"],
    ["На проверку", proc.needs_review ?? 0],
    ["Quick review (снимок)", proc.quick_review_snapshot ?? "—"],
  ]));
  // Workers
  const wkRows = Object.keys(wk).map(w => [w, wk[w].state, wk[w].state === "running" ? "ok" : (wk[w].state === "paused" ? "warn" : "")]);
  cards.push(_card("🔧 Workers", wkRows.length ? wkRows : [["—", "нет данных"]]));
  // Outbox / 1С
  cards.push(_card("📤 Outbox / 1С", [
    ["New", (ob.by_status || {}).new ?? 0],
    ["Error", (ob.by_status || {}).error ?? 0, ((ob.by_status||{}).error ? "warn" : "")],
    ["Sent", (ob.by_status || {}).sent ?? 0],
    ["Контрольные события", ob.control_events ?? 0],
    ["Возвратные заявки", ob.business_events ?? 0],
    ["Автодоставка", _yn(ob.delivery_enabled), (ob.delivery_enabled ? "ok" : "warn")],
  ]));
  // AI
  cards.push(_card("🤖 AI", [
    ["Включён", _yn(ai.enabled), (ai.enabled ? "warn" : "ok")],
    ["Паттерны", "0 токенов (без AI)", "ok"],
    ["Вызовов сегодня", ai.calls_today ?? 0],
    ["Стоимость сегодня", ai.cost_today ?? 0],
    ["Стоимость за месяц", ai.cost_month ?? 0],
  ]));
  grid.innerHTML = cards.join("");
  // Объяснение outbox
  if (ob.explanation) {
    grid.insertAdjacentHTML("beforeend",
      `<div class="dash-card wide ${((ob.by_status||{}).error ? "warn" : "")}"><div class="dash-card-h">Почему outbox в очереди</div><div class="dash-note">${esc(ob.explanation)}</div></div>`);
  }
  // Предупреждение о смене пароля
  if (au.bootstrap_required) {
    grid.insertAdjacentHTML("afterbegin",
      `<div class="dash-card wide danger"><div class="dash-card-h">⚠️ Смените пароль</div><div class="dash-note">Используется временный admin/admin. <button class="btn-sm" onclick="location.href='/login'">Сменить пароль</button></div></div>`);
  }
  renderDashActions();
}

function renderDashActions() {
  const box = $("dash-actions");
  if (!box) return;
  const dev = window._readmailDev || {};
  const isAdmin = dev.role === "admin" || dev.role === "developer";
  const safe = [
    `<button class="btn-sm" onclick="dashPause()">⏸ Пауза всех</button>`,
    `<button class="btn-sm" onclick="dashResume()">▶️ Возобновить</button>`,
    `<button class="btn-sm" onclick="activateTab('quick_review_pipeline',true)">Quick Review</button>`,
    `<button class="btn-sm" onclick="activateTab('outbox_staging',true)">Staging</button>`,
    `<button class="btn-sm" onclick="loadDashboard()">⟳ Обновить</button>`,
  ];
  // Опасные действия — только admin/developer и с confirm; в operator не показываем.
  const danger = isAdmin ? [
    `<button class="btn-sm danger" title="Требует подтверждения" onclick="confirmDanger('Доставить outbox в 1С?', null)" disabled>Доставить в 1С (off)</button>`,
  ] : [];
  box.innerHTML = safe.join(" ") + (danger.length ? ` <span class="dash-sep">|</span> ` + danger.join(" ") : "");
}

async function dashPause() { try { await api("/api/runtime/pause", {method:"POST"}); } catch(e){} loadDashboard(); }
async function dashResume() { try { await api("/api/runtime/resume", {method:"POST"}); } catch(e){} loadDashboard(); }
function confirmDanger(msg, fn) { if (confirm(msg) && typeof fn === "function") fn(); }

async function doLogout() {
  try { await api("/api/auth/logout", {method:"POST"}); } catch(e){}
  location.href = "/login";
}

async function loadAuthChip() {
  try {
    const me = await api("/api/auth/me");
    const chip = $("user-chip");
    if (me && me.authenticated) {
      $("user-chip-name").textContent = `${me.username} (${me.role})`;
      if (chip) chip.style.display = "";
    } else {
      // если auth включён и мы не вошли — на login
      const st = await api("/api/auth/status").catch(() => ({}));
      if (st.auth_required) location.href = "/login";
    }
  } catch(e) {}
}

const ENGINEERING_TABS = ["dashboard","evidence","ai_trace","defect_audit","outbox_staging","inbox_sorter","final_sorter","supplier_matrix","quick_review_pipeline"];

async function applyUiMode() {
  try {
    const me = await api("/api/auth/me").catch(() => ({}));
    const role = (me && me.role) || "operator";
    const mode = await api("/api/ui/mode?role=" + encodeURIComponent(role));
    const dev = !!mode.developer_mode;
    window._readmailDev = {role, developer_mode: dev};
    // Один shell: body-класс управляет инженерными блоками (pipeline-bar и пр.) через CSS.
    document.body.classList.toggle("developer-mode", dev);
    document.body.classList.toggle("operator-mode", !dev);
    // Инженерные вкладки прячем в operator-режиме.
    document.querySelectorAll('.tab[data-tab]').forEach(btn => {
      const t = btn.getAttribute('data-tab');
      btn.style.display = (ENGINEERING_TABS.includes(t) && !dev) ? "none" : "";
    });
    // Схлопнуть nav-группы, у которых все вкладки скрыты.
    document.querySelectorAll('.nav-group').forEach(group => {
      const tabs = group.querySelectorAll('.tab[data-tab]');
      if (!tabs.length) return;
      const allHidden = Array.from(tabs).every(b => b.style.display === "none");
      group.style.display = allHidden ? "none" : "";
    });
    // Если активная вкладка оказалась скрытой (напр. operator открыл dev-вкладку) — вернуться на Письма.
    const active = document.querySelector('.tab.active[data-tab]');
    if (active && active.style.display === "none") activateTab("emails", true);
  } catch(e) {}
}

// ── Глобальный поиск (письмо/кейс/outbox/клиент) ─────────────────────
let _gsTimer = null;
const GS_TYPE_LABEL = {raw_email: "Письма", case: "Кейсы", outbox: "Outbox", client: "Клиенты", pattern: "Паттерны"};
const GS_TAB_INPUT = {emails: "emails-search", review: "review-search", onec: "onec-search", clients: "clients-search"};

async function runGlobalSearch(q) {
  const dd = $("gsearch-dropdown");
  if (!dd) return;
  q = (q || "").trim();
  if (q.length < 1) { dd.style.display = "none"; dd.innerHTML = ""; return; }
  let res;
  try { res = await api("/api/search?q=" + encodeURIComponent(q) + "&limit=20"); }
  catch (e) { dd.innerHTML = `<div class="gs-empty">Ошибка поиска</div>`; dd.style.display = "block"; return; }
  if (!res.ok || !res.results || !res.results.length) {
    dd.innerHTML = `<div class="gs-empty">Ничего не найдено по «${esc(q)}» <span class="gs-type">(${esc(res.detected_type || "")})</span></div>`;
    dd.style.display = "block"; return;
  }
  const groups = {};
  res.results.forEach(r => { (groups[r.type] = groups[r.type] || []).push(r); });
  let html = `<div class="gs-head">Тип запроса: <b>${esc(res.detected_type)}</b> · найдено ${res.total}</div>`;
  Object.keys(groups).forEach(type => {
    html += `<div class="gs-group">${esc(GS_TYPE_LABEL[type] || type)}</div>`;
    groups[type].slice(0, 6).forEach((r, i) => {
      const payload = encodeURIComponent(JSON.stringify(r));
      html += `<div class="gs-item" data-r="${payload}">
        <div class="gs-title">#${esc(String(r.id))} ${esc(r.title)}</div>
        <div class="gs-sub">${esc(r.subtitle || "")} <span class="gs-matched">${(r.matched_fields||[]).map(esc).join(", ")}</span></div>
      </div>`;
    });
  });
  dd.innerHTML = html;
  dd.style.display = "block";
  dd.querySelectorAll(".gs-item").forEach(el => {
    el.addEventListener("click", () => openSearchResult(JSON.parse(decodeURIComponent(el.getAttribute("data-r")))));
  });
}

function openSearchResult(r) {
  const dd = $("gsearch-dropdown");
  if (dd) dd.style.display = "none";
  const tab = r.open_tab || "emails";
  activateTab(tab, true);
  // Пробросить запрос в локальный поиск вкладки, чтобы оператор увидел отфильтрованный объект.
  const inputId = GS_TAB_INPUT[tab];
  const token = String((r.open_params && (r.open_params.document_number || r.open_params.case_id ||
    r.open_params.outbox_id || r.open_params.raw_email_id || r.open_params.buyer_code)) || r.id || "");
  if (inputId) {
    const el = $(inputId);
    if (el) { el.value = token; el.dispatchEvent(new Event("input", {bubbles: true})); }
  }
}

function initGlobalSearch() {
  const inp = $("gsearch-input");
  const dd = $("gsearch-dropdown");
  if (!inp) return;
  inp.addEventListener("input", () => { clearTimeout(_gsTimer); _gsTimer = setTimeout(() => runGlobalSearch(inp.value), 300); });
  inp.addEventListener("keydown", (e) => { if (e.key === "Escape") { dd.style.display = "none"; inp.blur(); } });
  document.addEventListener("click", (e) => { if (dd && !$("global-search").contains(e.target)) dd.style.display = "none"; });
  // Ctrl+K / Cmd+K — фокус на поиск
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) { e.preventDefault(); inp.focus(); inp.select(); }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initTrainingMode();
  initAutoScan();
  loadSystemStatus();
  loadPipelineStatus();
  loadAuthChip();
  applyUiMode();
  initGlobalSearch();
  // Первый экран — Письма (как в старой панели). Пульт переехал в developer mode.
  let savedTab = localStorage.getItem("readmail.activeTab") || "emails";
  if (savedTab === "dashboard") savedTab = "emails";  // не открывать Пульт первым у вернувшихся
  if (!activateTab(savedTab, false)) loadEmails();

  // Стоп-кнопки (onclick на них нет — вешаем только здесь)
  $("pipeline-btn-import-stop")?.addEventListener("click", stopImport);
  $("pipeline-btn-patterns-stop")?.addEventListener("click", async () => {
    await api("/api/patterns/stop", { method: "POST" });
    btnToggle("pipeline-btn-patterns", false);
  });
  $("pipeline-btn-ai-stop")?.addEventListener("click", stopAiBatch);
  // Автопилот — кнопки в панели (у них нет onclick). Обёртки, чтобы click-event не ушёл как mode.
  $("pipeline-btn-autopilot-ai")?.addEventListener("click", () => startAutopilot("full_ai"));
  $("pipeline-btn-autopilot-stop")?.addEventListener("click", stopAutopilot);
  $("btn-add-text-price")?.addEventListener("click", () => insertCurrentAiPriceRule("text"));
  $("btn-add-vision-price")?.addEventListener("click", () => insertCurrentAiPriceRule("vision"));

  // Поиск/фильтры с debounce
  let searchTimer;
  const debouncedLoad = (fn) => (e) => { clearTimeout(searchTimer); searchTimer = setTimeout(fn, 350); };
  $("emails-search")?.addEventListener("input", debouncedLoad(loadEmails));
  $("emails-filter")?.addEventListener("change", loadEmails);
  $("emails-buyer")?.addEventListener("change", loadEmails);
  $("emails-sort")?.addEventListener("change", loadEmails);
  $("emails-page-size")?.addEventListener("change", loadEmails);
  $("links-search")?.addEventListener("input", debouncedLoad(() => { _linksPage = 1; loadLinks(); }));
  $("offtopic-search")?.addEventListener("input", debouncedLoad(() => { _offtopicPage = 1; loadOfftopic(); }));

  // Запуск slow-polling (10s). Если что-то будет работать — автоматически ускорится до 2s
  _pollSlow = setInterval(pollTick, 10000);
});

/* ── Обработанные / не требуют действия (Hidden Processed Mail) ── */
let _processedSummary = null, _processedGroup = null, _processedSub = null, _processedPage = 1;

async function loadProcessedHidden() {
  try {
    const s = await api("/api/processed-hidden/summary");
    if (!s || !s.ok) { $("processed-banner").textContent = "Ошибка загрузки"; return; }
    _processedSummary = s;
    const badge = $("badge-processed"); if (badge) badge.textContent = s.hidden_from_operator || 0;
    // accounting banner
    const ok = s.accounted_ok;
    $("processed-banner").style.background = ok ? "rgba(40,160,80,.15)" : "rgba(200,60,60,.15)";
    $("processed-banner").innerHTML =
      `<b>${ok ? "✅ Все письма учтены" : "❌ Расхождение"}:</b> всего <b>${s.total_raw}</b> = ` +
      `рабочие <b>${s.working_total}</b> + раздел <b>${s.hidden_from_operator}</b> ` +
      `· требуют действия: ${s.requires_action} · не требуют: ${s.no_action} ` +
      `· технические: ${s.technical} · неучтённых: ${s.unaccounted}`;
    // group chips
    if (!_processedGroup || !s.groups.some(g => g.key === _processedGroup)) {
      const first = s.groups.find(g => g.count > 0) || s.groups[0];
      _processedGroup = first ? first.key : null; _processedSub = null;
    }
    $("processed-groups").innerHTML = s.groups.map(g => {
      const active = g.key === _processedGroup;
      const warn = g.requires_action ? " ⚠" : "";
      return `<button class="btn-sm ${active ? "success" : ""}" onclick="selectProcessedGroup('${g.key}')">` +
             `${g.title}${warn} <b>${g.count}</b></button>`;
    }).join("");
    renderProcessedSubgroups();
    loadProcessedItems();
  } catch (e) { console.warn("loadProcessedHidden error:", e); }
}

function renderProcessedSubgroups() {
  const box = $("processed-subgroups"); if (!box) return;
  const g = (_processedSummary?.groups || []).find(x => x.key === _processedGroup);
  const subs = (g && g.subgroups) || [];
  if (subs.length <= 1) { box.innerHTML = ""; return; }
  const all = `<button class="btn-sm ${!_processedSub ? "success" : ""}" onclick="selectProcessedSub(null)">Все <b>${g.count}</b></button>`;
  box.innerHTML = all + subs.map(sc => {
    // JSON.stringify давал ДВОЙНЫЕ кавычки внутри onclick="..." (тоже двойные) → атрибут
    // схлопывался, подгруппы (напр. «маркировка») не кликались. Одинарные кавычки + экранирование.
    const sub = String(sc.subcategory);
    const jsArg = sub.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
    return `<button class="btn-sm ${_processedSub === sc.subcategory ? "success" : ""}" ` +
      `onclick="selectProcessedSub('${jsArg}')">${esc(sub)} <b>${sc.count}</b></button>`;
  }).join("");
}

function selectProcessedGroup(key) {
  _processedGroup = key; _processedSub = null; _processedPage = 1;
  $("processed-trace").style.display = "none";
  loadProcessedHidden();
}
function selectProcessedSub(sc) {
  _processedSub = sc; _processedPage = 1; renderProcessedSubgroups(); loadProcessedItems();
}

async function loadProcessedItems() {
  try {
    const q = $("processed-search")?.value || "";
    const params = new URLSearchParams({ group: _processedGroup || "", page: _processedPage,
                                         page_size: 50, q });
    if (_processedSub) params.set("subcategory", _processedSub);
    const res = await api("/api/processed-hidden/items?" + params.toString());
    const list = $("processed-list");
    if (!res || !res.ok) { list.innerHTML = "<div class='hint-text'>Ошибка</div>"; return; }
    if (!res.items.length) { list.innerHTML = "<div class='hint-text'>Писем нет</div>"; }
    else {
      list.innerHTML = res.items.map(it => `
        <div class="loose-card" style="display:flex;flex-direction:column;gap:2px;padding:8px;border-bottom:1px solid var(--border,#3333)">
          <div style="display:flex;justify-content:space-between;gap:8px">
            <b style="flex:1">${esc(it.subject || "(без темы)")}</b>
            <button class="btn-sm" onclick="openProcessedTrace('${it.trace_target}',${it.trace_id})">Trace</button>
          </div>
          <div style="font-size:12px;color:var(--text-muted,#999)">
            ${esc(it.from_addr || "")} · ${esc(it.buyer_code || "")} · raw ${it.raw_email_id}${it.case_id ? " · case " + it.case_id : ""}
          </div>
          <div style="font-size:12px">📂 ${esc(it.folder_name)} · <i>${esc(it.why_hidden || "")}</i></div>
          <div style="font-size:12px;color:var(--accent,#6cf)">→ ${esc(it.next_action || "")}</div>
        </div>`).join("");
    }
    $("processed-pagination").innerHTML = renderProcessedPager(res);
  } catch (e) { console.warn("loadProcessedItems error:", e); }
}

function renderProcessedPager(res) {
  const pages = Math.max(1, Math.ceil(res.total / res.page_size));
  const prev = res.page > 1 ? `<button class="btn-sm" onclick="_processedPage=${res.page - 1};loadProcessedItems()">←</button>` : "";
  const next = res.page < pages ? `<button class="btn-sm" onclick="_processedPage=${res.page + 1};loadProcessedItems()">→</button>` : "";
  return `${prev} Показано ${res.shown_from}–${res.shown_to} из ${res.total} (стр. ${res.page}/${pages}) ${next}`;
}

async function openProcessedTrace(target, id) {
  const box = $("processed-trace"); box.style.display = "block";
  box.innerHTML = "Загрузка трассировки…";
  const url = target === "case" ? `/api/cases/${id}/decision` : `/api/raw-emails/${id}/decision`;
  const d = await api(url);
  if (!d || !d.ok) { box.innerHTML = "Трассировка недоступна"; return; }
  const sr = d.safety_router_result || {};
  box.innerHTML =
    `<b>DECISION · ${target} ${id}</b> <button class="btn-sm" style="float:right" onclick="$('processed-trace').style.display='none'">✕</button><br>` +
    `<b>visible_bucket:</b> ${esc(d.visible_bucket || "")} · <b>subcat:</b> ${esc(d.subcategory || "")}<br>` +
    `<b>почему скрыто:</b> ${esc(d.why_hidden || "—")}<br>` +
    `<b>routing_reason:</b> ${esc(d.explanation_short || (sr.routing_reason || ""))}<br>` +
    `<b>evidence_strength:</b> ${esc(sr.evidence_strength || "")} · <b>conflicts:</b> ${esc((d.conflicts || []).join(", ") || "—")}<br>` +
    `<b>next_action:</b> ${esc(d.next_action || sr.next_action || "")}`;
}

/* фоновое число для вкладки «Обработанные» (cached endpoint, не блокирует UI) */
async function refreshProcessedBadge() {
  try {
    const s = await api("/api/processed-hidden/summary");
    const b = $("badge-processed");
    if (b && s && s.ok) { const n = s.hidden_from_operator || 0; b.textContent = n > 0 ? n : ""; b.style.display = n > 0 ? "inline-block" : "none"; }
  } catch (e) { /* ignore */ }
}

/* ── Pipeline (единый canonical-вид: все письма по route) ── */
const PIPELINE_ROUTE_TITLES = {
  ready_for_operator: "Готово к проверке", ai_assist: "AI-разбор", manual_review: "Ручная обработка",
  ready_to_1c: "Готово к 1С", no_action_archive: "Обработанные / не требуют действия",
  error_technical: "Технические / ошибки",
};
const PIPELINE_ORDER = ["ready_for_operator", "ai_assist", "manual_review", "ready_to_1c",
                        "no_action_archive", "error_technical"];
let _pipelineAcc = null, _pipelineRoute = null, _pipelineReason = null, _pipelinePage = 1;

async function loadPipeline() {
  try {
    const acc = await api("/api/pipeline/accounting");
    if (!acc || !acc.ok) { $("pipeline-banner").textContent = "Ошибка загрузки pipeline"; return; }
    _pipelineAcc = acc;
    const badge = $("badge-pipeline"); if (badge) badge.textContent = acc.total_raw || 0;
    const ok = acc.unaccounted === 0;
    $("pipeline-banner").style.background = ok ? "rgba(40,160,80,.15)" : "rgba(200,60,60,.15)";
    const parts = PIPELINE_ORDER.map(r => `${PIPELINE_ROUTE_TITLES[r]}: <b>${acc.by_route[r] || 0}</b>`);
    $("pipeline-banner").innerHTML =
      `<b>${ok ? "✅ Все письма учтены" : "❌ Расхождение"}:</b> всего <b>${acc.total_raw}</b> = ` +
      parts.join(" · ") + (ok ? "" : ` · неучтённых ${acc.unaccounted}`);
    if (!_pipelineRoute || !(acc.by_route[_pipelineRoute] != null)) {
      _pipelineRoute = PIPELINE_ORDER.find(r => (acc.by_route[r] || 0) > 0) || PIPELINE_ORDER[0];
      _pipelineReason = null;
    }
    $("pipeline-routes").innerHTML = PIPELINE_ORDER.map(r => {
      const active = r === _pipelineRoute;
      return `<button class="btn-sm ${active ? "success" : ""}" onclick="selectPipelineRoute('${r}')">` +
             `${PIPELINE_ROUTE_TITLES[r]} <b>${acc.by_route[r] || 0}</b></button>`;
    }).join("");
    renderPipelineReasons();
    loadPipelineItems();
  } catch (e) { console.warn("loadPipeline error:", e); }
}

function renderPipelineReasons() {
  const box = $("pipeline-reasons"); if (!box) return;
  const reasons = (_pipelineAcc?.reason_in_route || {})[_pipelineRoute] || {};
  const keys = Object.keys(reasons);
  if (!keys.length) { box.innerHTML = ""; return; }
  const total = _pipelineAcc.by_route[_pipelineRoute] || 0;
  let html = `<button class="btn-sm ${!_pipelineReason ? "success" : ""}" onclick="selectPipelineReason(null)">Все <b>${total}</b></button>`;
  html += keys.map(k => `<button class="btn-sm ${_pipelineReason === k ? "success" : ""}" ` +
    `onclick="selectPipelineReason('${k}')">${k} <b>${reasons[k]}</b></button>`).join("");
  box.innerHTML = html;
}

function selectPipelineRoute(r) {
  _pipelineRoute = r; _pipelineReason = null; _pipelinePage = 1;
  $("pipeline-trace").style.display = "none";
  renderPipelineReasons(); loadPipelineItems();
  // подсветка активной route-кнопки без полной перезагрузки
  document.querySelectorAll("#pipeline-routes .btn-sm").forEach(b => b.classList.remove("success"));
}
function selectPipelineReason(rg) { _pipelineReason = rg; _pipelinePage = 1; renderPipelineReasons(); loadPipelineItems(); }

async function loadPipelineItems() {
  try {
    const q = $("pipeline-search")?.value || "";
    const p = new URLSearchParams({ route: _pipelineRoute || "", page: _pipelinePage, page_size: 50, q });
    if (_pipelineReason) p.set("reason", _pipelineReason);
    const res = await api("/api/pipeline/items?" + p.toString());
    const list = $("pipeline-list");
    if (!res || !res.ok) { list.innerHTML = "<div class='hint-text'>Ошибка</div>"; return; }
    if (!res.items.length) { list.innerHTML = "<div class='hint-text'>Писем нет</div>"; }
    else list.innerHTML = res.items.map(it => `
      <div class="loose-card" style="display:flex;flex-direction:column;gap:2px;padding:8px;border-bottom:1px solid var(--border,#3333)">
        <div style="display:flex;justify-content:space-between;gap:8px">
          <b style="flex:1">${esc(it.subject || "(без темы)")}</b>
          <span>
            <button class="btn-sm" onclick="openPipelineTrace('${it.case_id ? "case" : "raw"}',${it.case_id || it.raw_email_id})">Decision</button>
            ${it.parent_case_id ? `<button class="btn-sm" onclick="openPipelineTrace('case',${it.parent_case_id})">Родитель</button>` : ""}
          </span>
        </div>
        <div style="font-size:12px;color:var(--text-muted,#999)">
          ${esc(it.from_addr || "")} · ${esc(it.buyer_code || "")} · ${esc(it.reason_label || it.reason_group || "")}
          ${it.link_type ? " · 🔗 " + esc(it.link_type) : ""}${it.priority_flag ? " · ⏰" : ""}
        </div>
        <div style="font-size:12px">
          ${it.document_number ? "📄 " + esc(it.document_number) : ""}${it.document_date ? " от " + esc(it.document_date) : ""}
          ${it.part_number ? " · арт " + esc(it.part_number) : ""}
          ${(it.missing_fields && it.missing_fields.length) ? " · ⚠ нет: " + esc(it.missing_fields.join(",")) : ""}
          ${(it.weak_fields && it.weak_fields.length) ? " · слабо: " + esc(it.weak_fields.join(",")) : ""}
        </div>
        <div style="font-size:12px;color:var(--accent,#6cf)">→ ${esc(it.next_action || "")} <span style="color:var(--text-muted,#888)">(${esc(it.routing_reason || "")})</span></div>
      </div>`).join("");
    const pages = Math.max(1, Math.ceil(res.total_count / res.page_size));
    const prev = res.page > 1 ? `<button class="btn-sm" onclick="_pipelinePage=${res.page-1};loadPipelineItems()">←</button>` : "";
    const next = res.has_more ? `<button class="btn-sm" onclick="_pipelinePage=${res.page+1};loadPipelineItems()">→</button>` : "";
    $("pipeline-pagination").innerHTML = `${prev} Показано ${res.shown_from}–${res.shown_to} из ${res.total_count} (стр. ${res.page}/${pages}) ${next}`;
  } catch (e) { console.warn("loadPipelineItems error:", e); }
}

async function openPipelineTrace(target, id) {
  const box = $("pipeline-trace"); box.style.display = "block"; box.innerHTML = "Загрузка…";
  const url = target === "case" ? `/api/cases/${id}/decision` : `/api/raw-emails/${id}/decision`;
  const d = await api(url);
  if (!d || !d.ok) { box.innerHTML = "Трассировка недоступна"; return; }
  const sr = d.safety_router_result || {};
  box.innerHTML =
    `<b>DECISION · ${target} ${id}</b> <button class="btn-sm" style="float:right" onclick="$('pipeline-trace').style.display='none'">✕</button><br>` +
    `<b>route/bucket:</b> ${esc(d.visible_bucket || "")} · <b>subcat:</b> ${esc(d.subcategory || "")}<br>` +
    `<b>почему:</b> ${esc(d.explanation_short || sr.routing_reason || "")}<br>` +
    `<b>next_action:</b> ${esc(d.next_action || sr.next_action || "")}<br>` +
    `<b>evidence:</b> ${esc(sr.evidence_strength || "")} · <b>conflicts:</b> ${esc((d.conflicts || []).join(", ") || "—")}`;
}

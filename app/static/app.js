/* 陶瓷合窑装载确认台 — 原生 JS */
"use strict";

const state = {
  kilns: [],
  batches: [],
  drafts: [],
  currentDraft: null,   // GET /drafts/:id 的完整数据
  preview: null,        // 最近一次预览结果
  loads: [],
  openLoadId: null,
  schedules: [],
  editingLoadId: null,  // 正在修改的已安排窑次；null = 新安排
};

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// ---------------- API ----------------

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* 非 JSON */ }
  if (!res.ok) {
    const e = new Error((data && data.error) || `请求失败 (${res.status})`);
    e.status = res.status;
    e.data = data || {};
    throw e;
  }
  return data;
}

// ---------------- Toast ----------------

let toastTimer = null;
function toast(msg, kind = "info") {
  const el = $("toast");
  el.textContent = msg;
  el.className = `toast ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 4000);
}

// ---------------- 格式化 ----------------

const fmtPeak = (r) => `${r.min ?? r.peak_min} ~ ${r.max ?? r.peak_max} °C`;
const fmtHold = (r) => `${r.min ?? r.hold_min} ~ ${r.max ?? r.hold_max} 分钟`;

// ---------------- 数据加载 ----------------

async function reloadBatches() {
  state.batches = await api("GET", "/api/batches");
  renderBatches();
  renderBatchOptions();
}

async function reloadKilns() {
  state.kilns = await api("GET", "/api/kilns");
  $("d-kiln").innerHTML = state.kilns
    .map((k) => `<option value="${k.id}">${esc(k.code)} · ${esc(k.name)}</option>`)
    .join("");
}

async function reloadDrafts(selectId) {
  state.drafts = await api("GET", "/api/drafts");
  renderDraftOptions(selectId);
}

async function reloadLoads() {
  state.loads = await api("GET", "/api/loads");
  renderLoads();
  if ($("sc-load")) renderLoadScheduleOptions();
}

// ---------------- 批次 ----------------

function renderBatches() {
  const tb = $("batch-table").querySelector("tbody");
  if (!state.batches.length) {
    tb.innerHTML = `<tr><td colspan="6" style="color:#8a8175">暂无批次，请先登记</td></tr>`;
    return;
  }
  tb.innerHTML = state.batches.map((b) => {
    const used = b.used_by_load != null;
    return `<tr>
      <td>${esc(b.code)}</td>
      <td>${b.peak_min} ~ ${b.peak_max}</td>
      <td>${b.hold_min} ~ ${b.hold_max}</td>
      <td>v${b.version}</td>
      <td>${used
        ? `<span class="tag tag-used">已用于窑次 #${b.used_by_load}</span>`
        : `<span class="tag tag-open">待烧</span>`}</td>
      <td>
        <button data-edit="${b.id}">改工艺</button>
        <button data-del="${b.id}">删除</button>
      </td>
    </tr>`;
  }).join("");

  tb.querySelectorAll("[data-edit]").forEach((btn) =>
    btn.addEventListener("click", () => editBatch(Number(btn.dataset.edit)))
  );
  tb.querySelectorAll("[data-del]").forEach((btn) =>
    btn.addEventListener("click", () => deleteBatch(Number(btn.dataset.del)))
  );
}

async function editBatch(id) {
  const b = state.batches.find((x) => x.id === id);
  const peakMin = prompt(`修改批次 ${b.code} 的峰温下限(°C)`, b.peak_min);
  if (peakMin === null) return;
  const peakMax = prompt(`修改批次 ${b.code} 的峰温上限(°C)`, b.peak_max);
  if (peakMax === null) return;
  const holdMin = prompt(`修改批次 ${b.code} 的保温下限(分钟)`, b.hold_min);
  if (holdMin === null) return;
  const holdMax = prompt(`修改批次 ${b.code} 的保温上限(分钟)`, b.hold_max);
  if (holdMax === null) return;
  try {
    await api("PUT", `/api/batches/${id}`, {
      peak_min: Number(peakMin), peak_max: Number(peakMax),
      hold_min: parseInt(holdMin, 10), hold_max: parseInt(holdMax, 10),
      version: b.version,
    });
    toast(`批次 ${b.code} 工艺范围已更新（版本递增，旧预览将失效）`, "ok");
    await reloadBatches();
    await refreshCurrentDraft();
  } catch (e) { toast(e.message, "err"); }
}

async function deleteBatch(id) {
  const b = state.batches.find((x) => x.id === id);
  if (!confirm(`确认删除未入窑的批次 ${b.code}？`)) return;
  try {
    await api("DELETE", `/api/batches/${id}`);
    toast("批次已删除", "ok");
    await reloadBatches();
    await refreshCurrentDraft();
  } catch (e) { toast(e.message, "err"); }
}

$("batch-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const b = await api("POST", "/api/batches", {
      code: $("b-code").value.trim(),
      peak_min: Number($("b-peak-min").value),
      peak_max: Number($("b-peak-max").value),
      hold_min: parseInt($("b-hold-min").value, 10),
      hold_max: parseInt($("b-hold-max").value, 10),
    });
    toast(`批次 ${b.code} 已登记`, "ok");
    ev.target.reset();
    await reloadBatches();
  } catch (e) { toast(e.message, "err"); }
});

// ---------------- 草案 ----------------

function renderDraftOptions(selectId) {
  const sel = $("draft-select");
  sel.innerHTML = state.drafts
    .map((d) => `<option value="${d.id}">${esc(d.name)}（${esc(d.kiln_code)}，${d.item_count} 批，v${d.version}，${d.status === "open" ? "草案" : "已确认"}）</option>`)
    .join("");
  if (state.drafts.length) {
    sel.value = String(selectId || state.currentDraft?.id || state.drafts[0].id);
    loadDraft(Number(sel.value));
  } else {
    state.currentDraft = null;
    $("draft-panel").classList.add("hidden");
  }
}

async function loadDraft(id) {
  try {
    state.currentDraft = await api("GET", `/api/drafts/${id}`);
    state.preview = null;
    renderDraftPanel();
  } catch (e) { toast(e.message, "err"); }
}

async function refreshCurrentDraft() {
  if (!state.currentDraft) return;
  await reloadDrafts(state.currentDraft.id);
}

function currentKiln() {
  return state.kilns.find((k) => k.id === state.currentDraft.kiln_id);
}

function renderBatchOptions() {
  if (!state.currentDraft) return;
  const usedIds = new Set(state.currentDraft.items.map((i) => i.batch_id));
  const opts = state.batches
    .filter((b) => !usedIds.has(b.id))
    .map((b) => `<option value="${b.id}">${esc(b.code)}（峰 ${b.peak_min}~${b.peak_max}，保温 ${b.hold_min}~${b.hold_max}）</option>`)
    .join("");
  $("item-batch").innerHTML = opts || `<option value="">（所有批次已在草案中）</option>`;
}

function renderDraftPanel() {
  const d = state.currentDraft;
  const panel = $("draft-panel");
  panel.classList.remove("hidden");

  const confirmed = d.status !== "open";
  $("item-add").disabled = confirmed;
  $("item-batch").disabled = confirmed;
  $("item-pos").disabled = confirmed;
  $("btn-preview").disabled = confirmed;
  $("draft-delete").disabled = confirmed;

  // 窑位下拉 + 占位图
  const positions = currentKiln()?.positions || [];
  const occupied = new Map(d.items.map((i) => [i.pos_no, i.batch_code]));
  $("item-pos").innerHTML = positions
    .map((p) => `<option value="${p}">${p} 号位${occupied.has(p) ? `（现放 ${esc(occupied.get(p))}）` : ""}</option>`)
    .join("");

  $("position-map").innerHTML = positions
    .map((p) => occupied.has(p)
      ? `<div class="pos-cell busy">${p} 号位<br>${esc(occupied.get(p))}</div>`
      : `<div class="pos-cell free">${p} 号位<br>空</div>`)
    .join("");

  // 批次明细表
  const tb = $("item-table").querySelector("tbody");
  tb.innerHTML = d.items.length
    ? d.items.map((i) => `<tr>
        <td>${i.pos_no}</td>
        <td>${esc(i.batch_code)}</td>
        <td>${i.peak_min} ~ ${i.peak_max}</td>
        <td>${i.hold_min} ~ ${i.hold_max}</td>
        <td>v${i.batch_version}</td>
        <td>${confirmed ? "" : `<button data-rm="${i.batch_id}">移出</button>`}</td>
      </tr>`).join("")
    : `<tr><td colspan="6" style="color:#8a8175">草案为空，请放入批次</td></tr>`;
  tb.querySelectorAll("[data-rm]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      try {
        await api("DELETE", `/api/drafts/${d.id}/items/${btn.dataset.rm}`);
        await reloadDrafts(d.id);
      } catch (e) { toast(e.message, "err"); }
    })
  );

  renderBatchOptions();
  renderPreview();
}

function renderPreview() {
  const box = $("preview-box");
  const btn = $("btn-confirm");
  const p = state.preview;
  if (!p) {
    box.classList.add("hidden");
    btn.disabled = true;
    return;
  }
  const busy = new Map((p.items || []).map((i) => [i.pos_no, i.batch_code]));
  // 占位图叠加冲突高亮
  document.querySelectorAll("#position-map .pos-cell").forEach((cell, idx) => {
    const posNo = (p.positions || [])[idx];
    cell.classList.remove("conflict");
    if (p.conflicts.some((c) => c.type === "dup_position" && c.pos_no === posNo)) {
      cell.classList.add("conflict");
    }
  });

  const warnings = p.warnings || [];
  box.className = `preview ${p.ready ? "ok" : "bad"}`;
  box.innerHTML = `
    <div class="kv">草案版本：<b>v${p.draft_version}</b>（确认时将携带此版本及各批次版本）</div>
    <div class="kv">共同可用峰温：${p.common_peak
      ? `<span class="range">${fmtPeak(p.common_peak)}</span>`
      : `<span class="range">无交集 ✗</span>`}</div>
    <div class="kv">共同可用保温：${p.common_hold
      ? `<span class="range">${fmtHold(p.common_hold)}</span>`
      : `<span class="range">无交集 ✗</span>`}</div>
    <div class="kv">占位：${Object.keys(p.occupied).length} / ${p.positions.length} 位</div>
    ${warnings.length ? `<ul>${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
    <div class="kv" style="margin-top:6px">${p.ready
      ? "✅ 检查通过，可以确认"
      : "❌ 存在冲突，无法确认"}</div>`;
  box.classList.remove("hidden");
  btn.disabled = !p.ready || state.currentDraft.status !== "open";
}

$("draft-create").addEventListener("click", async () => {
  try {
    const d = await api("POST", "/api/drafts", {
      name: $("d-name").value.trim() || `草案 ${new Date().toLocaleString()}`,
      kiln_id: Number($("d-kiln").value),
    });
    $("d-name").value = "";
    toast("草案已创建", "ok");
    await reloadDrafts(d.id);
  } catch (e) { toast(e.message, "err"); }
});

$("draft-select").addEventListener("change", () =>
  loadDraft(Number($("draft-select").value))
);

$("draft-delete").addEventListener("click", async () => {
  if (!state.currentDraft || !confirm("删除此草案？已确认草案不可删除。")) return;
  try {
    await api("DELETE", `/api/drafts/${state.currentDraft.id}`);
    toast("草案已删除", "ok");
    state.currentDraft = null;
    await Promise.all([reloadDrafts(), reloadBatches()]);
  } catch (e) { toast(e.message, "err"); }
});

$("item-add").addEventListener("click", async () => {
  const d = state.currentDraft;
  const batchId = Number($("item-batch").value);
  const posNo = Number($("item-pos").value);
  if (!batchId) { toast("没有可放入的批次", "err"); return; }
  try {
    await api("PUT", `/api/drafts/${d.id}/items`, { batch_id: batchId, pos_no: posNo });
    toast(`已放入 ${posNo} 号位`, "ok");
    await reloadDrafts(d.id);
  } catch (e) { toast(e.message, "err"); }
});

$("btn-preview").addEventListener("click", async () => {
  try {
    state.preview = await api("GET", `/api/drafts/${state.currentDraft.id}/preview`);
    renderPreview();
    if (state.preview.ready) toast("预览通过，可确认", "ok");
    else toast("预览发现冲突，请查看明细", "err");
  } catch (e) {
    state.preview = null;
    renderPreview();
    toast(e.message, "err");
  }
});

// ---------------- 确认 ----------------

$("btn-confirm").addEventListener("click", async () => {
  const p = state.preview;
  if (!p || !p.ready) return;
  if (!confirm("确认后将生成不可变装载清单，批次即被占用且不能修改草案。继续？")) return;

  // 严格携带“预览时”的草案版本与批次版本/位置快照
  const payload = {
    draft_version: p.draft_version,
    items: p.items.map((i) => ({
      batch_id: i.batch_id,
      batch_version: i.batch_version,
      pos_no: i.pos_no,
    })),
  };
  try {
    const load = await api("POST", `/api/drafts/${p.draft_id}/confirm`, payload);
    toast(`窑次 #${load.id} 已确认并保存不可变清单`, "ok");
    state.preview = null;
    await Promise.all([reloadDrafts(p.draft_id), reloadBatches(), reloadLoads()]);
    state.openLoadId = load.id;
    renderLoads();
  } catch (e) {
    if (e.data && e.data.stale) {
      toast(`${e.message} —— 请按下方最新草案重新预览`, "err");
      state.preview = null;
      await reloadDrafts(p.draft_id);
    } else {
      toast(e.message, "err");
      await refreshCurrentDraft();
    }
  }
});

// ---------------- 已确认窑次 ----------------

function renderLoads() {
  const box = $("load-list");
  if (!state.loads.length) {
    box.innerHTML = `<p style="color:#8a8175;font-size:13px">尚无已确认窑次</p>`;
    return;
  }
  box.innerHTML = state.loads.map((l) => `
    <div class="load-item" data-load="${l.id}">
      <div class="title">窑次 #${l.id} · ${esc(l.name)}（${esc(l.kiln_code)}）</div>
      <div class="meta">
        ${l.item_count} 批 · 共同峰温 ${l.peak_min} ~ ${l.peak_max} °C ·
        保温 ${l.hold_min} ~ ${l.hold_max} 分钟
      </div>
      <div class="meta">确认于 ${new Date(l.confirmed_at).toLocaleString()}</div>
      <div class="load-detail hidden" id="load-detail-${l.id}"></div>
    </div>`).join("");

  box.querySelectorAll(".load-item").forEach((el) =>
    el.addEventListener("click", async () => {
      const id = Number(el.dataset.load);
      const detail = $(`load-detail-${id}`);
      if (!detail.classList.contains("hidden")) { detail.classList.add("hidden"); return; }
      try {
        const full = await api("GET", `/api/loads/${id}`);
        detail.innerHTML = `<table>
          <thead><tr><th>窑位</th><th>批次(快照)</th><th>峰温</th><th>保温(min)</th></tr></thead>
          <tbody>${full.items.map((i) => `<tr>
            <td>${i.pos_no}</td><td>${esc(i.batch_code)}</td>
            <td>${i.peak_min} ~ ${i.peak_max}</td>
            <td>${i.hold_min} ~ ${i.hold_max}</td></tr>`).join("")}
          </tbody></table>
          <div class="meta">共同工艺区间为确认当时快照，之后批次修改不影响本清单。</div>`;
        detail.classList.remove("hidden");
      } catch (e) { toast(e.message, "err"); }
    })
  );

  if (state.openLoadId) {
    const d = $(`load-detail-${state.openLoadId}`);
    if (d) d.parentElement.click();
    state.openLoadId = null;
  }
}

$("loads-refresh").addEventListener("click", reloadLoads);

// ---------------- 烧成时段安排 ----------------

const SCHED_TZ_OPTIONS = [
  ["+08:00", "UTC+08:00（北京）"],
  ["+09:00", "UTC+09:00（东京）"],
  ["+07:00", "UTC+07:00（曼谷）"],
  ["+05:30", "UTC+05:30（印度）"],
  ["+00:00", "UTC+00:00（伦敦）"],
  ["-05:00", "UTC-05:00（纽约）"],
  ["-08:00", "UTC-08:00（旧金山）"],
];

function buildTzOptions(sel) {
  sel.innerHTML = SCHED_TZ_OPTIONS
    .map(([v, label]) => `<option value="${v}">${label}</option>`)
    .join("");
  sel.value = "+08:00";
}

async function reloadSchedules() {
  state.schedules = await api("GET", "/api/schedules");
  renderScheduleBoard();
  renderLoadScheduleOptions();
  fillScheduleFormFromSelection();
}

function scheduleForLoad(loadId) {
  return state.schedules.find((s) => s.load_id === loadId) || null;
}

// 把带时区 ISO 8601 拆成本地墙钟值（datetime-local）与 UTC 偏移
function splitZonedIso(iso) {
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)(Z|[+-]\d{2}:?\d{2})$/.exec(iso.trim());
  if (!m) return null;
  let off = m[3] === "Z" ? "+00:00" : m[3];
  if (off.length === 5) off = `${off.slice(0, 3)}:${off.slice(3)}`;
  return { local: `${m[1]}T${m[2]}`, offset: off };
}

function buildZonedIso(localValue, offset) {
  // localValue: YYYY-MM-DDTHH:mm[:ss]；offset: +HH:MM
  return `${localValue}${offset === "+00:00" ? "Z" : offset}`;
}

function renderLoadScheduleOptions() {
  const sel = $("sc-load");
  if (!state.loads.length) {
    sel.innerHTML = `<option value="">（暂无已确认窑次）</option>`;
    return;
  }
  sel.innerHTML = state.loads.map((l) => {
    const sched = scheduleForLoad(l.id);
    const tag = sched ? "［已安排］" : "［未安排］";
    return `<option value="${l.id}">${tag} #${l.id} ${esc(l.name)}（${esc(l.kiln_code)}）</option>`;
  }).join("");
  // 保持正在修改的选择；否则选第一个未安排窑次
  const keep = state.editingLoadId;
  if (keep && state.loads.some((l) => l.id === keep)) {
    sel.value = String(keep);
  } else {
    const firstUnsched = state.loads.find((l) => !scheduleForLoad(l.id));
    sel.value = String(firstUnsched ? firstUnsched.id : state.loads[0].id);
  }
}

function fillScheduleFormFromSelection() {
  const loadId = Number($("sc-load").value);
  const load = state.loads.find((l) => l.id === loadId);
  const sched = loadId ? scheduleForLoad(loadId) : null;
  if (!load) {
    $("sc-kiln").value = "";
    $("sc-peak-range").value = "";
    $("sc-hold-range").value = "";
    return;
  }
  $("sc-kiln").value = load.kiln_code;
  $("sc-peak-range").value = `${load.peak_min} ~ ${load.peak_max} °C`;
  $("sc-hold-range").value = `${load.hold_min} ~ ${load.hold_max} min`;

  state.editingLoadId = sched ? load.id : null;
  if (sched) {
    const s = splitZonedIso(sched.start_at);
    const e = splitZonedIso(sched.end_at);
    if (s) { $("sc-start").value = s.local; $("sc-tz-start").value = s.offset; }
    if (e) { $("sc-end").value = e.local; $("sc-tz-end").value = e.offset; }
    $("sc-peak").value = sched.peak_temp;
    $("sc-hold").value = sched.hold_minutes;
  } else {
    $("sc-start").value = "";
    $("sc-end").value = "";
    $("sc-peak").value = "";
    $("sc-hold").value = "";
  }
  $("sc-submit").textContent = sched ? "保存修改（同一窑次）" : "保存安排";
  $("sc-cancel").classList.toggle("hidden", !sched);
  $("sc-hint").textContent = sched
    ? `正在修改窑次 #${load.id} 的既有安排；保存冲突时原安排保持不变。`
    : "新安排的工艺参数必须落在确认时共同区间内。";
}

$("sc-load").addEventListener("change", fillScheduleFormFromSelection);
$("sc-cancel").addEventListener("click", () => {
  state.editingLoadId = null;
  renderLoadScheduleOptions();
  fillScheduleFormFromSelection();
});

$("schedule-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const loadId = Number($("sc-load").value);
  if (!loadId) { toast("请先确认装窑生成窑次，再安排烧成时段", "err"); return; }
  const lv = $("sc-start").value;
  const evv = $("sc-end").value;
  if (!lv || !evv) { toast("请填写开始与结束时间", "err"); return; }
  const body = {
    start_at: buildZonedIso(lv, $("sc-tz-start").value),
    end_at: buildZonedIso(evv, $("sc-tz-end").value),
    peak_temp: Number($("sc-peak").value),
    hold_minutes: parseInt($("sc-hold").value, 10),
  };
  try {
    const saved = await api("PUT", `/api/schedules/loads/${loadId}`, body);
    toast(saved.created
      ? `窑次 #${loadId} 烧成时段已安排`
      : `窑次 #${loadId} 烧成安排已更新`, "ok");
    state.editingLoadId = null;
    await reloadSchedules();
    await reloadLoads();
    renderLoadScheduleOptions();
    // 保存后停留在该窑次（展示最新值）
    $("sc-load").value = String(loadId);
    fillScheduleFormFromSelection();
  } catch (e) {
    // 冲突/参数越界/时间非法：后端未写入，表单保留原值，刷新出最新安排供核对
    toast(e.message, "err");
    try {
      await reloadSchedules();
    } catch (_) { /* ignore */ }
  }
});

$("sc-refresh").addEventListener("click", async () => {
  try {
    await Promise.all([reloadLoads(), reloadSchedules()]);
    toast("烧成安排已刷新", "ok");
  } catch (e) { toast(e.message, "err"); }
});

function fmtZoned(iso) {
  const parts = splitZonedIso(iso);
  if (!parts) return esc(iso);
  const off = parts.offset;
  return `${parts.local.replace("T", " ").slice(0, 19)} <span class="s-utc">${off === "+00:00" ? "Z" : off}</span>`;
}

function renderScheduleBoard() {
  const board = $("schedule-board");
  if (!state.kilns.length) { board.innerHTML = ""; return; }
  board.innerHTML = state.kilns.map((k) => {
    const items = state.schedules
      .filter((s) => s.kiln_id === k.id)
      .sort((a, b) => a.start_utc < b.start_utc ? -1 : a.start_utc > b.start_utc ? 1 : a.id - b.id);
    return `<div class="kiln-col">
      <h3>${esc(k.code)} · ${esc(k.name)}</h3>
      ${items.length ? items.map((s) => `
        <div class="sched-item">
          <div class="s-title">窑次 #${s.load_id} · ${esc(s.load_name)}</div>
          <div class="s-meta">开始：${fmtZoned(s.start_at)}</div>
          <div class="s-meta">结束：${fmtZoned(s.end_at)}</div>
          <div class="s-meta">峰温 <b>${s.peak_temp} °C</b>（允许 ${s.allowed_peak_min}~${s.allowed_peak_max}）·
            保温 <b>${s.hold_minutes} min</b>（允许 ${s.allowed_hold_min}~${s.allowed_hold_max}）</div>
          <div class="s-utc">UTC ${s.start_utc} ~ ${s.end_utc}；更新于 ${new Date(s.updated_at).toLocaleString()}</div>
          <button data-edit-sched="${s.load_id}">修改时段/参数</button>
        </div>`).join("")
        : `<div class="sched-empty">暂无安排（可与其他窑同时烧成）</div>`}
    </div>`;
  }).join("");

  board.querySelectorAll("[data-edit-sched]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const loadId = Number(btn.dataset.editSched);
      state.editingLoadId = loadId;
      renderLoadScheduleOptions();
      $("sc-load").value = String(loadId);
      fillScheduleFormFromSelection();
      $("schedule-form").scrollIntoView({ behavior: "smooth", block: "center" });
    })
  );
}

// ---------------- 启动 ----------------

(async function init() {
  try {
    buildTzOptions($("sc-tz-start"));
    buildTzOptions($("sc-tz-end"));
    await Promise.all([reloadKilns(), reloadBatches(), reloadLoads(), reloadSchedules()]);
    await reloadDrafts();
  } catch (e) {
    toast("初始化失败：" + e.message, "err");
  }
})();

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

// ---------------- 启动 ----------------

(async function init() {
  try {
    await Promise.all([reloadKilns(), reloadBatches(), reloadLoads()]);
    await reloadDrafts();
  } catch (e) {
    toast("初始化失败：" + e.message, "err");
  }
})();

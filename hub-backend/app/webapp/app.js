/* Nekowork web client — vanilla JS, polling MVP. */
"use strict";
const $ = (s) => document.querySelector(s);
const TOKEN_KEY = "nw.token";
const USER_KEY = "nw.user";
let token = localStorage.getItem(TOKEN_KEY) || "";
let me = null;
try { me = JSON.parse(localStorage.getItem(USER_KEY) || "null"); } catch {}
let chats = [];
let active = null;          // {handoffId, taskId, ref, since}
let pollTimer = null;

function api(path, opts = {}) {
  opts.headers = Object.assign({}, opts.headers,
    token ? { "Authorization": "Device " + token } : {});
  return fetch(path, opts).then(async (r) => {
    if (r.status === 401 && token) { logout(); throw new Error("Sesi habis, masuk lagi"); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || data.error || ("HTTP " + r.status));
    return data;
  });
}
function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}
function esc(s) {
  const d = document.createElement("div");
  d.textContent = String(s ?? "");
  return d.innerHTML.replace(/"/g, "&quot;");
}

/* ---------- Auth ---------- */
function showLogin() {
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  clearInterval(pollTimer);
}
function showApp() {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#who").textContent = (me && me.displayName) || "…";
  loadChats();
  pollTimer = setInterval(() => { if (active) refreshChat(); }, 2500);
}
function logout() {
  token = ""; me = null;
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  active = null;
  showLogin();
}
$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("#login-btn");
  btn.disabled = true;
  $("#login-err").classList.add("hidden");
  try {
    const r = await api("/api/v1/web/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account: $("#f-account").value.trim(), password: $("#f-password").value }),
    });
    token = r.token;
    me = r.user;
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(USER_KEY, JSON.stringify(me));
    showApp();
  } catch (e) {
    $("#login-err").textContent = e.message;
    $("#login-err").classList.remove("hidden");
  } finally {
    btn.disabled = false;
  }
});
$("#logout").addEventListener("click", logout);

/* ---------- Chat list ---------- */
const ST_LABEL = { running: "jalan", queued: "antre", done: "selesai", failed: "gagal",
  stopped: "dihentikan", dispatching: "mengirim", starting: "menyiapkan" };
function statusOf(item) {
  const st = (item.execution && item.execution.status) || "";
  return ST_LABEL[st] || st || "baru";
}
async function loadChats() {
  try {
    const r = await api("/api/v1/cloud-execution-sync");
    chats = (r.items || []).map((x) => ({
      id: x.id, title: x.title || "Tanpa judul", createdAt: x.createdAt,
      status: (x.execution && x.execution.status) || "", item: x,
    }));
    const list = $("#chat-list");
    list.innerHTML = chats.map((c) =>
      `<div class="chat-item${active && active.handoffId === c.id ? " active" : ""}" data-id="${esc(c.id)}">` +
      `${esc(c.title)}<span class="st muted">· ${esc(statusOf(c.item))}</span></div>`).join("")
      || `<div class="muted small" style="padding:8px 10px">Belum ada chat.</div>`;
    list.querySelectorAll(".chat-item").forEach((el) =>
      el.addEventListener("click", () => openChat(el.dataset.id)));
  } catch (e) { toast(e.message); }
}

/* ---------- Chat view ---------- */
function renderMessages(conversation, items) {
  const box = $("#messages");
  if (!items.length) {
    box.innerHTML = `<div class="empty muted">Belum ada pesan.</div>`;
    return;
  }
  box.innerHTML = items.map((it) => {
    const k = it.kind || "";
    if (k === "user") return `<div class="msg user"><div class="bubble">${esc(it.text)}</div></div>`;
    if (k === "tool" || k === "toolUse" || k === "tool_result") {
      const label = it.tool || k;
      return `<div class="tool" title="${esc(it.text)}">🔧 ${esc(label)} — ${esc((it.text || "").slice(0, 120))}</div>`;
    }
    if (k === "error" || it.isError) {
      return `<div class="msg assistant err-text"><div class="bubble">${esc(it.text || "Terjadi kesalahan.")}</div></div>`;
    }
    return `<div class="msg assistant"><div class="bubble">${esc(it.text || it.full || "")}</div></div>`;
  }).join("");
  box.scrollTop = box.scrollHeight;
}
function renderPermission(conversation) {
  const exec = conversation.execution || {};
  const pending = (exec.pendingPermissions || []);
  const banner = $("#perm-banner");
  const running = ["running", "queued", "dispatching", "starting"].includes(exec.status || "");
  $("#stop").classList.toggle("hidden", !running);
  if (!pending.length) { banner.classList.add("hidden"); banner.innerHTML = ""; return; }
  const p = pending[0];
  const opts = (p.options && p.options.length)
    ? `<div class="opts">${p.options.map((o) =>
        `<button data-opt="${esc(o.id || o.optionId || o)}">${esc(o.label || o.title || o.name || o)}</button>`).join("")}</div>`
    : "";
  banner.innerHTML =
    `<div class="pt">Butuh izin: ${esc(p.title || p.action || p.tool || "aksi")}</div>` +
    (p.target ? `<div class="pd">${esc(p.target)}</div>` : "") +
    `<div class="opts">` +
    `<button data-dec="once">Izinkan sekali</button>` +
    (p.allowSession ? `<button data-dec="session">Izinkan sesi ini</button>` : "") +
    `<button data-dec="deny" class="danger">Tolak</button></div>` + opts;
  banner.classList.remove("hidden");
  banner.querySelectorAll("[data-dec]").forEach((b) => b.addEventListener("click", () =>
    answerPermission(p, b.dataset.dec, null)));
  banner.querySelectorAll("[data-opt]").forEach((b) => b.addEventListener("click", () =>
    answerPermission(p, "answer", b.dataset.opt)));
}
async function answerPermission(p, decision, optionId) {
  try {
    await api(`/api/v1/web/tasks/${active.taskId}/permission`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ requestId: p.requestId, decision,
        policyRevision: p.policyRevision, ...(optionId ? { optionId } : {}) }),
    });
    toast(decision === "deny" ? "Ditolak." : "Diizinkan.");
  } catch (e) { toast("Gagal: " + e.message); }
  refreshChat();
}
let msgs = [];
async function refreshChat() {
  if (!active) return;
  try {
    const r = await api(`/api/v1/cloud-execution-sync/${active.handoffId}?since=${active.since || 0}`);
    const c = r.conversation || {};
    const exec = c.execution || {};
    if (exec.taskId) active.taskId = exec.taskId;
    if (exec.ref) active.ref = exec.ref;
    if (r.page && r.page.last) active.since = r.page.last;
    $("#chat-title").textContent = c.title || "Chat";
    const mode = (exec.accessControl && exec.accessControl.mode) || "standard";
    const st = ST_LABEL[exec.status] || exec.status || "baru";
    $("#chat-meta").textContent = `${st} · mode ${mode}`;
    const items = (r.page && r.page.items) || [];
    if (items.length) msgs = active.loadedFull ? msgs.concat(items) : items;
    active.loadedFull = true;
    renderMessages(c, msgs);
    renderPermission(c);
  } catch (e) {
    if (String(e.message).includes("409")) { toast("Eksekusi berpindah, muat ulang."); active.since = 0; active.loadedFull = false; msgs = []; }
  }
}
async function openChat(id) {
  active = { handoffId: id, taskId: null, ref: "", since: 0, loadedFull: false };
  msgs = [];
  $("#messages").innerHTML = `<div class="empty muted">Memuat…</div>`;
  loadChats();
  await refreshChat();
}

/* ---------- Composer ---------- */
$("#composer").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = $("#input");
  const text = input.value.trim();
  if (!text) return;
  if (!active || !active.taskId) { toast("Buka chat dulu."); return; }
  input.value = "";
  try {
    await api(`/api/v1/tasks/${active.taskId}/ai-say`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, messageId: crypto.randomUUID() }),
    });
  } catch (e) { input.value = text; toast("Gagal kirim: " + e.message); }
  refreshChat();
});
$("#stop").addEventListener("click", async () => {
  if (!active || !active.taskId) return;
  try {
    await api(`/api/v1/tasks/${active.taskId}/ai-stop`, { method: "POST" });
    toast("Perintah stop dikirim.");
  } catch (e) { toast("Gagal: " + e.message); }
  refreshChat();
});

/* ---------- New chat dialog ---------- */
const dlg = $("#dlg");
$("#new-chat").addEventListener("click", async () => {
  $("#dlg-err").classList.add("hidden");
  try {
    const r = await api("/api/v1/cloud-environments");
    const envs = (r.environments || r.items || []).filter((e) => e.eligible && e.online);
    const sel = $("#d-runner");
    sel.innerHTML = envs.length
      ? envs.map((e) => `<option value="${esc(e.id)}">${esc(e.name)} (${esc(e.statusText)})</option>`).join("")
      : `<option value="">PC desktop tidak online</option>`;
    sel.onchange = () => fillAgents(envs);
    fillAgents(envs);
    dlg.showModal();
    if (!envs.length) {
      $("#dlg-err").textContent = "Tidak ada PC yang online. Nyalakan desktop client di rumah dulu.";
      $("#dlg-err").classList.remove("hidden");
    }
  } catch (e) { toast(e.message); }
});
function fillAgents(envs) {
  const env = envs.find((e) => e.id === $("#d-runner").value);
  const agents = (env && env.agents) || [];
  $("#d-agent").innerHTML = agents.map((a) =>
    `<option value="${esc(a.id)}"${a.ready ? "" : " disabled"}>${esc(a.name || a.id)}</option>`).join("")
    || `<option value="">(tidak ada agent)</option>`;
}
$("#dlg-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (ev.submitter && ev.submitter.value === "cancel") return;
  const runnerId = $("#d-runner").value;
  const agentId = $("#d-agent").value;
  const model = $("#d-model").value.trim();
  const text = $("#input").value.trim() || "Halo";
  if (!runnerId || !agentId) {
    $("#dlg-err").textContent = "Pilih PC dan agent dulu.";
    $("#dlg-err").classList.remove("hidden");
    return;
  }
  $("#dlg-err").classList.add("hidden");
  try {
    const options = {
      sessionId: crypto.randomUUID(),
      agentId, model,
      title: text.slice(0, 60),
      text,
      targetRunnerId: runnerId,
      targetRunnerKind: "work-computer",
      requiredSkills: [],
    };
    const fd = new FormData();
    fd.append("options", JSON.stringify(options));
    const r = await api(`/api/v1/cloud-conversations/${options.sessionId}`,
      { method: "POST", body: fd });
    dlg.close();
    $("#input").value = "";
    await loadChats();
    const match = chats.find((c) => ((c.item.execution || {}).taskId) === r.taskId);
    if (match) await openChat(match.id);
    else if (chats.length) await openChat(chats[0].id);
    toast("Chat dibuat. PC lokal mulai mengeksekusi.");
  } catch (e) {
    $("#dlg-err").textContent = e.message;
    $("#dlg-err").classList.remove("hidden");
  }
});

/* ---------- Boot ---------- */
if (token) showApp(); else showLogin();

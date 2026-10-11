const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const state = { tab: "rooms", recPage: 0, recLimit: 30, recTotal: 0 };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function splitSummary(mode, arg) {
  if (mode === "time") return `每 ${arg} 分钟切一次`;
  if (mode === "size") return `每 ${arg} MB 切一次`;
  return "不分段";
}

function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("err", isErr);
  el.classList.remove("hidden");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 3000);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let body = null;
  try { body = await res.json(); } catch { /* ignore */ }
  if (!res.ok) {
    const detail = body && (body.detail || body.message);
    throw new Error(detail || `请求失败 ${res.status}`);
  }
  return body;
}

function fmtSize(bytes) {
  if (!bytes) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = bytes;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}

function fmtDuration(sec) {
  if (sec == null) return "-";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return (h ? h + ":" : "") + String(m).padStart(h ? 2 : 1, "0") + ":" + String(s).padStart(2, "0");
}

function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ---------- tabs ----------
$$(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.tab = btn.dataset.tab;
    $$(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    $("#page-rooms").classList.toggle("hidden", state.tab !== "rooms");
    $("#page-recordings").classList.toggle("hidden", state.tab !== "recordings");
    if (state.tab === "recordings") loadRecordings();
  });
});

// ---------- rooms ----------
const QUALITY_NAMES = { 0: "自动", 20000: "4K", 10000: "原画", 401: "蓝光杜比", 400: "蓝光", 250: "超清", 150: "高清", 75: "流畅" };

async function loadRooms() {
  let rooms = [];
  try { rooms = await api("/api/rooms"); } catch (e) { toast(e.message, true); return; }
  const tbody = $("#rooms-tbody");
  tbody.innerHTML = "";
  $("#rooms-empty").classList.toggle("hidden", rooms.length > 0);
  for (const r of rooms) {
    const tr = document.createElement("tr");
    let badge;
    if (r.runtime?.recording) badge = `<span class="badge recording">● 录制中 ${fmtDuration(r.runtime.rec_seconds)}</span>`;
    else if (r.runtime?.living) badge = `<span class="badge living">直播中</span>`;
    else if (r.runtime?.status === "unknown" || r.runtime?.status === "error") badge = `<span class="badge offline">获取失败</span>`;
    else badge = `<span class="badge offline">未开播</span>`;

    tr.innerHTML = `
      <td><a href="https://live.bilibili.com/${r.room_id}" target="_blank">${r.room_id}</a></td>
      <td class="title-cell" title="${esc(r.title || r.name)}">${esc(r.title || r.name) || "-"}</td>
      <td>${badge}</td>
      <td>${esc(QUALITY_NAMES[r.quality] ?? r.quality)}<div class="sub">${esc(splitSummary(r.split_mode, r.split_arg))}</div></td>
      <td><label class="switch"><input type="checkbox" data-act="danmaku" ${r.record_danmaku ? "checked" : ""}><span class="slider"></span></label></td>
      <td><label class="switch"><input type="checkbox" data-act="enabled" ${r.enabled ? "checked" : ""}><span class="slider"></span></label></td>
      <td class="ops"></td>`;

    const ops = tr.querySelector(".ops");
    const mkBtn = (text, cls, fn) => {
      const b = document.createElement("button");
      b.className = "btn small " + (cls || "");
      b.textContent = text;
      b.addEventListener("click", fn);
      ops.appendChild(b);
    };
    if (r.runtime?.recording) {
      mkBtn("停止录制", "danger", () => doRoomAction(r.room_id, "stop"));
    } else {
      mkBtn("立即录制", "", () => doRoomAction(r.room_id, "record"));
    }
    mkBtn("设置", "", () => openRoomModal(r));
    mkBtn("删除", "danger", async () => {
      if (!confirm(`删除房间 ${r.room_id} 的监控？（不会删除已录制文件）`)) return;
      try { await api(`/api/rooms/${r.room_id}`, { method: "DELETE" }); toast("已删除"); refresh(); }
      catch (e) { toast(e.message, true); }
    });

    tr.querySelectorAll("input[type=checkbox]").forEach((cb) => {
      cb.addEventListener("change", async () => {
        const field = cb.dataset.act === "enabled" ? "enabled" : "record_danmaku";
        try {
          await api(`/api/rooms/${r.room_id}`, {
            method: "PATCH",
            body: JSON.stringify({ [field]: cb.checked ? 1 : 0 }),
          });
          refresh();
        } catch (e) { cb.checked = !cb.checked; toast(e.message, true); }
      });
    });
    tbody.appendChild(tr);
  }
}

async function doRoomAction(roomId, action) {
  try {
    await api(`/api/rooms/${roomId}/${action}`, { method: "POST" });
    toast(action === "record" ? "已开始录制" : "已停止录制");
    refresh();
  } catch (e) { toast(e.message, true); }
}

$("#btn-add-room").addEventListener("click", async () => {
  const input = $("#new-room-input");
  const val = input.value.trim();
  const msgEl = $("#add-room-msg");
  if (!val) { msgEl.textContent = "请输入房间号"; msgEl.className = "msg err"; return; }
  msgEl.textContent = "添加中…"; msgEl.className = "msg";
  try {
    const r = await api("/api/rooms", {
      method: "POST",
      body: JSON.stringify({ room_id: val, quality: Number($("#new-room-quality").value) }),
    });
    msgEl.textContent = `已添加：${r.title || r.room_id}`; msgEl.className = "msg ok";
    input.value = "";
    refresh();
  } catch (e) {
    msgEl.textContent = e.message; msgEl.className = "msg err";
  }
});

// ---------- recordings ----------
async function loadRecordings() {
  const offset = state.recPage * state.recLimit;
  let data;
  try {
    data = await api(`/api/recordings?limit=${state.recLimit}&offset=${offset}`);
  } catch (e) { toast(e.message, true); return; }
  state.recTotal = data.total;
  const tbody = $("#recordings-tbody");
  tbody.innerHTML = "";
  $("#recordings-empty").classList.toggle("hidden", data.items.length > 0);
  const statusNames = {
    recording: '<span class="badge recording">录制中</span>',
    converting: '<span class="badge converting">转封装中</span>',
    completed: '<span class="badge done">已完成</span>',
    failed: '<span class="badge failed">失败</span>',
  };
  for (const rec of data.items) {
    const dur = rec.end_ts ? rec.end_ts - rec.start_ts : null;
    const links = [];
    if (rec.video_url) links.push(`<a href="/files/${rec.video_url}" download>视频</a>`);
    if (rec.danmaku_url) links.push(`<a href="/files/${rec.danmaku_url}" download>弹幕XML</a>`);
    if (rec.events_url) links.push(`<a href="/files/${rec.events_url}" download>事件</a>`);
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${rec.id}</td>
      <td><a href="https://live.bilibili.com/${rec.room_id}" target="_blank">${rec.room_id}</a></td>
      <td class="title-cell" title="${esc(rec.title)}">${esc(rec.title) || "-"}</td>
      <td>${fmtTime(rec.start_ts)}</td>
      <td>${fmtDuration(dur)}</td>
      <td>${fmtSize(rec.size)}</td>
      <td>${rec.danmaku_count || 0}</td>
      <td>${statusNames[rec.status] || rec.status}</td>
      <td>${links.join(" / ") || "-"}</td>
      <td></td>`;
    const lastTd = tr.querySelector("td:last-child");
    if (rec.status !== "recording" && rec.status !== "converting") {
      const del = document.createElement("button");
      del.className = "btn small danger";
      del.textContent = "删除";
      del.addEventListener("click", async () => {
        if (!confirm(`删除记录 #${rec.id}？${rec.video_url ? "\n将同时删除对应视频/弹幕文件！" : ""}`)) return;
        try { await api(`/api/recordings/${rec.id}?delete_files=1`, { method: "DELETE" }); toast("已删除"); loadRecordings(); }
        catch (e) { toast(e.message, true); }
      });
      lastTd.appendChild(del);
    }
    tbody.appendChild(tr);
  }
  const pages = Math.max(1, Math.ceil(data.total / state.recLimit));
  $("#page-info").textContent = `第 ${state.recPage + 1} / ${pages} 页（共 ${data.total} 条）`;
  $("#btn-prev").disabled = state.recPage === 0;
  $("#btn-next").disabled = state.recPage >= pages - 1;
}

$("#btn-prev").addEventListener("click", () => { if (state.recPage > 0) { state.recPage--; loadRecordings(); } });
$("#btn-next").addEventListener("click", () => { state.recPage++; loadRecordings(); });

// ---------- settings ----------
$("#btn-settings").addEventListener("click", async () => {
  $("#modal-settings").classList.remove("hidden");
  try {
    const s = await api("/api/settings");
    $("#set-cookie").value = s.cookie || "";
    $("#set-poll-interval").value = s.poll_interval || 30;
    $("#set-default-split-mode").value = s.default_split_mode || "none";
    $("#set-default-split-arg").value = s.default_split_arg || 30;
    $("#set-filename-format").value = s.filename_format || "";
    $("#set-filename-format").placeholder = s.filename_format_default || "";
  } catch (e) { toast(e.message, true); }
});
$("#btn-close-settings").addEventListener("click", () => $("#modal-settings").classList.add("hidden"));

$("#btn-cookie-check").addEventListener("click", async () => {
  const el = $("#cookie-status");
  el.textContent = "检测中…"; el.className = "msg";
  try {
    const r = await api("/api/settings/cookie_check", {
      method: "POST",
      body: JSON.stringify({ cookie: $("#set-cookie").value.trim() }),
    });
    el.textContent = r.logged_in ? `已登录：${r.uname}（UID ${r.mid}）` : `未登录：${r.message || "Cookie 无效或未填"}`;
    el.className = r.logged_in ? "msg ok" : "msg err";
  } catch (e) { el.textContent = e.message; el.className = "msg err"; }
});

$("#btn-save-settings").addEventListener("click", async () => {
  try {
    const mode = $("#set-default-split-mode").value;
    const body = {
      cookie: $("#set-cookie").value.trim(),
      poll_interval: Number($("#set-poll-interval").value) || 30,
      default_split_mode: mode,
      filename_format: $("#set-filename-format").value.trim(),
    };
    if (mode !== "none") body.default_split_arg = Number($("#set-default-split-arg").value) || 0;
    await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
    toast("设置已保存");
    $("#modal-settings").classList.add("hidden");
    refresh();
  } catch (e) { toast(e.message, true); }
});

// ---------- room config modal ----------
let roomModalId = 0;

function syncSplitUnit() {
  const mode = $("#room-split-mode").value;
  $("#room-split-unit").textContent = mode === "size" ? "MB" : "分钟";
  $("#room-split-arg").disabled = mode === "none";
}

function openRoomModal(r) {
  roomModalId = r.room_id;
  $("#room-config-id").textContent = `#${r.room_id} ${r.title || r.name || ""}`;
  $("#room-quality").value = String(r.quality);
  $("#room-danmaku").checked = !!r.record_danmaku;
  $("#room-convert").checked = !!r.auto_convert_mp4;
  $("#room-split-mode").value = r.split_mode || "none";
  $("#room-split-arg").value = r.split_arg || (r.split_mode === "size" ? 2048 : 30);
  syncSplitUnit();
  $("#modal-room").classList.remove("hidden");
}

$("#room-split-mode").addEventListener("change", syncSplitUnit);
$("#btn-close-room").addEventListener("click", () => $("#modal-room").classList.add("hidden"));
$("#btn-save-room").addEventListener("click", async () => {
  const mode = $("#room-split-mode").value;
  const body = {
    quality: Number($("#room-quality").value),
    record_danmaku: $("#room-danmaku").checked ? 1 : 0,
    auto_convert_mp4: $("#room-convert").checked ? 1 : 0,
    split_mode: mode,
  };
  if (mode !== "none") body.split_arg = Number($("#room-split-arg").value) || 0;
  try {
    await api(`/api/rooms/${roomModalId}`, { method: "PATCH", body: JSON.stringify(body) });
    toast("已保存，下次录制生效");
    $("#modal-room").classList.add("hidden");
    refresh();
  } catch (e) { toast(e.message, true); }
});

// ---------- status bar & refresh ----------
async function loadStatus() {
  try {
    const s = await api("/api/status");
    $("#status-bar").textContent =
      `监控 ${s.rooms_total} · 直播中 ${s.living} · 录制中 ${s.recording} · 磁盘剩余 ${fmtSize(s.disk_free)}`;
  } catch { $("#status-bar").textContent = "服务连接失败"; }
}

function refresh() {
  loadRooms();
  loadStatus();
  if (state.tab === "recordings") loadRecordings();
}

loadRooms();
loadStatus();
setInterval(() => { loadRooms(); loadStatus(); }, 5000);

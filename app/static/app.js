"use strict";
const $ = (id) => document.getElementById(id);
let mode = "rtmp",
  fileId = "",
  socket = null,
  loggedIn = false,
  config = {},
  current = {},
  allEvents = [],
  lastEventId = 0,
  initialized = false,
  uploading = false,
  toastTimer;
const phases = {
  idle: "待接入",
  connecting: "连接中",
  running: "监测中",
  reconnecting: "重连中",
  stopped: "已停止",
  ended: "播放完毕",
  error: "输入异常",
};
const states = {
  IDLE: ["待机", "等待开始一个新的监测任务"],
  WARMUP: ["建立背景", "正在采集背景参考，暂不作结论"],
  ARMED: ["持续监测", "当前未确认点火事件"],
  VERIFYING: ["疑似点火", "正在检查连续强证据，尚未发出确认"],
  CONFIRMED: ["点火已确认", "已生成点火确认事件"],
  REVIEW: ["点火已确认", "当前火光减弱，保留已发生事件"],
  PAUSED: ["画面复核", "画面暂不可判定，已有确认事件保留"],
  COOLDOWN: ["纠错冷却", "候选已取消或确认已撤销"],
  STOPPED: ["已停止", "检测任务已停止，事件记录保留"],
};
const labels = {
  started: "监测任务开始",
  confirmed: "点火已确认",
  retracted: "点火确认已撤销",
  candidate_cancelled: "疑似信号已取消",
  command: "事件指令执行",
  ended: "视频检查完成",
  reconnecting: "直播信号重连",
  error: "输入出现异常",
  config_saved: "任务配置已保存",
  config_migrated: "已升级为发动机初始火光模式，请核对喷口区域",
};
function toast(text, alert = false) {
  $("toast").textContent = text;
  $("toast").className = "show" + (alert ? " alert" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").className = ""), 5000);
}
async function api(path, options = {}) {
  const r = await fetch("/api/" + path, {
    ...options,
    headers: {
      ...(options.body && !(options.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...options.headers,
    },
  });
  let data;
  try {
    data = await r.json();
  } catch {
    data = { detail: "服务器响应异常" };
  }
  if (!r.ok) {
    if (r.status === 401 && path !== "login") showLogin();
    throw Error(
      typeof data.detail === "string" ? data.detail : "参数无效，请检查配置",
    );
  }
  return data;
}
function showLogin() {
  loggedIn = false;
  $("login").hidden = false;
  $("app").hidden = true;
  $("preview").removeAttribute("src");
  if (socket) {
    socket.onclose = null;
    socket.close();
    socket = null;
  }
}
async function enter() {
  config = await api("config");
  loggedIn = true;
  $("login").hidden = true;
  $("app").hidden = false;
  $("preview").src = "/preview.mjpg";
  drawROI();
  connect();
}
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("login", {
      method: "POST",
      body: JSON.stringify({ token: $("token").value }),
    });
    $("token").value = "";
    $("login-error").textContent = "";
    await enter();
  } catch (err) {
    $("login-error").textContent = err.message;
  }
});
$("logout").onclick = async () => {
  try {
    await api("logout", { method: "POST" });
  } catch {}
  showLogin();
};
function connect() {
  if (!loggedIn) return;
  socket = new WebSocket(
    (location.protocol === "https:" ? "wss://" : "ws://") +
      location.host +
      "/ws",
  );
  socket.onopen = () => {
    $("connection").className = "connection online";
    $("connection").lastChild.textContent = " 服务已连接";
  };
  socket.onmessage = (e) => {
    const data = JSON.parse(e.data);
    renderStatus(data.status);
    if (Array.isArray(data.events)) renderEvents(data.events);
  };
  socket.onclose = () => {
    if (!loggedIn) return;
    $("connection").className = "connection offline";
    $("connection").lastChild.textContent = " 正在重连";
    setTimeout(connect, 1800);
  };
  socket.onerror = () => socket.close();
}
function timecode(t) {
  t = Math.floor(t || 0);
  return [Math.floor(t / 3600), Math.floor(t / 60) % 60, t % 60]
    .map((v) => String(v).padStart(2, "0"))
    .join(":");
}
function renderStatus(s) {
  current = s;
  if (s.active_roi) drawROI(s.active_roi);
  const running = ["running", "connecting", "reconnecting"].includes(s.phase);
  $("phase").textContent = phases[s.phase] || s.phase;
  $("confirm-policy").textContent =
    (s.detector_mode || config.detector_mode) === "legacy"
      ? "最近 10 帧至少 8 帧强证据"
      : "喷口局部强证据，4 帧中至少 3 帧确认";
  $("phase").parentElement.classList.toggle("live", s.phase === "running");
  $("source-name").textContent = s.source_name || "NO SIGNAL";
  $("start").disabled = running || uploading;
  $("stop").disabled = !running;
  $("settings-open").disabled = running;
  $("fps").textContent = Number(s.fps || 0).toFixed(1);
  $("frames").textContent = (s.frames || 0).toLocaleString();
  $("score").textContent = Number(s.score || 0).toFixed(4);
  $("video-time").textContent = timecode(s.source_seconds);
  let state = states[s.state] || [s.state, ""];
  $("state").textContent = state[0];
  $("state-desc").textContent = s.error || state[1];
  $("preview-status").textContent =
    s.phase === "running" ? "● MONITORING" : s.phase.toUpperCase();
  const hasFrame = s.frames > 0;
  $("preview").hidden = !hasFrame;
  $("placeholder").hidden = hasFrame;
  $("roi-box").hidden = !hasFrame;
  $("preview-caption").textContent = hasFrame
    ? "实时采样预览 · 检测区域已标记"
    : "画面就绪后显示检测区域";
  const confirmed = s.confirmed_latched ?? (s.event === 1);
  $("event-value").textContent = confirmed
    ? (s.event === null ? "已确认 · 画面复核" : "已确认点火")
    : s.event === null && hasFrame
      ? "暂不可判定"
      : "等待确认";
  $("retract").disabled = !confirmed;
  $("event-value").closest(".stat").classList.toggle("fired", confirmed);
}
function renderEvents(events) {
  allEvents = events;
  const max = events[0]?.id || 0;
  if (initialized && max > lastEventId) {
    for (const e of [...events].reverse().filter((e) => e.id > lastEventId)) {
      if (e.kind === "confirmed") toast("已确认点火 · 事件 #" + e.decision_id);
      if (e.kind === "retracted")
        toast("点火确认已撤销 · 补偿指令已按配置派发", true);
    }
  }
  if (initialized && max === lastEventId) return;
  initialized = true;
  lastEventId = max;
  $("event-count").textContent = events.length;
  const root = $("activity-list");
  root.replaceChildren();
  if (!events.length) {
    const p = document.createElement("div");
    p.className = "empty-events";
    p.textContent = "任务开始后，确认、撤销与指令结果会出现在这里。";
    root.append(p);
    return;
  }
  for (const e of events) {
    const row = document.createElement("div");
    row.className = "event-row " + e.kind;
    const dot = document.createElement("span");
    dot.className = "event-dot";
    dot.textContent =
      e.kind === "confirmed"
        ? "↗"
        : e.kind === "retracted"
          ? "↶"
          : e.kind === "command"
            ? "⌘"
            : "·";
    const main = document.createElement("div");
    main.className = "event-main";
    const title = document.createElement("strong");
    title.textContent = labels[e.kind] || e.kind;
    const desc = document.createElement("p");
    if (e.kind === "command")
      desc.textContent =
        ({
          ok: "执行成功",
          failed: "执行失败",
          timeout: "执行超时",
          rejected: "队列不可用",
        }[e.status] || e.status) +
        (e.exit_code !== undefined ? " · 退出码 " + e.exit_code : "") +
        (e.error ? " · " + e.error : "") +
        (e.output ? "\n" + e.output.slice(-800) : "");
    else
      desc.textContent =
        (e.source_name ||
          e.message ||
          (e.decision_id ? "事件 #" + e.decision_id : "")) +
        (e.source_seconds !== undefined
          ? " · 视频 " + timecode(e.source_seconds)
          : "");
    main.append(title, desc);
    const time = document.createElement("time");
    time.textContent = new Date(e.ts * 1000).toLocaleTimeString("zh-CN", {
      hour12: false,
    });
    row.append(dot, main, time);
    root.append(row);
  }
}
function drawROI(activeROI) {
  const [x1, y1, x2, y2] = activeROI || (config.detector_mode === "legacy" ? config.roi : config.engine_roi) || [0.47, 0.79, 0.55, 0.91];
  Object.assign($("roi-box").style, {
    left: x1 * 100 + "%",
    top: y1 * 100 + "%",
    width: (x2 - x1) * 100 + "%",
    height: (y2 - y1) * 100 + "%",
  });
}
function setMode(value) {
  mode = value;
  $("tab-rtmp").setAttribute("aria-selected", value === "rtmp");
  $("tab-file").setAttribute("aria-selected", value === "file");
  $("rtmp-panel").hidden = value !== "rtmp";
  $("file-panel").hidden = value !== "file";
}
$("tab-rtmp").onclick = () => setMode("rtmp");
$("tab-file").onclick = () => setMode("file");
function upload(file) {
  if (!file || uploading) return;
  uploading = true;
  fileId = "";
  $("start").disabled = true;
  $("file-label").textContent = "正在上传 " + file.name;
  $("upload-progress").hidden = false;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable)
      $("upload-progress").value = (e.loaded / e.total) * 100;
  };
  xhr.onload = () => {
    try {
      const data = JSON.parse(xhr.responseText);
      if (xhr.status !== 200)
        throw Error(typeof data.detail === "string" ? data.detail : "上传失败");
      fileId = data.file_id;
      $("file-label").textContent = data.name;
      toast("视频上传完成");
    } catch (e) {
      $("file-label").textContent = "重新选择视频";
      toast(e.message, true);
    } finally {
      uploading = false;
      $("upload-progress").hidden = true;
      renderStatus(current);
    }
  };
  xhr.onerror = () => {
    uploading = false;
    $("upload-progress").hidden = true;
    $("file-label").textContent = "重新选择视频";
    renderStatus(current);
    toast("上传连接失败", true);
  };
  const data = new FormData();
  data.append("file", file);
  xhr.send(data);
}
$("video-file").onchange = (e) => upload(e.target.files[0]);
for (const type of ["dragover", "dragenter"])
  $("dropzone").addEventListener(type, (e) => {
    e.preventDefault();
    $("dropzone").classList.add("dragging");
  });
$("dropzone").ondragleave = () => $("dropzone").classList.remove("dragging");
$("dropzone").ondrop = (e) => {
  e.preventDefault();
  $("dropzone").classList.remove("dragging");
  upload(e.dataTransfer.files[0]);
};
$("start").onclick = async () => {
  if (mode === "file" && !fileId) {
    toast("请先上传视频", true);
    return;
  }
  if (mode === "rtmp" && !$("stream-url").value.trim()) {
    toast("请输入直播地址", true);
    return;
  }
  $("start").disabled = true;
  try {
    const s = await api("start", {
      method: "POST",
      body: JSON.stringify({
        kind: mode,
        url: $("stream-url").value.trim(),
        file_id: fileId,
      }),
    });
    renderStatus(s);
    toast("监测任务已开始");
  } catch (e) {
    toast(e.message, true);
    $("start").disabled = false;
  }
};
$("stop").onclick = async () => {
  try {
    renderStatus(await api("stop", { method: "POST" }));
    toast("监测已停止");
  } catch (e) {
    toast(e.message, true);
  }
};
$("retract").onclick = async () => {
  try {
    renderStatus(await api("retract", { method: "POST" }));
    toast("确认已撤销，补偿指令按配置执行");
  } catch (e) {
    toast(e.message, true);
  }
};
$("fullscreen").onclick = () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else
    $("screen")
      .requestFullscreen()
      .catch(() => toast("当前浏览器不支持全屏", true));
};
function fillDetectorSettings() {
  const engine = $("detector-mode").value === "engine_glow";
  const roi = (engine ? config.engine_roi : config.roi) || [0.47,0.79,0.55,0.91];
  roi.forEach((v,i) => $("roi-"+i).value = v);
  $("candidate-threshold").value = (engine ? config.engine_candidate_threshold : config.candidate_threshold) || .0005;
  $("strong-threshold").value = (engine ? config.engine_strong_threshold : config.strong_threshold) || (engine ? .001 : .01);
  $("warm-threshold").value = config.min_warm_score ?? .0003;
  $("warm-threshold").disabled = engine;
  $("auto-viewport").disabled = !engine;
}
$("detector-mode").onchange = fillDetectorSettings;
$("settings-open").onclick = async () => {
  try {
    config = await api("config");
    $("detector-mode").value = config.detector_mode || "engine_glow";
    $("auto-viewport").checked = config.auto_viewport !== false;
    fillDetectorSettings();
    $("commands-enabled").checked = config.commands_enabled;
    $("confirm-command").value = config.confirm_command;
    $("retract-command").value = config.retract_command;
    $("command-timeout").value = config.command_timeout;
    $("settings").showModal();
  } catch (e) {
    toast(e.message, true);
  }
};
$("settings-close").onclick = () => $("settings").close();
$("settings-form").onsubmit = async (e) => {
  e.preventDefault();
  const engine = $("detector-mode").value === "engine_glow";
  const data = {
    ...config,
    detector_mode: $("detector-mode").value,
    auto_viewport: $("auto-viewport").checked,
    commands_enabled: $("commands-enabled").checked,
    confirm_command: $("confirm-command").value,
    retract_command: $("retract-command").value,
    command_timeout: Number($("command-timeout").value),
  };
  data[engine ? "engine_roi" : "roi"] = [0,1,2,3].map(i => Number($("roi-" + i).value));
  data[engine ? "engine_candidate_threshold" : "candidate_threshold"] = Number($("candidate-threshold").value);
  data[engine ? "engine_strong_threshold" : "strong_threshold"] = Number($("strong-threshold").value);
  data.min_warm_score = Number($("warm-threshold").value);
  try {
    await api("config", { method: "PUT", body: JSON.stringify(data) });
    config = data;
    drawROI();
    $("settings").close();
    toast("配置已保存，下次任务生效");
  } catch (err) {
    toast(err.message, true);
  }
};
$("download-events").onclick = async () => {
  try {
    const events = await api("events");
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(events, null, 2)], { type: "application/json" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = "ignition-events.json";
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    toast(e.message, true);
  }
};
enter().catch(() => showLogin());

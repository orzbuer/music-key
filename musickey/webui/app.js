"use strict";

const $ = (id) => document.getElementById(id);
const listEl = $("list");
const emptyEl = $("empty");
const state = {
  items: new Map(),
  busyUploads: 0,
  filter: "",
};

const PLATFORMS = {
  ncm: { name: "网易云", color: "#ff6b6b" },
  mflac: { name: "QQ音乐", color: "#31c27c" },
  mgg: { name: "QQ音乐", color: "#31c27c" },
  qmc: { name: "QQ音乐", color: "#31c27c" },
  tkm: { name: "QQ音乐", color: "#31c27c" },
  bkc: { name: "QQ音乐", color: "#31c27c" },
  kgm: { name: "酷狗", color: "#4d9fff" },
  kgma: { name: "酷狗", color: "#4d9fff" },
  vpr: { name: "酷狗", color: "#4d9fff" },
  kwm: { name: "酷我", color: "#ffb020" },
};

function platformOf(name) {
  const lower = name.toLowerCase();
  for (const key of Object.keys(PLATFORMS).sort((a, b) => b.length - a.length)) {
    if (lower.includes(key)) return PLATFORMS[key];
  }
  return { name: "未知", color: "#7c5cff" };
}

function fmtSize(n) {
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
  return n + " B";
}

function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove("show"), 2600);
}

function statusText(item) {
  const phaseText = {
    "decrypt": "解密中",
    "transcode": "转码中",
    "wait": "处理中",
    "prepare": "准备中",
  };
  if (item.status === "uploading") return "上传中";
  if (item.status === "queued") return "排队中";
  if (item.status === "running") return phaseText[item.phase] || "处理中";
  if (item.status === "done") return "完成";
  if (item.status === "failed") return "失败";
  if (item.status === "cancelled") return "已取消";
  return "等待中";
}

function iconSvg(kind) {
  if (kind === "download") return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0 5-5m-5 5-5-5"/><path d="M4 20h16"/></svg>';
  if (kind === "play") return '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 5v14l12-7z"/></svg>';
  if (kind === "trash") return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M9 7V4h6v3m-8 0 1 13h8l1-13"/></svg>';
  return "";
}

function render() {
  const query = state.filter.trim().toLowerCase();
  let uploading = 0, queued = 0, running = 0, done = 0, failed = 0;
  listEl.innerHTML = "";
  for (const item of state.items.values()) {
    if (query && !item.name.toLowerCase().includes(query)) continue;
    if (item.status === "uploading") uploading++;
    else if (item.status === "queued") queued++;
    else if (item.status === "running") running++;
    else if (item.status === "done") done++;
    else if (item.status === "failed") failed++;

    const card = document.createElement("article");
    card.className = "card";
    card.style.setProperty("--pc", item.platform.color);

    const cover = document.createElement("div");
    cover.className = "cover";
    cover.innerHTML = '<span style="font-size:22px">♫</span>';
    if (item.coverUrl) cover.innerHTML = `<img src="${item.coverUrl}" alt="">`;

    const info = document.createElement("div");
    info.className = "info";
    const name = document.createElement("div");
    name.className = "name";
    name.innerHTML = `<strong title="${item.name.replace(/"/g, "&quot;")}"></strong><span class="platform"></span>`;
    name.querySelector("strong").textContent = item.name;
    name.querySelector(".platform").textContent = item.platform.name;
    const meta = document.createElement("div");
    meta.className = "meta";
    const sourceSize = item.file ? `${fmtSize(item.file.size)} · ${item.sourceExt}` : item.phase || "";
    const tagline = item.status === "done" && item.savedName
      ? `已保存 ${item.savedName}${item.size ? ` · ${fmtSize(item.size)}` : ""}`
      : (item.outputName ? `输出 ${item.container.toUpperCase()} · ${fmtSize(item.size)}` : (item.error || item.phase || ""));
    meta.innerHTML = `<span>${sourceSize}</span><span class="dot">·</span><span class="tagline">${tagline || "等待上传"}</span>`;
    info.append(name, meta);

    if (item.error && item.status === "failed") {
      const error = document.createElement("div");
      error.className = "error";
      error.textContent = item.error;
      info.appendChild(error);
    }
    if (item.status === "uploading" || item.status === "running" || item.status === "queued") {
      const progress = document.createElement("div");
      progress.className = "progress";
      const fill = document.createElement("div");
      fill.style.width = `${item.progress || 4}%`;
      progress.appendChild(fill);
      info.appendChild(progress);
    }

    const end = document.createElement("div");
    end.className = "end";
    const stateEl = document.createElement("span");
    stateEl.className = `state ${item.status}`;
    stateEl.innerHTML = `<span class="dot"></span><span></span>`;
    stateEl.querySelector("span:last-child").textContent = statusText(item);
    const actions = document.createElement("div");
    actions.className = "actions";
    if (item.status === "done" && item.id) {
      const playable = ["mp3", "flac", "m4a", "ogg", "wav"].includes((item.container || "").toLowerCase());
      if (playable) {
        const play = document.createElement("a");
        play.className = "icon-btn";
        play.href = `/api/audio/${item.id}`;
        play.target = "_blank";
        play.innerHTML = iconSvg("play");
        actions.appendChild(play);
      }
      const download = document.createElement("a");
      download.className = "icon-btn primary";
      download.href = `/api/download/${item.id}`;
      download.title = "下载";
      download.innerHTML = iconSvg("download");
      actions.appendChild(download);
    }
    const remove = document.createElement("button");
    remove.className = "icon-btn";
    remove.title = "移除";
    remove.innerHTML = iconSvg("trash");
    remove.onclick = () => removeItem(item);
    actions.appendChild(remove);
    end.append(stateEl, actions);

    card.append(cover, info, end);
    listEl.appendChild(card);
  }

  emptyEl.classList.toggle("hidden", state.items.size > 0);
  $("stats").innerHTML =
    `<span>总任务 <b>${state.items.size}</b></span>` +
    `<span>上传中 <b style="color:var(--accent-2)">${uploading}</b></span>` +
    `<span>处理中 <b style="color:var(--accent-2)">${running}</b></span>` +
    `<span>完成 <b style="color:var(--ok)">${done}</b></span>` +
    `<span>失败 <b style="color:var(--err)">${failed}</b></span>`;
}

function addFiles(files) {
  for (const file of files) {
    if (!file.name) continue;
    const key = file.name + ":" + file.size;
    if (state.items.has(key)) continue;
    const platform = platformOf(file.name);
    state.items.set(key, {
      key,
      name: file.name,
      file,
      sourceExt: (file.name.split(".").pop() || "?").toUpperCase(),
      platform,
      status: "uploading",
      progress: 0,
      phase: "上传中",
      id: null,
      coverUrl: null,
      outputName: null,
      savedName: null,
      container: "",
      size: 0,
      error: "",
    });
  }
  render();
  pumpUploads();
}

function pumpUploads() {
  const max = 2;
  while (state.busyUploads < max) {
    const item = [...state.items.values()].find((candidate) => candidate.status === "uploading");
    if (!item) break;
    state.busyUploads++;
    uploadItem(item);
  }
}

function uploadItem(item) {
  const fd = new FormData();
  fd.append("file", item.file);
  fd.append("format", $("format").value);
  fd.append("embed_cover", $("embedCover").checked ? "1" : "0");
  const ekey = $("ekey").value.trim();
  if (ekey) fd.append("ekey", ekey);

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/jobs");
  xhr.upload.onprogress = (event) => {
    if (event.lengthComputable) {
      item.progress = Math.round((event.loaded / event.total) * 40);
      render();
    }
  };
  xhr.onload = () => {
    try {
      const payload = JSON.parse(xhr.responseText || "{}");
      if (payload.ok) {
        item.id = payload.id;
        item.status = "queued";
        item.progress = 42;
        item.phase = "排队中";
        pollItem(item);
      } else {
        item.status = "failed";
        item.error = payload.error || "上传失败";
        item.progress = 100;
      }
    } catch {
      item.status = "failed";
      item.error = "服务器响应异常";
      item.progress = 100;
    }
    state.busyUploads--;
    render();
    pumpUploads();
  };
  xhr.onerror = () => {
    item.status = "failed";
    item.error = "网络错误";
    item.progress = 100;
    state.busyUploads--;
    render();
    pumpUploads();
  };
  xhr.send(fd);
}

function pollItem(item) {
  if (!item.id || item.status === "done" || item.status === "failed" || item.status === "cancelled") return;
  fetch(`/api/jobs/${item.id}`)
    .then((response) => response.json())
    .then((payload) => {
      if (!payload.ok) {
        item.status = "failed";
        item.error = payload.error || "任务不存在";
        item.progress = 100;
        render();
        return;
      }
      const job = payload.job;
      item.status = job.status;
      item.progress = job.status === "done" ? 100 : Math.max(item.progress, 42 + Math.round(job.progress * 0.58));
      item.phase = job.phase || job.status;
      item.container = job.container || item.container;
      item.size = job.size || 0;
      item.outputName = job.output_name;
      item.savedName = job.saved_name || job.output_name;
      item.error = job.error || "";
      if (job.tags && job.tags.title) item.name = job.tags.title;
      if (job.status === "done" && job.has_cover) {
        fetch(`/api/cover/${item.id}`)
          .then((response) => response.blob())
          .then((blob) => {
            item.coverUrl = URL.createObjectURL(blob);
            render();
          })
          .catch(() => {});
      }
      render();
      if (["queued", "running"].includes(job.status)) setTimeout(() => pollItem(item), 550);
    })
    .catch(() => {
      if (["queued", "running"].includes(item.status)) setTimeout(() => pollItem(item), 900);
    });
}

async function removeItem(item) {
  if (item.id && !["done", "failed", "cancelled"].includes(item.status)) {
    await fetch(`/api/jobs/${item.id}`, { method: "DELETE" }).catch(() => {});
  }
  state.items.delete(item.key || item.name + ":" + item.file?.size);
  render();
}

async function collectEntry(entry, out) {
  if (entry.isFile) {
    const file = await new Promise((resolve) => entry.file(resolve));
    out.push(file);
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    while (true) {
      const batch = await new Promise((resolve) => reader.readEntries(resolve));
      if (!batch.length) break;
      for (const child of batch) await collectEntry(child, out);
    }
  }
}

async function health() {
  try {
    const response = await fetch("/api/health");
    const payload = await response.json();
    const pill = $("health");
    pill.textContent = `本地服务 v${payload.version}${payload.ffmpeg ? "" : " · 无 ffmpeg"}`;
    pill.classList.add("ok");
  } catch {
    $("health").textContent = "连接失败";
  }
}

const drop = $("drop");
drop.addEventListener("click", () => $("picker").click());
$("pickBtn").addEventListener("click", (event) => {
  event.stopPropagation();
  $("picker").click();
});
$("picker").addEventListener("change", (event) => {
  addFiles([...event.target.files]);
  event.target.value = "";
});
drop.addEventListener("dragover", (event) => {
  event.preventDefault();
  drop.classList.add("over");
});
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", async (event) => {
  event.preventDefault();
  drop.classList.remove("over");
  const entries = [...(event.dataTransfer.items || [])]
    .map((entry) => entry.webkitGetAsEntry && entry.webkitGetAsEntry())
    .filter(Boolean);
  if (entries.length) {
    const files = [];
    for (const entry of entries) await collectEntry(entry, files);
    addFiles(files);
  } else {
    addFiles([...event.dataTransfer.files]);
  }
});

$("filter").addEventListener("input", (event) => {
  state.filter = event.target.value;
  render();
});
async function saveOutputDir() {
  const outputDir = $("outputDir").value.trim();
  const response = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ output_dir: outputDir }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!payload.ok) {
    toast(payload.error || "保存失败");
    return false;
  }
  $("outputDir").value = payload.output_dir;
  toast("成品目录已保存");
  return true;
}

async function loadSettings() {
  try {
    const response = await fetch("/api/settings");
    const payload = await response.json();
    if (payload.ok) {
      $("outputDir").value = payload.output_dir;
      $("lyricDir").value = payload.lyric_dir || "";
    }
  } catch {
    toast("无法读取成品目录");
  }
}

$("saveDirBtn").addEventListener("click", (event) => {
  event.stopPropagation();
  saveOutputDir();
});
$("outputDir").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    saveOutputDir();
  }
});
async function saveLyricDir() {
  const lyricDir = $("lyricDir").value.trim();
  const response = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lyric_dir: lyricDir }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!payload.ok) {
    toast(payload.error || "保存失败");
    return false;
  }
  $("lyricDir").value = payload.lyric_dir || "";
  toast(lyricDir ? "歌词目录已保存" : "已清除歌词目录");
  return true;
}

$("saveLyricBtn").addEventListener("click", (event) => {
  event.stopPropagation();
  saveLyricDir();
});
$("lyricDir").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    saveLyricDir();
  }
});
$("browseLyricBtn").addEventListener("click", async (event) => {
  event.stopPropagation();
  toast("请在弹出的窗口里选择歌词所在文件夹");
  const response = await fetch("/api/browse-folder", { method: "POST" });
  const payload = await response.json().catch(() => ({}));
  if (!payload.ok) {
    if (payload.error && payload.error !== "已取消") toast(payload.error);
    return;
  }
  $("lyricDir").value = payload.output_dir;
  await saveLyricDir();
});
$("browseDirBtn").addEventListener("click", async (event) => {
  event.stopPropagation();
  toast("请在弹出的窗口里选择文件夹");
  const response = await fetch("/api/browse-folder", { method: "POST" });
  const payload = await response.json().catch(() => ({}));
  if (!payload.ok) {
    if (payload.error && payload.error !== "已取消") toast(payload.error);
    return;
  }
  $("outputDir").value = payload.output_dir;
  await saveOutputDir();
});
$("revealBtn").addEventListener("click", async () => {
  const response = await fetch("/api/reveal", { method: "POST" });
  const payload = await response.json().catch(() => ({}));
  if (!payload.ok) {
    toast(payload.error || "无法打开目录");
    return;
  }
  toast(payload.output_dir ? `已打开 ${payload.output_dir}` : "已打开成品目录");
});
$("clearBtn").addEventListener("click", async () => {
  await fetch("/api/clear", { method: "POST" }).catch(() => {});
  for (const item of [...state.items.values()]) {
    if (item.status === "done" || item.status === "failed" || item.status === "cancelled") state.items.delete(item.key || item.name + ":" + item.file?.size);
  }
  render();
  toast("已清空完成的任务");
});
$("exitBtn").addEventListener("click", async () => {
  if (!confirm("确定退出音钥 MusicKey？")) return;
  await fetch("/api/shutdown", { method: "POST" }).catch(() => {});
});

health();
loadSettings();
render();

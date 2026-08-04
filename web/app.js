
const $ = (id) => document.getElementById(id);
let pollTimer = null;
let busy = false;
let pollFails = 0;
const branchCache = {};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || data.detail || data.message || res.statusText);
    err.data = data;
    throw err;
  }
  return data;
}

function setPill(el, text, cls) {
  if (!el) return;
  el.textContent = text;
  el.className = "pill" + (cls ? " " + cls : "");
}

function showJob(msg, logText) {
  const meta = $("jobMeta");
  const log = $("jobLog");
  if (meta) meta.textContent = msg;
  if (log && logText !== undefined) log.textContent = logText;
  const panel = $("logsPanel");
  if (panel) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function refreshHealth() {
  const h = await api("/api/health");
  setPill($("pillAgent"), "助手在线", "ok");
  if (h.docker && h.docker.ok) setPill($("pillDocker"), "Docker 可用", "ok");
  else setPill($("pillDocker"), "Docker 不可用", "bad");
  if ($("targetRepo")) $("targetRepo").textContent = h.registry + "/" + h.org;
  if ($("footNote")) {
    $("footNote").textContent = h.allow_remote
      ? "服务器共享模式 · 一人登录 SWR 后，过期前其他人可直接构建推送"
      : "仅本机访问 · SWR 临时密码只用于 Docker 登录";
  }
  if ($("tokenStatus")) {
    if (h.github_ssh_configured) {
      $("tokenStatus").textContent = "已配置 GitHub SSH（服务器密钥）";
      $("tokenStatus").style.color = "var(--ok)";
    } else {
      $("tokenStatus").textContent = h.github_token_configured
        ? "已配置 GitHub Token"
        : "未配置 Token / SSH";
      $("tokenStatus").style.color = h.github_token_configured ? "var(--ok)" : "var(--muted)";
    }
  }
  return h;
}

async function saveToken() {
  await api("/api/config/token", {
    method: "POST",
    body: JSON.stringify({ github_token: ($("tokenInput").value || "").trim() }),
  });
  $("tokenInput").value = "";
  await refreshHealth();
}

async function login() {
  $("loginLog").textContent = "登录中…";
  try {
    const data = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ command: ($("loginCmd").value || "").trim() }),
    });
    $("loginLog").textContent = data.output || (data.ok ? "Login Succeeded" : "failed");
  } catch (e) {
    $("loginLog").textContent = (e.data && e.data.output) || e.message;
  }
}

async function checkLogin() {
  $("loginLog").textContent = "检查中…";
  try {
    const data = await api("/api/login/check", { method: "POST", body: "{}" });
    $("loginLog").textContent = data.ok
      ? "登录有效：服务器共享凭证仍可用，其他人可直接构建推送"
      : "未登录或已过期：需要任一人重新粘贴 docker login";
  } catch (e) {
    $("loginLog").textContent = e.message;
  }
}

async function logout() {
  const log = $("loginLog");
  if (log) log.textContent = "正在退出服务器上的共享 SWR 登录…";
  try {
    const data = await api("/api/login/logout", { method: "POST", body: "{}" });
    if (log) {
      log.textContent =
        (data.output || "已退出") +
        "\n共享凭证已清除；之后需要任一人重新登录才能推送。";
    }
    if ($("loginCmd")) $("loginCmd").value = "";
  } catch (e) {
    if (log) log.textContent = "退出失败：" + ((e.data && e.data.output) || e.message || String(e));
  }
}

function fillBranchSelect(selectEl, branches, defaultBranch) {
  selectEl.innerHTML = "";
  const list = branches && branches.length ? branches : [defaultBranch || "main"];
  for (const b of list) {
    const opt = document.createElement("option");
    opt.value = b;
    opt.textContent = b;
    if (b === defaultBranch) opt.selected = true;
    selectEl.appendChild(opt);
  }
  selectEl.disabled = false;
}

async function loadBranches(serviceId, selectEl, defaultBranch) {
  if (branchCache[serviceId] && branchCache[serviceId].loaded) {
    fillBranchSelect(selectEl, branchCache[serviceId].branches, defaultBranch);
    return;
  }
  selectEl.disabled = true;
  try {
    const data = await api("/api/services/" + encodeURIComponent(serviceId) + "/branches");
    branchCache[serviceId] = { loaded: true, branches: data.branches || [] };
    fillBranchSelect(selectEl, data.branches || [], data.default_branch || defaultBranch);
  } catch (e) {
    fillBranchSelect(selectEl, [defaultBranch || "main"], defaultBranch || "main");
  }
}

function selectedItems() {
  const items = [];
  document.querySelectorAll(".row-svc").forEach((row) => {
    const cb = row.querySelector('[data-act="pick"]');
    if (!cb || !cb.checked) return;
    const id = row.getAttribute("data-id");
    const title = row.getAttribute("data-title") || id;
    const select = row.querySelector("[data-branch]");
    const branch = ((select && select.value) || row.getAttribute("data-default-branch") || "main").trim();
    items.push({ service_id: id, branch, title });
  });
  return items;
}

function updateBatchUi() {
  const items = selectedItems();
  const btn = $("btnBatch");
  const count = $("selCount");
  if (count) count.textContent = String(items.length);
  if (btn) {
    btn.disabled = busy || items.length === 0;
    btn.textContent = items.length ? "构建所选 (" + items.length + ")" : "构建所选";
  }
  const all = $("chkAll");
  if (all) {
    const boxes = Array.from(document.querySelectorAll('[data-act="pick"]'));
    const checked = boxes.filter((b) => b.checked).length;
    all.checked = boxes.length > 0 && checked === boxes.length;
    all.indeterminate = checked > 0 && checked < boxes.length;
  }
}

function setButtonsDisabled(disabled) {
  document.querySelectorAll('[data-act="run"]').forEach((btn) => {
    btn.disabled = !!disabled;
  });
  document.querySelectorAll('[data-act="pick"]').forEach((cb) => {
    cb.disabled = !!disabled;
  });
  if ($("chkAll")) $("chkAll").disabled = !!disabled;
  if ($("btnBatch")) $("btnBatch").disabled = !!disabled || selectedItems().length === 0;
}

async function refreshServices() {
  const data = await api("/api/services");
  const root = $("serviceTable");
  root.innerHTML = "";
  for (const s of data.services) {
    const row = document.createElement("div");
    row.className = "row-svc";
    row.setAttribute("data-id", s.id);
    row.setAttribute("data-title", s.title || s.id);
    row.setAttribute("data-default-branch", s.default_branch || "main");
    row.innerHTML =
      '<label class="svc-pick"><input type="checkbox" data-act="pick" /></label>' +
      "<div>" +
      '<div class="svc-title"></div>' +
      '<div class="svc-meta"></div>' +
      '<label class="branch-label">分支 <select class="input branch-select" data-branch></select></label>' +
      "</div>" +
      '<div class="svc-actions"><button type="button" class="btn primary" data-act="run">构建并推送</button></div>';
    row.querySelector(".svc-title").textContent = s.title;
    row.querySelector(".svc-meta").textContent = s.repo || s.github || "";
    const select = row.querySelector("[data-branch]");
    fillBranchSelect(select, [s.default_branch || "main"], s.default_branch || "main");
    setTimeout(() => loadBranches(s.id, select, s.default_branch), 30);
    row.querySelector('[data-act="pick"]').addEventListener("change", updateBatchUi);
    const btn = row.querySelector('[data-act="run"]');
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const branch = (select.value || s.default_branch || "main").trim();
      pushItems([{ service_id: s.id, branch, title: s.title }], btn);
    });
    root.appendChild(row);
  }
  updateBatchUi();
  if (busy) setButtonsDisabled(true);
}

async function pushItems(items, btn) {
  if (busy) {
    showJob("已有任务进行中，请等待完成");
    return;
  }
  if (!items || !items.length) {
    showJob("请先勾选要构建的微服务");
    return;
  }
  busy = true;
  setButtonsDisabled(true);
  if (btn) btn.textContent = "进行中…";
  const summary =
    items.length === 1
      ? items[0].title + " @ " + items[0].branch
      : items.length + " 个服务";
  showJob(summary + " · 已提交，正在启动…", "正在请求 /api/push …\n");
  try {
    const loginCmd = (($("loginCmd") && $("loginCmd").value) || "").trim();
    const resp = await api("/api/push", {
      method: "POST",
      body: JSON.stringify({
        items: items.map((it) => ({ service_id: it.service_id, branch: it.branch })),
        login_command: loginCmd,
      }),
    });
    const jobId = resp.job_id;
    pollFails = 0;
    showJob(summary + " · job " + jobId, "job_id=" + jobId + "\n开始轮询日志…\n");
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => pollJob(jobId, summary), 1500);
    await pollJob(jobId, summary);
  } catch (e) {
    const msg = (e.data && (e.data.detail || e.data.error)) || e.message;
    showJob(summary + " · 失败", String(msg));
    busy = false;
    setButtonsDisabled(false);
    document.querySelectorAll('[data-act="run"]').forEach((b) => (b.textContent = "构建并推送"));
    updateBatchUi();
  }
}

async function pollJob(jobId, summary) {
  try {
    const job = await api("/api/jobs/" + jobId);
    pollFails = 0;
    const logEl = $("jobLog");
    if (logEl) {
      logEl.textContent = (job.log || []).join("\n") || "(暂无日志)";
      logEl.scrollTop = logEl.scrollHeight;
    }
    const progress = job.progress ? " · " + job.progress : "";
    const current = job.current ? " · " + job.current : "";
    if (job.status === "running" || job.status === "unknown") {
      $("jobMeta").textContent = summary + " · 进行中" + progress + current;
      return;
    }
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
    busy = false;
    setButtonsDisabled(false);
    document.querySelectorAll('[data-act="run"]').forEach((b) => (b.textContent = "构建并推送"));
    updateBatchUi();
    const archiveHint = job.archive_dir || job.archive || "";
    if (job.status === "ok") {
      $("jobMeta").textContent =
        summary +
        " · 成功" +
        (archiveHint ? " · 归档 " + archiveHint : "") +
        (job.remote ? " · " + job.remote : "");
    } else {
      $("jobMeta").textContent =
        summary +
        " · 失败 · " +
        (job.error || "") +
        (archiveHint ? " · 归档 " + archiveHint : "");
    }
  } catch (e) {
    pollFails += 1;
    if (pollFails < 80) {
      if ($("jobMeta")) {
        $("jobMeta").textContent = summary + " · 网络抖动，重连中 (" + pollFails + ")";
      }
      return;
    }
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
    busy = false;
    setButtonsDisabled(false);
    updateBatchUi();
    showJob("轮询失败", e.message || String(e));
  }
}

function bind(id, fn) {
  const el = $(id);
  if (el) el.addEventListener("click", fn);
}

bind("btnLogin", login);
bind("btnCheckLogin", checkLogin);
bind("btnLogout", logout);
bind("btnSaveToken", () =>
  saveToken().catch((e) => {
    $("tokenStatus").textContent = e.message;
    $("tokenStatus").style.color = "var(--danger)";
  })
);
bind("btnBatch", () => {
  const items = selectedItems();
  pushItems(items, $("btnBatch"));
});

const chkAll = $("chkAll");
if (chkAll) {
  chkAll.addEventListener("change", () => {
    const on = !!chkAll.checked;
    document.querySelectorAll('[data-act="pick"]').forEach((cb) => {
      cb.checked = on;
    });
    updateBatchUi();
  });
}

(async function boot() {
  try {
    await Promise.all([refreshHealth(), refreshServices()]);

    // Auto-recover live running job after page refresh
    try {
      const running = await api("/api/running-job");
      if (running && running.id && running.status === "running") {
        const summary = running.service_id || "任务";
        showJob(summary + " · job " + running.id, (running.log || []).join("\n") || "(正在重新连接…)");
        if (pollTimer) clearInterval(pollTimer);
        busy = true;
        setButtonsDisabled(true);
        pollTimer = setInterval(() => pollJob(running.id, summary), 1500);
        return;
      }
    } catch (_) { /* ignore – backend may not have the endpoint yet */ }

    showJob(
      "就绪",
      "可勾选多个微服务后点「构建所选」；同一次任务归档到同一时间戳目录。\n也可点单行「构建并推送」。"
    );
  } catch (e) {
    $("serviceTable").innerHTML = '<p class="hint">无法连接助手，请先运行 start.bat</p>';
    showJob("助手未连接", String(e.message || e));
  }
})();

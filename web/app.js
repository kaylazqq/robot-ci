
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

function setButtonsDisabled(disabled) {
  document.querySelectorAll('[data-act="run"]').forEach((btn) => {
    btn.disabled = !!disabled;
  });
}

async function refreshServices() {
  const data = await api("/api/services");
  const root = $("serviceTable");
  root.innerHTML = "";
  for (const s of data.services) {
    const row = document.createElement("div");
    row.className = "row-svc";
    row.innerHTML =
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
    const btn = row.querySelector('[data-act="run"]');
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const branch = (select.value || s.default_branch || "main").trim();
      pushOne(s.id, s.title, branch, btn);
    });
    root.appendChild(row);
  }
  if (busy) setButtonsDisabled(true);
}

async function pushOne(serviceId, title, branch, btn) {
  if (busy) {
    showJob("已有任务进行中，请等待完成");
    return;
  }
  busy = true;
  setButtonsDisabled(true);
  if (btn) btn.textContent = "进行中…";
  showJob(title + " @ " + branch + " · 已提交，正在启动…", "正在请求 /api/push …\n");
  try {
    const loginCmd = (($("loginCmd") && $("loginCmd").value) || "").trim();
    // loginCmd may be empty: server reuses shared SWR login if still valid.
    const resp = await api("/api/push", {
      method: "POST",
      body: JSON.stringify({
        service_id: serviceId,
        branch: branch,
        login_command: loginCmd,
      }),
    });
    const jobId = resp.job_id;
    pollFails = 0;
    showJob(title + " @ " + branch + " · job " + jobId, "job_id=" + jobId + "\n开始轮询日志…\n");
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => pollJob(jobId, title, branch), 1500);
    await pollJob(jobId, title, branch);
  } catch (e) {
    const msg = (e.data && (e.data.detail || e.data.error)) || e.message;
    showJob(title + " · 失败", String(msg));
    busy = false;
    setButtonsDisabled(false);
    document.querySelectorAll('[data-act="run"]').forEach((b) => (b.textContent = "构建并推送"));
  }
}

async function pollJob(jobId, title, branch) {
  try {
    const job = await api("/api/jobs/" + jobId);
    pollFails = 0;
    const logEl = $("jobLog");
    if (logEl) {
      logEl.textContent = (job.log || []).join("\n") || "(暂无日志)";
      logEl.scrollTop = logEl.scrollHeight;
    }
    const br = job.branch || branch;
    if (job.status === "running" || job.status === "unknown") {
      $("jobMeta").textContent = title + " @ " + br + " · 进行中";
      return;
    }
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
    busy = false;
    setButtonsDisabled(false);
    document.querySelectorAll('[data-act="run"]').forEach((b) => (b.textContent = "构建并推送"));
    if (job.status === "ok") {
      $("jobMeta").textContent = title + " @ " + br + " · 成功 · " + (job.remote || "");
    } else {
      $("jobMeta").textContent = title + " @ " + br + " · 失败 · " + (job.error || "");
    }
  } catch (e) {
    pollFails += 1;
    // Keep polling through transient network blips (Failed to fetch / timeouts)
    if (pollFails < 80) {
      if ($("jobMeta")) {
        $("jobMeta").textContent =
          title + " @ " + branch + " · 网络抖动，重连中 (" + pollFails + ")";
      }
      return;
    }
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
    busy = false;
    setButtonsDisabled(false);
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

(async function boot() {
  try {
    await Promise.all([refreshHealth(), refreshServices()]);
    showJob("就绪", "可直接构建推送；若失败提示未登录，点「检查登录」或重新登录 SWR。");
  } catch (e) {
    $("serviceTable").innerHTML = '<p class="hint">无法连接助手，请先运行 start.bat</p>';
    showJob("助手未连接", String(e.message || e));
  }
})();

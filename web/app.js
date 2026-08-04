
const $ = (id) => document.getElementById(id);
let pollTimer = null;
let busy = false;
let pollFails = 0;
const branchCache = {};
let testCasePage = 1;
let testCaseJobId = "";
let testCasePageSize = 10;

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
  renderTestResult(null);
  const panel = $("logsPanel");
  if (panel) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function formatDuration(ms) {
  const value = Number(ms || 0);
  if (value === 0) return "<0.001 ms";
  if (value < 1) return value.toFixed(3) + " ms";
  if (value < 10) return value.toFixed(2) + " ms";
  if (value < 1000) return value.toFixed(1) + " ms";
  return (value / 1000).toFixed(value >= 10000 ? 0 : 1) + " s";
}

function isQuietDependencyLog(line) {
  const text = String(line || "").replace(/^\[[^\]]+\]\s*/, "").trim().toLowerCase();
  return (
    text.startsWith("requirement already satisfied:") ||
    text.startsWith("collecting ") ||
    text.startsWith("downloading ") ||
    text.startsWith("using cached ") ||
    text.startsWith("building wheels for collected packages") ||
    text.startsWith("building wheel for ") ||
    text.startsWith("installing collected packages:") ||
    text.startsWith("successfully installed ") ||
    text.startsWith("looking in indexes:") ||
    text.startsWith("processing ") ||
    text.startsWith("defaulting to user installation")
  );
}

function displayJobLog(lines) {
  const visible = [];
  let testRunning = false;
  for (const line of lines || []) {
    const text = String(line || "");
    const plain = text.replace(/^\[[^\]]+\]\s*/, "").trim();
    if (plain.startsWith("tests start ")) {
      testRunning = true;
      visible.push(text.replace("tests start", "Starting tests"));
      continue;
    }
    if (plain.startsWith("@@TEST_STEP@@ ")) {
      visible.push(text.replace("@@TEST_STEP@@ ", "Running test suite: "));
      continue;
    }
    if (plain.startsWith("TEST summary ")) {
      testRunning = false;
      continue;
    }
    if (text.includes("@@TEST_SUMMARY@@")) {
      continue;
    }
    if (testRunning) {
      if (/^\{.*"Action"\s*:/.test(plain)) {
        continue;
      }
      if (/(^|\s)(error|failed|failure|panic|traceback)(:|\s|$)/i.test(plain)) {
        visible.push(text);
      }
      continue;
    }
    if (isQuietDependencyLog(line)) {
      continue;
    }
    visible.push(line);
  }
  return visible.join("\n") || "(暂无日志)";
}

function shortCaseName(name) {
  const value = String(name || "");
  if (value.startsWith("Test") || value.startsWith("test_")) return value;
  if (value.startsWith("github.com/")) {
    const testMarker = value.indexOf(".Test");
    return testMarker >= 0 ? value.slice(testMarker + 1) : value;
  }
  const parts = value.split(".");
  return parts.length > 2 ? parts.slice(-2).join(".") : value;
}

function renderTestResult(job) {
  const root = $("testResult");
  if (!root) return;
  if (!job || !job.test_status) {
    root.hidden = true;
    root.replaceChildren();
    return;
  }
  const summary = job.test_summary || {};
  const isOk = job.test_status === "passed";
  const overallLabel = isOk ? "通过" : job.test_status === "not_configured" ? "未配置" : "未通过";
  const commands = Array.isArray(job.test_commands) ? job.test_commands : [];
  const cases = Array.isArray(job.test_cases) ? job.test_cases : [];
  const failures = Array.isArray(job.test_failures) ? job.test_failures : [];
  if (job.id !== testCaseJobId) {
    testCaseJobId = job.id || "";
    testCasePage = 1;
  }
  root.hidden = false;
  root.replaceChildren();

  const head = document.createElement("div");
  head.className = "test-result-head";
  const title = document.createElement("strong");
  title.textContent = "测试结果：" + overallLabel;
  title.className = "test-status " + (isOk ? "ok" : "bad");
  const overview = document.createElement("span");
  overview.className = "test-result-summary";
  overview.textContent = "总用例 " + (summary.total || 0) + " · 通过 " + (summary.passed || 0) + " · 失败 " + (summary.failed || 0) + " · 错误 " + (summary.errors || 0) + " · 跳过 " + (summary.skipped || 0) + " · 总耗时 " + formatDuration(summary.duration_ms);
  head.append(title, overview);
  root.appendChild(head);

  const gateHint = document.createElement("div");
  gateHint.className = "test-gate-hint";
  gateHint.textContent = "当前测试结果不阻塞镜像构建和推送";
  root.appendChild(gateHint);

  const sourceRows = cases.length ? cases : commands;
  const statusPriority = { failed: 0, error: 0, skipped: 1, passed: 2 };
  const rows = cases.length
    ? sourceRows.map((item, index) => ({ item, index }))
        .sort((left, right) =>
          (statusPriority[left.item.status] ?? 0) - (statusPriority[right.item.status] ?? 0) || left.index - right.index)
        .map(({ item }) => item)
    : sourceRows;
  if (rows.length) {
    const pageSize = testCasePageSize;
    const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
    testCasePage = Math.min(testCasePage, pageCount);
    const start = (testCasePage - 1) * pageSize;
    const pageRows = rows.slice(start, start + pageSize);
    const table = document.createElement("table");
    table.className = "test-table";
    const thead = document.createElement("thead");
    const header = document.createElement("tr");
    (cases.length ? ["测试脚本", "测试用例", "结果", "耗时"] : ["执行脚本", "结果", "耗时"]).forEach((label) => {
      const th = document.createElement("th");
      th.textContent = label;
      header.appendChild(th);
    });
    thead.appendChild(header);
    table.appendChild(thead);
    const body = document.createElement("tbody");
    pageRows.forEach((command, index) => {
      const row = document.createElement("tr");
      const sourceFile = command.file || command.command;
      const previousSourceFile = index > 0 ? (pageRows[index - 1].file || pageRows[index - 1].command) : "";
      if (cases.length && (index === 0 || previousSourceFile !== sourceFile)) {
        const script = document.createElement("td");
        script.className = "test-script";
        script.textContent = sourceFile || "测试脚本";
        let span = 1;
        while (index + span < pageRows.length && (pageRows[index + span].file || pageRows[index + span].command) === sourceFile) span += 1;
        script.rowSpan = span;
        row.appendChild(script);
      }
      const name = document.createElement("td");
      const caseName = document.createElement("div");
      caseName.textContent = cases.length ? shortCaseName(command.name) : command.name || "测试脚本";
      name.appendChild(caseName);
      if (command.detail && command.status !== "passed") {
        const detail = document.createElement("div");
        detail.className = "test-case-detail";
        detail.textContent = command.detail;
        name.appendChild(detail);
      }
      const status = document.createElement("td");
      const caseStatus = command.status;
      const passed = caseStatus ? caseStatus === "passed" : Number(command.exit_code) === 0;
      const skipped = caseStatus === "skipped";
      status.textContent = skipped ? "跳过" : passed ? "通过" : caseStatus === "error" ? "错误" : "失败";
      status.className = "test-status " + (passed ? "ok" : skipped ? "" : "bad");
      const duration = document.createElement("td");
      duration.textContent = command.duration_ms == null ? "—" : formatDuration(command.duration_ms);
      row.append(name, status, duration);
      body.appendChild(row);
    });
    table.appendChild(body);
    root.appendChild(table);
    if (pageCount > 1) {
      const pager = document.createElement("div");
      pager.className = "test-pager";
      const sizeControl = document.createElement("label");
      sizeControl.className = "test-page-size";
      sizeControl.append("每页 ");
      const sizeSelect = document.createElement("select");
      [10, 25, 50, 100].forEach((size) => {
        const option = document.createElement("option");
        option.value = String(size);
        option.textContent = String(size);
        option.selected = size === testCasePageSize;
        sizeSelect.appendChild(option);
      });
      sizeSelect.addEventListener("change", () => {
        testCasePageSize = Number(sizeSelect.value) || 10;
        testCasePage = 1;
        renderTestResult(job);
      });
      sizeControl.append(sizeSelect, " 条");
      const previous = document.createElement("button");
      previous.type = "button";
      previous.className = "btn ghost";
      previous.textContent = "上一页";
      previous.disabled = testCasePage <= 1;
      previous.addEventListener("click", () => { testCasePage -= 1; renderTestResult(job); });
      const position = document.createElement("span");
      position.textContent = "第 " + testCasePage + " / " + pageCount + " 页（" + rows.length + " 条用例）";
      const next = document.createElement("button");
      next.type = "button";
      next.className = "btn ghost";
      next.textContent = "下一页";
      next.disabled = testCasePage >= pageCount;
      next.addEventListener("click", () => { testCasePage += 1; renderTestResult(job); });
      pager.append(sizeControl, previous, position, next);
      root.appendChild(pager);
    }
  }

  if (failures.length) {
    const list = document.createElement("ul");
    list.className = "test-failures";
    failures.slice(0, 10).forEach((failure) => {
      const item = document.createElement("li");
      item.textContent = (failure.name || "测试失败") + "：" + (failure.detail || "请查看完整作业日志");
      list.appendChild(item);
    });
    root.appendChild(list);
  }
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
      logEl.textContent = displayJobLog(job.log || []);
      logEl.scrollTop = logEl.scrollHeight;
    }
    renderTestResult(job);
    const br = job.branch || branch;
    if (job.status === "running" || job.status === "unknown") {
      const stage = job.stage ? " · " + job.stage : "";
      $("jobMeta").textContent = title + " @ " + br + " · 进行中" + stage;
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

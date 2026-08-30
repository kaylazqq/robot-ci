
const $ = (id) => document.getElementById(id);
let pollTimer = null;
let activeProbeTimer = null;
let lastJobExpiryTimer = null;
let busy = true;
let pollFails = 0;
let pollGeneration = 0;
let activeJobId = "";
let liveLogLines = [];
let logCursor = 0;
let testRevision = -1;
let cachedTestRuns = [];
const branchCache = {};
const BRANCH_LOAD_LIMIT = 3;
let branchLoadsActive = 0;
const branchLoadQueue = [];
let testResultJobId = "";
const testTableState = new Map();
const artifactsPager = { page: 1, pageSize: 20 };
const historyPager = { page: 1, pageSize: 20 };
let historyDetailTimer = null;
let historyDetailGen = 0;
let historyDetailJobId = "";
let historyLogLines = [];
let historyLogCursor = 0;
const LAST_JOB_KEY = "robotCiLastJob";
const LEGACY_ACTIVE_JOB_KEY = "robotCiActiveJob";
const MY_JOBS_KEY = "robotCiMyJobs";
const CLIENT_ID_KEY = "robotCiClientId";
const LAST_JOB_TTL_MS = 10 * 60 * 1000;
const DAEMON_VERSION_KEY = "robotCiDaemonVersion";
const DAEMON_VERSION_PATTERN = /^v[0-9]+\.[0-9]+\.[0-9]+$/;
const LOG_FOLLOW_SLOP_PX = 48;
let myJobs = [];
let submitting = false;
const liveLogFollow = newLogFollowState();
const historyLogFollow = newLogFollowState();

/** In-memory Huawei Cloud credentials for CCE APIs only (never persisted). */
let hwCloudCreds = null;
let cceRegionsLoaded = false;

function newLogFollowState() {
  return { dragging: false, follow: true, pendingText: null, flush: null };
}

function resetLogFollow(state) {
  state.dragging = false;
  state.follow = true;
  state.pendingText = null;
}

function isLogNearBottom(el) {
  if (!el) return true;
  return el.scrollHeight - el.scrollTop - el.clientHeight <= LOG_FOLLOW_SLOP_PX;
}

function bindLogFollow(el, state) {
  if (!el || el.dataset.logFollowBound === "1") return;
  el.dataset.logFollowBound = "1";
  const onDown = () => {
    state.dragging = true;
  };
  const onUp = () => {
    if (!state.dragging) return;
    state.dragging = false;
    state.follow = isLogNearBottom(el);
    if (typeof state.flush === "function") state.flush();
  };
  el.addEventListener("pointerdown", onDown);
  el.addEventListener("mousedown", onDown);
  window.addEventListener("pointerup", onUp);
  window.addEventListener("mouseup", onUp);
  window.addEventListener("pointercancel", onUp);
  el.addEventListener("scroll", () => {
    if (state.dragging) {
      state.follow = false;
      return;
    }
    state.follow = isLogNearBottom(el);
  });
}

function paintLog(el, text, state) {
  if (!el) return;
  bindLogFollow(el, state);
  const write = (nextText) => {
    const keepTop = el.scrollTop;
    const stick = state.follow && !state.dragging;
    if (el.textContent === nextText) {
      if (stick) el.scrollTop = el.scrollHeight;
      return;
    }
    el.textContent = nextText;
    el.scrollTop = stick ? el.scrollHeight : keepTop;
  };
  state.flush = () => {
    if (state.pendingText === null) return;
    const nextText = state.pendingText;
    state.pendingText = null;
    write(nextText);
  };
  if (state.dragging) {
    state.pendingText = text;
    return;
  }
  state.pendingText = null;
  write(text);
}

function clientId() {
  try {
    let id = localStorage.getItem(CLIENT_ID_KEY) || "";
    if (!/^[A-Za-z0-9_-]{8,64}$/.test(id)) {
      id =
        (crypto.randomUUID && crypto.randomUUID().replace(/-/g, "")) ||
        ("c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
      localStorage.setItem(CLIENT_ID_KEY, id);
    }
    return id;
  } catch (_) {
    return "anon" + Date.now().toString(36);
  }
}

function loadMyJobs() {
  try {
    const raw = JSON.parse(localStorage.getItem(MY_JOBS_KEY) || "[]");
    myJobs = Array.isArray(raw) ? raw.filter((j) => j && j.id) : [];
  } catch (_) {
    myJobs = [];
  }
}

function saveMyJobs() {
  try {
    localStorage.setItem(MY_JOBS_KEY, JSON.stringify(myJobs.slice(0, 20)));
  } catch (_) {
    /* ignore */
  }
}

function upsertMyJob(jobId, summary, status) {
  if (!jobId) return;
  const existing = myJobs.find((j) => j.id === jobId);
  if (existing) {
    existing.summary = summary || existing.summary;
    if (status) existing.status = status;
    existing.observed_at = Date.now();
  } else {
    myJobs.unshift({
      id: jobId,
      summary: summary || jobId,
      status: status || "running",
      observed_at: Date.now(),
    });
  }
  myJobs = myJobs.slice(0, 20);
  saveMyJobs();
}

function rememberedDaemonVersion() {
  try {
    const value = (localStorage.getItem(DAEMON_VERSION_KEY) || "").trim();
    return DAEMON_VERSION_PATTERN.test(value) ? value : "";
  } catch (_) {
    return "";
  }
}

function rememberDaemonVersion(value) {
  if (!DAEMON_VERSION_PATTERN.test(value || "")) return;
  try { localStorage.setItem(DAEMON_VERSION_KEY, value); } catch (_) { /* storage may be disabled */ }
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || data.detail || data.message || res.statusText);
    err.data = data;
    err.status = res.status;
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
  if (log && logText !== undefined) {
    resetLogFollow(liveLogFollow);
    log.textContent = logText;
    log.scrollTop = log.scrollHeight;
  }
  renderTestResult(null);
  const panel = $("logsPanel");
  if (panel && getMainView() === "services") {
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function getMainView() {
  return ($("mainLayout") && $("mainLayout").dataset.view) || "services";
}

function setMainView(view) {
  const prev = getMainView();
  const next =
    view === "artifacts" || view === "history" || view === "cce" ? view : "services";
  const layout = $("mainLayout");
  if (layout) layout.dataset.view = next;
  const servicesPane = $("viewServices");
  const artifactsPane = $("viewArtifacts");
  const historyPane = $("viewHistory");
  const ccePane = $("viewCce");
  if (servicesPane) servicesPane.hidden = next !== "services";
  if (artifactsPane) artifactsPane.hidden = next !== "artifacts";
  if (historyPane) historyPane.hidden = next !== "history";
  if (ccePane) ccePane.hidden = next !== "cce";
  document.querySelectorAll(".view-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === next);
  });
  if (next === "artifacts") {
    refreshArtifacts().catch(() => {});
  }
  if (next === "history") {
    if (prev === "history" && historyDetailJobId) {
      closeHistoryDetail();
    } else if (!historyDetailJobId) {
      refreshHistory().catch(() => {});
    }
  }
  if (next === "cce") {
    initCceView().catch(() => {});
  }
  try {
    localStorage.setItem("robotCiMainView", next);
  } catch (_) {
    /* ignore */
  }
}

function shortSha(sha) {
  const value = String(sha || "").trim();
  return value ? value.slice(0, 7) : "—";
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
  );
}

function renderArtifacts(items, meta) {
  const body = $("artifactsBody");
  if (!body) return;
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="7" class="hint">暂无产物；成功构建后会出现在这里。</td></tr>';
    renderArtifactsPager(meta || { total: 0, page: 1, page_count: 1, page_size: artifactsPager.pageSize });
    return;
  }
  body.innerHTML = rows
    .map((item) => {
      const expired = item.expired || item.available === false;
      const imageName = item.remote && item.remote !== "archive-only"
        ? item.remote
        : item.image_ref || ((item.image || "") + (item.tag ? ":" + item.tag : ""));
      const pkg = item.package_name || (item.archive ? String(item.archive).split(/[/\\]/).pop() : "—");
      const link = expired
        ? '<span class="art-expired">已失效</span>'
        : item.download_url
          ? `<a href="${esc(item.download_url)}" target="_blank" rel="noopener">下载</a>`
          : '<span class="art-expired">已失效</span>';
      return (
        `<tr${expired ? ' class="expired"' : ""}>` +
        `<td class="mono">${esc(item.created_at || "—")}</td>` +
        `<td>${esc(item.title || item.service_id || "—")}</td>` +
        `<td class="mono">${esc(item.branch || "—")}</td>` +
        `<td class="mono" title="${esc(item.commit_sha || "")}">${esc(shortSha(item.commit_sha))}</td>` +
        `<td class="mono">${esc(imageName || "—")}</td>` +
        `<td class="mono">${esc(pkg)}</td>` +
        `<td>${link}</td>` +
        "</tr>"
      );
    })
    .join("");
  renderArtifactsPager(meta);
}

function renderArtifactsPager(meta) {
  const pager = $("artifactsPager");
  if (!pager) return;
  const total = Number((meta && meta.total) || 0);
  const pageCount = Math.max(1, Number((meta && meta.page_count) || 1));
  const pageSize = Number((meta && meta.page_size) || artifactsPager.pageSize) || 20;
  artifactsPager.page = Math.min(Math.max(1, Number((meta && meta.page) || artifactsPager.page) || 1), pageCount);
  artifactsPager.pageSize = pageSize;
  if (!total) {
    pager.hidden = true;
    pager.innerHTML = "";
    return;
  }
  pager.hidden = false;
  pager.innerHTML = "";
  const sizeControl = document.createElement("label");
  sizeControl.className = "test-page-size";
  sizeControl.append("每页 ");
  const sizeSelect = document.createElement("select");
  [10, 20, 50, 100].forEach((size) => {
    const option = document.createElement("option");
    option.value = String(size);
    option.textContent = String(size);
    option.selected = size === pageSize;
    sizeSelect.appendChild(option);
  });
  sizeSelect.addEventListener("change", () => {
    artifactsPager.pageSize = Number(sizeSelect.value) || 20;
    artifactsPager.page = 1;
    refreshArtifacts().catch(() => {});
  });
  sizeControl.append(sizeSelect, " 条");
  const previous = document.createElement("button");
  previous.type = "button";
  previous.className = "btn ghost";
  previous.textContent = "上一页";
  previous.disabled = artifactsPager.page <= 1;
  previous.addEventListener("click", () => {
    artifactsPager.page -= 1;
    refreshArtifacts().catch(() => {});
  });
  const position = document.createElement("span");
  const expired = Number((meta && meta.expired_count) || 0);
  position.textContent =
    "第 " + artifactsPager.page + " / " + pageCount + " 页（" + total + " 条" +
    (expired ? "，已失效 " + expired : "") + "）";
  const next = document.createElement("button");
  next.type = "button";
  next.className = "btn ghost";
  next.textContent = "下一页";
  next.disabled = artifactsPager.page >= pageCount;
  next.addEventListener("click", () => {
    artifactsPager.page += 1;
    refreshArtifacts().catch(() => {});
  });
  pager.append(sizeControl, previous, position, next);
}

async function refreshArtifacts() {
  try {
    const data = await api(
      "/api/artifacts?page=" + encodeURIComponent(String(artifactsPager.page)) +
      "&page_size=" + encodeURIComponent(String(artifactsPager.pageSize))
    );
    renderArtifacts(data.artifacts || [], data);
  } catch (e) {
    const body = $("artifactsBody");
    if (body) {
      body.innerHTML =
        '<tr><td colspan="7" class="hint" style="color:var(--danger)">产物列表加载失败：' +
        String(e.message || e) +
        "</td></tr>";
    }
    const pager = $("artifactsPager");
    if (pager) {
      pager.hidden = true;
      pager.innerHTML = "";
    }
  }
}

function historyStatusLabel(status) {
  if (status === "ok") return "成功";
  if (status === "running") return "进行中";
  if (status === "failed") return "失败";
  if (status === "stopped") return "已停止";
  return status || "—";
}

function historyStatusClass(status) {
  if (status === "ok") return "art-status-ok";
  if (status === "failed") return "art-status-failed";
  if (status === "running") return "art-status-running";
  if (status === "stopped") return "art-status-stopped";
  return "";
}

function setStopButtonState(btn, visible, stopping) {
  if (!btn) return;
  btn.hidden = !visible;
  btn.disabled = !!stopping;
  btn.textContent = stopping ? "正在停止…" : "停止任务";
}

function setLiveStopVisible(visible, stopping) {
  setStopButtonState($("btnStopJob"), visible, stopping);
}

async function stopRunningJob(jobId, { button, onStopping } = {}) {
  if (!jobId) return;
  setStopButtonState(button, true, true);
  try {
    await api("/api/jobs/" + jobId + "/stop", { method: "POST", body: "{}" });
    if (typeof onStopping === "function") onStopping();
  } catch (e) {
    setStopButtonState(button, true, false);
    const msg = (e.data && (e.data.detail || e.data.error)) || e.message || String(e);
    if ($("jobMeta") && jobId === activeJobId) {
      $("jobMeta").textContent = "停止失败 · " + msg;
    }
  }
}

function renderHistory(items, meta) {
  const body = $("historyBody");
  if (!body) return;
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="6" class="hint">暂无本浏览器的构建记录。</td></tr>';
    renderHistoryPager(meta || { total: 0, page: 1, page_count: 1, page_size: historyPager.pageSize });
    return;
  }
  body.innerHTML = rows
    .map((item) => {
      const status = item.status || "unknown";
      const services = Array.isArray(item.service_ids) && item.service_ids.length
        ? item.service_ids.join(", ")
        : (item.service_id || "—");
      const summary = services + (item.branch ? " @ " + item.branch : "");
      return (
        "<tr>" +
        `<td class="hist-time">${esc(item.created_at || "—")}</td>` +
        `<td class="hist-svc" title="${esc(services)}">${esc(services)}</td>` +
        `<td class="hist-branch" title="${esc(item.branch || "")}">${esc(item.branch || "—")}</td>` +
        `<td class="hist-status ${historyStatusClass(status)}">${esc(historyStatusLabel(status))}</td>` +
        `<td class="hist-sha">${esc(shortSha(item.commit_sha))}</td>` +
        `<td class="hist-act">` +
        (status === "running"
          ? `<button type="button" class="btn danger" data-history-stop="${esc(item.id)}">停止</button> `
          : "") +
        `<button type="button" class="btn ghost" data-history-job="${esc(item.id)}" data-history-summary="${esc(summary)}">查看日志</button>` +
        `</td>` +
        "</tr>"
      );
    })
    .join("");
  body.querySelectorAll("[data-history-job]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-history-job");
      const summary = btn.getAttribute("data-history-summary") || id;
      if (id) openHistoryJob(id, summary);
    });
  });
  body.querySelectorAll("[data-history-stop]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-history-stop");
      if (!id) return;
      stopRunningJob(id, {
        button: btn,
        onStopping: () => refreshHistory().catch(() => {}),
      });
    });
  });
  renderHistoryPager(meta);
}

function renderHistoryPager(meta) {
  const pager = $("historyPager");
  if (!pager) return;
  const total = Number((meta && meta.total) || 0);
  const pageCount = Math.max(1, Number((meta && meta.page_count) || 1));
  const pageSize = Number((meta && meta.page_size) || historyPager.pageSize) || 20;
  historyPager.page = Math.min(Math.max(1, Number((meta && meta.page) || historyPager.page) || 1), pageCount);
  historyPager.pageSize = pageSize;
  if (!total) {
    pager.hidden = true;
    pager.innerHTML = "";
    return;
  }
  pager.hidden = false;
  pager.innerHTML = "";
  const sizeControl = document.createElement("label");
  sizeControl.className = "test-page-size";
  sizeControl.append("每页 ");
  const sizeSelect = document.createElement("select");
  [10, 20, 50, 100].forEach((size) => {
    const option = document.createElement("option");
    option.value = String(size);
    option.textContent = String(size);
    option.selected = size === pageSize;
    sizeSelect.appendChild(option);
  });
  sizeSelect.addEventListener("change", () => {
    historyPager.pageSize = Number(sizeSelect.value) || 20;
    historyPager.page = 1;
    refreshHistory().catch(() => {});
  });
  sizeControl.append(sizeSelect, " 条");
  const previous = document.createElement("button");
  previous.type = "button";
  previous.className = "btn ghost";
  previous.textContent = "上一页";
  previous.disabled = historyPager.page <= 1;
  previous.addEventListener("click", () => {
    historyPager.page -= 1;
    refreshHistory().catch(() => {});
  });
  const position = document.createElement("span");
  position.textContent = "第 " + historyPager.page + " / " + pageCount + " 页（" + total + " 条）";
  const next = document.createElement("button");
  next.type = "button";
  next.className = "btn ghost";
  next.textContent = "下一页";
  next.disabled = historyPager.page >= pageCount;
  next.addEventListener("click", () => {
    historyPager.page += 1;
    refreshHistory().catch(() => {});
  });
  pager.append(sizeControl, previous, position, next);
}

function stopHistoryDetailPoll() {
  historyDetailGen += 1;
  if (historyDetailTimer) clearTimeout(historyDetailTimer);
  historyDetailTimer = null;
}

function showHistoryList() {
  stopHistoryDetailPoll();
  historyDetailJobId = "";
  historyLogLines = [];
  historyLogCursor = 0;
  renderJobPipeline("historyJobPipeline", null);
  setStopButtonState($("btnHistoryStop"), false, false);
  const list = $("historyList");
  const detail = $("historyDetail");
  if (list) list.hidden = false;
  if (detail) detail.hidden = true;
}

function closeHistoryDetail() {
  showHistoryList();
  refreshHistory().catch(() => {});
}

function openHistoryJob(jobId, summary) {
  stopHistoryDetailPoll();
  historyDetailJobId = jobId;
  historyLogLines = [];
  historyLogCursor = 0;
  const list = $("historyList");
  const detail = $("historyDetail");
  const meta = $("historyDetailMeta");
  const logEl = $("historyJobLog");
  if (list) list.hidden = true;
  if (detail) detail.hidden = false;
  if (meta) meta.textContent = (summary || jobId) + " · 加载中…";
  if (logEl) {
    resetLogFollow(historyLogFollow);
    logEl.textContent = "正在加载这次构建的日志…";
    logEl.scrollTop = 0;
  }
  const gen = historyDetailGen;
  const tick = async () => {
    if (gen !== historyDetailGen) return;
    try {
      const query = new URLSearchParams({
        compact: "1",
        view: "ui",
        log_after: String(historyLogCursor),
      });
      const job = await api("/api/jobs/" + jobId + "?" + query.toString());
      if (gen !== historyDetailGen) return;
      if (Array.isArray(job.log) && job.log.length) historyLogLines.push(...job.log);
      historyLogCursor = Number.isFinite(Number(job.log_cursor))
        ? Number(job.log_cursor)
        : historyLogLines.length;
      if (logEl) {
        paintLog(logEl, displayJobLog(historyLogLines), historyLogFollow);
      }
      renderJobPipeline("historyJobPipeline", job.pipeline, job.status);
      if (meta) {
        meta.textContent =
          (summary || job.service_id || jobId) +
          " · " +
          historyStatusLabel(job.status) +
          (job.progress ? " · " + job.progress : "") +
          (job.current ? " · " + job.current : "") +
          (job.error ? " · " + job.error : "");
      }
      const running = job.status === "running" || job.status === "unknown";
      setStopButtonState($("btnHistoryStop"), running, job.stage === "stopping");
      if (running) {
        historyDetailTimer = setTimeout(tick, 1500);
      }
    } catch (e) {
      if (gen !== historyDetailGen) return;
      if (meta) meta.textContent = (summary || jobId) + " · 加载失败";
      if (logEl) logEl.textContent = String(e.message || e);
    }
  };
  tick();
}

async function refreshHistory() {
  try {
    const data = await api(
      "/api/jobs?client_id=" + encodeURIComponent(clientId()) +
      "&page=" + encodeURIComponent(String(historyPager.page)) +
      "&page_size=" + encodeURIComponent(String(historyPager.pageSize))
    );
    renderHistory(data.jobs || [], data);
  } catch (e) {
    const body = $("historyBody");
    if (body) {
      body.innerHTML =
        '<tr><td colspan="6" class="hint" style="color:var(--danger)">构建历史加载失败：' +
        String(e.message || e) +
        "</td></tr>";
    }
    const pager = $("historyPager");
    if (pager) {
      pager.hidden = true;
      pager.innerHTML = "";
    }
  }
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
    if (plain.startsWith("@@TEST_ERROR@@ ")) {
      visible.push(text.replace("@@TEST_ERROR@@ ", "Test infrastructure error: "));
      continue;
    }
    if (plain.startsWith("TEST summary ")) {
      testRunning = false;
      continue;
    }
    if (plain.startsWith("Tests completed ")) {
      // Also accept the user-facing completion line as a boundary. This keeps
      // old in-memory jobs readable even if their internal summary was filtered.
      testRunning = false;
      visible.push(text);
      continue;
    }
    if (text.includes("@@TEST_SUMMARY@@")) {
      continue;
    }
    if (testRunning) {
      if (/^\{.*"Action"\s*:/.test(plain)) {
        continue;
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

function decodeUnicodeEscapes(value) {
  return String(value || "").replace(/(?:\\u[0-9a-fA-F]{4})+/g, (sequence) => {
    try {
      return JSON.parse('"' + sequence + '"');
    } catch (_) {
      return sequence;
    }
  });
}

function shortCaseName(name) {
  const value = decodeUnicodeEscapes(name);
  if (value.startsWith("Test") || value.startsWith("test_")) return value;
  if (value.startsWith("github.com/")) {
    const testMarker = value.indexOf(".Test");
    return testMarker >= 0 ? value.slice(testMarker + 1) : value;
  }
  const parts = value.split(".");
  return parts.length > 2 ? parts.slice(-2).join(".") : value;
}

function renderTestRun(root, run, stateKey, rerender) {
  const summary = run.summary || {};
  const testStatus = run.status || "error";
  const isOk = testStatus === "passed";
  const overallLabel = isOk ? "通过" : testStatus === "not_configured" ? "未配置" : "未通过";
  const commands = Array.isArray(run.commands) ? run.commands : [];
  const cases = Array.isArray(run.test_cases) ? run.test_cases : [];
  const failures = Array.isArray(run.failures) ? run.failures : [];
  const state = testTableState.get(stateKey) || { page: 1, pageSize: 10 };
  testTableState.set(stateKey, state);

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
  const sourceFileOf = (item) => String(item.file || item.command || "");
  const scriptPriority = new Map();
  if (cases.length) {
    sourceRows.forEach((item) => {
      const sourceFile = sourceFileOf(item);
      const priority = statusPriority[item.status] ?? 0;
      scriptPriority.set(sourceFile, Math.min(scriptPriority.get(sourceFile) ?? priority, priority));
    });
  }
  const rows = cases.length
    ? sourceRows.map((item, index) => ({ item, index }))
        .sort((left, right) => {
          const leftFile = sourceFileOf(left.item);
          const rightFile = sourceFileOf(right.item);
          return (scriptPriority.get(leftFile) ?? 0) - (scriptPriority.get(rightFile) ?? 0)
            || leftFile.localeCompare(rightFile, "en")
            || (statusPriority[left.item.status] ?? 0) - (statusPriority[right.item.status] ?? 0)
            || left.index - right.index;
        })
        .map(({ item }) => item)
    : sourceRows;
  if (rows.length) {
    const pageSize = state.pageSize;
    const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
    state.page = Math.min(state.page, pageCount);
    const start = (state.page - 1) * pageSize;
    const pageRows = rows.slice(start, start + pageSize);
    const table = document.createElement("table");
    table.className = "test-table";
    const thead = document.createElement("thead");
    const header = document.createElement("tr");
    (cases.length ? ["测试脚本", "测试用例", "结果", "耗时"] : ["执行脚本", "结果", "耗时"]).forEach((label, index) => {
      const th = document.createElement("th");
      th.textContent = label;
      if (cases.length) {
        th.className = ["test-script-column", "test-case-column", "test-status-column", "test-duration-column"][index];
      }
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
      name.className = "test-case";
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
    {
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
        option.selected = size === state.pageSize;
        sizeSelect.appendChild(option);
      });
      sizeSelect.addEventListener("change", () => {
        state.pageSize = Number(sizeSelect.value) || 10;
        state.page = 1;
        rerender();
      });
      sizeControl.append(sizeSelect, " 条");
      const previous = document.createElement("button");
      previous.type = "button";
      previous.className = "btn ghost";
      previous.textContent = "上一页";
      previous.disabled = state.page <= 1;
      previous.addEventListener("click", () => { state.page -= 1; rerender(); });
      const position = document.createElement("span");
      position.textContent = "第 " + state.page + " / " + pageCount + " 页（" + rows.length + " 条用例）";
      const next = document.createElement("button");
      next.type = "button";
      next.className = "btn ghost";
      next.textContent = "下一页";
      next.disabled = state.page >= pageCount;
      next.addEventListener("click", () => { state.page += 1; rerender(); });
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

const PIPELINE_STATE_LABELS = {
  pending: "未开始",
  running: "进行中",
  done: "已完成",
  failed: "失败",
  skipped: "跳过",
  warn: "有告警",
};

const PIPELINE_NODE_GLYPH = {
  pending: "",
  running: "",
  done: "✓",
  failed: "✕",
  skipped: "—",
  warn: "!",
};

const PIPELINE_POST_STEPS = [
  { id: "cleanup", label: "镜像清理", status: "pending" },
  { id: "index", label: "产物索引", status: "pending" },
  { id: "notify", label: "状态回写", status: "pending" },
];

function pipelineJobStatusLabel(status) {
  const map = { running: "执行中", ok: "成功", failed: "失败", stopped: "已停止", unknown: "未知" };
  return map[status] || status || "—";
}

function shortSha(sha) {
  const text = String(sha || "").trim();
  if (!text || text.startsWith("0000000")) return "";
  return text.slice(0, 8);
}

function resolvePostSteps(pipeline, jobStatus) {
  const steps = PIPELINE_POST_STEPS.map((item) => ({ ...item }));
  if (jobStatus === "ok") {
    steps.forEach((item) => { item.status = "done"; });
  } else if (jobStatus === "running") {
    const mainDone = (pipeline.steps || []).every((step) =>
      ["done", "skipped", "warn"].includes(step.status)
    );
    if (mainDone) steps[0].status = "running";
  } else if (jobStatus === "failed" || jobStatus === "stopped") {
    steps.forEach((item) => { item.status = "skipped"; });
  }
  return steps;
}

function flattenFlowTimeline(pipeline, jobStatus) {
  const prepare = (pipeline.prepare || []).map((step) => ({ step, tier: "mini", group: "prepare" }));
  const main = (pipeline.steps || []).map((step) => ({ step, tier: "main", group: "build" }));
  const post = resolvePostSteps(pipeline, jobStatus).map((step) => ({ step, tier: "mini", group: "post" }));
  return [...prepare, ...main, ...post];
}

function connectorFillStatus(prevStatus) {
  if (prevStatus === "done" || prevStatus === "warn") return "done";
  if (prevStatus === "failed") return "failed";
  if (prevStatus === "running") return "partial";
  if (prevStatus === "skipped") return "done";
  return "pending";
}

function createFlowSubdots(subtasks) {
  const row = document.createElement("div");
  row.className = "flow-subdots";
  (subtasks || []).forEach((sub) => {
    const dot = document.createElement("span");
    dot.className = "flow-subdot is-" + (sub.status || "pending");
    dot.title = (sub.label || sub.id || "") + " · " + (PIPELINE_STATE_LABELS[sub.status] || sub.status || "");
    row.appendChild(dot);
  });
  return row;
}

function createFlowConnector(prevStep, nextTier, subtasks) {
  const wrap = document.createElement("div");
  const prevStatus = (prevStep && prevStep.status) || "pending";
  wrap.className =
    "flow-connector is-" +
    connectorFillStatus(prevStatus) +
    (nextTier === "main" ? " to-main" : "") +
    (subtasks && subtasks.length ? " has-subdots" : "");
  const line = document.createElement("div");
  line.className = "flow-connector-line";
  wrap.appendChild(line);
  if (subtasks && subtasks.length) {
    wrap.appendChild(createFlowSubdots(subtasks));
  }
  return wrap;
}

function createFlowNode(item, activeId) {
  const step = item.step || {};
  const status = step.status || "pending";
  const tier = item.tier || "main";
  const isActive = step.id === activeId || status === "running";

  const node = document.createElement("div");
  node.className =
    "flow-node is-" +
    tier +
    " is-" +
    status +
    (isActive ? " is-active" : "") +
    (item.group ? " group-" + item.group : "");

  const ring = document.createElement("div");
  ring.className = "flow-ring";
  const glyph = document.createElement("span");
  glyph.className = "flow-glyph";
  glyph.textContent = PIPELINE_NODE_GLYPH[status] || (tier === "mini" ? "·" : "");
  ring.appendChild(glyph);
  node.appendChild(ring);

  const name = document.createElement("div");
  name.className = "flow-name";
  name.textContent = step.label || step.id || "";
  name.title = step.label || step.id || "";
  node.appendChild(name);

  const state = document.createElement("div");
  state.className = "flow-state";
  state.textContent = PIPELINE_STATE_LABELS[status] || status;
  node.appendChild(state);

  if (tier === "main" && (step.detail || isActive)) {
    const detail = document.createElement("div");
    detail.className = "flow-detail";
    detail.textContent = step.detail || step.hint || "";
    detail.title = detail.textContent;
    node.appendChild(detail);
  }

  return node;
}

function findActiveStepId(timeline) {
  const running = timeline.find((item) => (item.step.status || "") === "running");
  if (running) return running.step.id;
  const failed = timeline.find((item) => (item.step.status || "") === "failed");
  if (failed) return failed.step.id;
  for (let i = timeline.length - 1; i >= 0; i -= 1) {
    const st = timeline[i].step.status || "";
    if (st === "done" || st === "warn" || st === "skipped") return timeline[i].step.id;
  }
  return "";
}

function createFlowGroupMarker(label) {
  const mark = document.createElement("div");
  mark.className = "flow-group-mark";
  mark.textContent = label;
  return mark;
}

function renderJobPipeline(containerId, pipeline, jobStatus) {
  const root = $(containerId);
  if (!root) return;
  const steps = pipeline && Array.isArray(pipeline.steps) ? pipeline.steps : [];
  if (!pipeline || !steps.length) {
    root.hidden = true;
    root.replaceChildren();
    return;
  }

  root.hidden = false;
  root.replaceChildren();

  const meta = pipeline.meta || {};
  const summary = pipeline.summary || {};
  const status = jobStatus || meta.status || "";
  const timeline = flattenFlowTimeline(pipeline, status);
  const activeId = findActiveStepId(timeline);

  const services = Array.isArray(pipeline.services) ? pipeline.services : [];
  const focusId = pipeline.focus_service || pipeline.current_service || "";
  const focusSvc = services.find((item) => item.service_id === focusId) || services[0];

  const toolbar = document.createElement("div");
  toolbar.className = "flow-toolbar";
  const title = document.createElement("span");
  title.className = "flow-title";
  title.textContent = "执行流水线";
  toolbar.appendChild(title);
  if (focusSvc) {
    const focus = document.createElement("span");
    focus.className = "flow-focus";
    focus.textContent = (focusSvc.title || focusSvc.service_id) + (meta.branch ? " @ " + meta.branch : "");
    toolbar.appendChild(focus);
  }
  const badges = document.createElement("span");
  badges.className = "flow-badges";
  badges.textContent = [
    pipelineJobStatusLabel(status),
    pipeline.progress || "",
    shortSha(meta.commit_sha) ? "#" + shortSha(meta.commit_sha) : "",
  ].filter(Boolean).join(" · ");
  toolbar.appendChild(badges);
  if (summary && summary.percent != null) {
    const bar = document.createElement("div");
    bar.className = "flow-bar";
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-valuenow", String(summary.percent));
    const fill = document.createElement("div");
    fill.className = "flow-bar-fill";
    fill.style.width = summary.percent + "%";
    bar.appendChild(fill);
    toolbar.appendChild(bar);
  }
  root.appendChild(toolbar);

  if (services.length > 1) {
    const batch = document.createElement("div");
    batch.className = "flow-batch";
    services.forEach((item) => {
      const chip = document.createElement("span");
      chip.className = "flow-batch-chip";
      if (item.service_id === focusId) chip.classList.add("is-focus");
      if (item.status === "running") chip.classList.add("is-running");
      else if (item.status === "ok") chip.classList.add("is-ok");
      else if (item.status === "failed") chip.classList.add("is-failed");
      chip.textContent = item.title || item.service_id;
      batch.appendChild(chip);
    });
    root.appendChild(batch);
  }

  const scroll = document.createElement("div");
  scroll.className = "flow-scroll";
  const track = document.createElement("div");
  track.className = "flow-track";

  let lastGroup = "";
  timeline.forEach((item, index) => {
    if (item.group && item.group !== lastGroup) {
      const labels = { prepare: "准备", build: "构建", post: "收尾" };
      track.appendChild(createFlowGroupMarker(labels[item.group] || item.group));
      lastGroup = item.group;
    }
    track.appendChild(createFlowNode(item, activeId));
    if (index < timeline.length - 1) {
      const next = timeline[index + 1];
      const subtasks = item.tier === "main" ? item.step.subtasks : null;
      track.appendChild(createFlowConnector(item.step, next.tier, subtasks));
    }
  });

  scroll.appendChild(track);
  root.appendChild(scroll);

  const artifacts = pipeline.artifacts || {};
  const footParts = [];
  if (artifacts.image) footParts.push("镜像 " + artifacts.image);
  if (artifacts.tag) footParts.push("tag " + artifacts.tag);
  if (artifacts.package_name) footParts.push(artifacts.package_name);
  if (meta.archive_dir) footParts.push("归档 " + meta.archive_dir.split("/").pop());
  const foot = document.createElement("div");
  foot.className = "flow-foot";
  if (footParts.length) {
    foot.textContent = footParts.join(" · ");
  } else {
    foot.textContent = "产物信息将在构建完成后显示";
    foot.classList.add("is-muted");
  }
  if (artifacts.download_url) {
    const link = document.createElement("a");
    link.className = "flow-foot-link";
    link.href = artifacts.download_url;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "下载";
    foot.appendChild(document.createTextNode(" · "));
    foot.appendChild(link);
  }
  root.appendChild(foot);
}

function renderTestResult(job) {
  const root = $("testResult");
  if (!root) return;
  const runs = job && Array.isArray(job.test_runs) && job.test_runs.length
    ? job.test_runs
    : job && job.test_status
      ? [{
          service_id: job.service_id,
          branch: job.branch,
          commit_sha: job.commit_sha,
          status: job.test_status,
          summary: job.test_summary || {},
          commands: job.test_commands || [],
          failures: job.test_failures || [],
          test_cases: job.test_cases || [],
        }]
      : [];
  if (!job || !runs.length) {
    root.hidden = true;
    root.classList.remove("test-result-list");
    root.replaceChildren();
    return;
  }
  if ((job.id || "") !== testResultJobId) {
    testResultJobId = job.id || "";
    testTableState.clear();
  }

  const isBatch = runs.length > 1 || String(job.service_id || "").includes(",");
  root.hidden = false;
  root.classList.toggle("test-result-list", isBatch);
  root.replaceChildren();
  runs.forEach((run, index) => {
    const serviceId = run.service_id || "service-" + (index + 1);
    const target = isBatch ? document.createElement("section") : root;
    if (isBatch) {
      target.className = "test-result-card";
      const serviceHead = document.createElement("div");
      serviceHead.className = "test-service-head";
      serviceHead.textContent = (run.title || serviceId) + (run.branch ? " @ " + run.branch : "");
      target.appendChild(serviceHead);
      root.appendChild(target);
    }
    const stateKey = (job.id || "job") + ":" + serviceId;
    renderTestRun(target, run, stateKey, () => renderTestResult(job));
  });
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

function runQueuedBranchLoads() {
  while (branchLoadsActive < BRANCH_LOAD_LIMIT && branchLoadQueue.length) {
    const queued = branchLoadQueue.shift();
    branchLoadsActive += 1;
    Promise.resolve()
      .then(queued.task)
      .then(queued.resolve, queued.reject)
      .finally(() => {
        branchLoadsActive -= 1;
        runQueuedBranchLoads();
      });
  }
}

function queueBranchLoad(task, priority = false) {
  return new Promise((resolve, reject) => {
    const queued = {task, resolve, reject};
    if (priority) branchLoadQueue.unshift(queued);
    else branchLoadQueue.push(queued);
    runQueuedBranchLoads();
  });
}

function setBranchLoadState(row, state, message = "") {
  const status = row && row.querySelector("[data-branch-status]");
  const retry = row && row.querySelector("[data-branch-retry]");
  if (status) {
    status.textContent = message;
    status.className = "branch-status" + (state ? " " + state : "");
    if (state !== "error") status.removeAttribute("title");
  }
  if (retry) {
    retry.disabled = submitting || state === "loading";
    retry.classList.toggle("loading", state === "loading");
  }
}

async function loadBranches(serviceId, selectEl, defaultBranch, force = false) {
  const row = selectEl.closest(".row-svc");
  if (!force && branchCache[serviceId] && branchCache[serviceId].loaded) {
    const cached = branchCache[serviceId].branches;
    fillBranchSelect(selectEl, cached, defaultBranch);
    setBranchLoadState(row, "ok", "已加载 " + cached.length + " 个分支");
    return;
  }
  selectEl.disabled = true;
  setBranchLoadState(row, "loading", force ? "正在重新加载…" : "正在加载分支…");
  try {
    const suffix = force ? "?refresh=1" : "";
    const data = await queueBranchLoad(
      () => api("/api/services/" + encodeURIComponent(serviceId) + "/branches" + suffix),
      force
    );
    branchCache[serviceId] = { loaded: true, branches: data.branches || [] };
    fillBranchSelect(selectEl, data.branches || [], data.default_branch || defaultBranch);
    setBranchLoadState(row, "ok", "已更新 " + (data.branches || []).length + " 个分支");
  } catch (e) {
    fillBranchSelect(selectEl, [defaultBranch || "main"], defaultBranch || "main");
    delete branchCache[serviceId];
    const detail = (e.data && (e.data.detail || e.data.error)) || e.message || "未知错误";
    setBranchLoadState(row, "error", "分支加载失败，当前仅保留默认 " + (defaultBranch || "main"));
    const status = row && row.querySelector("[data-branch-status]");
    if (status) status.title = String(detail);
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
    const versionInput = row.querySelector("[data-version]");
    const version = ((versionInput && versionInput.value) || "").trim();
    items.push({ service_id: id, branch, title, version, requires_version: !!versionInput });
  });
  return items;
}

function updateBatchUi() {
  const items = selectedItems();
  const btn = $("btnBatch");
  const count = $("selCount");
  if (count) count.textContent = String(items.length);
  if (btn) {
    btn.disabled = submitting || items.length === 0;
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
  document.querySelectorAll("[data-version]").forEach((input) => {
    input.disabled = !!disabled;
  });
  document.querySelectorAll("[data-branch-retry]").forEach((button) => {
    button.disabled = !!disabled || button.classList.contains("loading");
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
    const versionField = s.requires_version
      ? '<label class="version-label">' +
        '<span></span><input class="input version-input" data-version required ' +
        'pattern="v[0-9]+\\.[0-9]+\\.[0-9]+" placeholder="' + (s.version_example || "v1.2.3") + '" />' +
        '</label><div class="version-hint">Daemon版本变化时需要修改版本号，无变化时可不修改</div>'
      : "";
    const runLabel = s.archive_only ? "构建并归档" : "构建并推送";
    row.innerHTML =
      '<label class="svc-pick"><input type="checkbox" data-act="pick" /></label>' +
      "<div>" +
      '<div class="svc-title"></div>' +
      '<div class="svc-meta"></div>' +
      '<div class="branch-row">' +
      '<label class="branch-label">分支 <select class="input branch-select" data-branch></select></label>' +
      '<button type="button" class="branch-retry" data-branch-retry title="重新拉取该微服务的最新分支" aria-label="重新拉取该微服务的最新分支">↻</button>' +
      '<span class="branch-status" data-branch-status></span>' +
      '</div>' +
      versionField +
      "</div>" +
      '<div class="svc-actions"><button type="button" class="btn primary" data-act="run"></button></div>';
    row.querySelector(".svc-title").textContent = s.title;
    row.querySelector(".svc-meta").textContent = (s.repo || s.github || "") + (s.archive_only ? " · 仅生成归档，不推送 SWR" : "");
    const versionLabel = row.querySelector(".version-label span");
    if (versionLabel) versionLabel.textContent = (s.version_label || "Version") + "（例如 " + (s.version_example || "v1.2.3") + "）";
    const versionInput = row.querySelector("[data-version]");
    if (versionInput) {
      versionInput.value = s.last_version || rememberedDaemonVersion();
      rememberDaemonVersion(versionInput.value);
      versionInput.addEventListener("input", () => {
        versionInput.setCustomValidity("");
        rememberDaemonVersion(versionInput.value.trim());
      });
    }
    const select = row.querySelector("[data-branch]");
    fillBranchSelect(select, [s.default_branch || "main"], s.default_branch || "main");
    row.querySelector("[data-branch-retry]").addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      loadBranches(s.id, select, s.default_branch, true);
    });
    setTimeout(() => loadBranches(s.id, select, s.default_branch), 30);
    row.querySelector('[data-act="pick"]').addEventListener("change", updateBatchUi);
    const btn = row.querySelector('[data-act="run"]');
    btn.textContent = runLabel;
    btn.dataset.defaultLabel = runLabel;
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const branch = (select.value || s.default_branch || "main").trim();
      const versionInput = row.querySelector("[data-version]");
      pushItems([{
        service_id: s.id,
        branch,
        title: s.title,
        version: ((versionInput && versionInput.value) || "").trim(),
        requires_version: !!versionInput,
      }], btn);
    });
    root.appendChild(row);
  }
  updateBatchUi();
  if (submitting) setButtonsDisabled(true);
}

function rememberLastJob(jobId, summary) {
  try {
    const observedAt = Date.now();
    localStorage.setItem(LAST_JOB_KEY, JSON.stringify({ id: jobId, summary, observed_at: observedAt }));
    localStorage.removeItem(LEGACY_ACTIVE_JOB_KEY);
    scheduleLastJobExpiry(jobId, observedAt, LAST_JOB_TTL_MS);
  } catch (_) { /* storage may be disabled */ }
}

function clearStoredLastJob() {
  try {
    localStorage.removeItem(LAST_JOB_KEY);
    localStorage.removeItem(LEGACY_ACTIVE_JOB_KEY);
  } catch (_) { /* storage may be disabled */ }
}

function scheduleLastJobExpiry(jobId, observedAt, delay) {
  if (lastJobExpiryTimer) clearTimeout(lastJobExpiryTimer);
  lastJobExpiryTimer = setTimeout(() => {
    lastJobExpiryTimer = null;
    try {
      const current = JSON.parse(localStorage.getItem(LAST_JOB_KEY) || "null");
      if (current && current.id === jobId && Number(current.observed_at) === observedAt) {
        clearStoredLastJob();
      }
    } catch (_) {
      clearStoredLastJob();
    }
  }, Math.max(0, delay));
}

function rememberedLastJob() {
  try {
    const raw = localStorage.getItem(LAST_JOB_KEY) || localStorage.getItem(LEGACY_ACTIVE_JOB_KEY);
    const value = JSON.parse(raw || "null");
    const observedAt = Number(value && value.observed_at);
    const age = Date.now() - observedAt;
    if (!value || !value.id || !observedAt || age < 0 || age >= LAST_JOB_TTL_MS) {
      clearStoredLastJob();
      return null;
    }
    scheduleLastJobExpiry(value.id, observedAt, LAST_JOB_TTL_MS - age);
    return value;
  } catch (_) {
    clearStoredLastJob();
    return null;
  }
}

function stopPolling() {
  pollGeneration += 1;
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
}

function stopActiveProbe() {
  if (activeProbeTimer) clearTimeout(activeProbeTimer);
  activeProbeTimer = null;
}

function resetJobView() {
  liveLogLines = [];
  logCursor = 0;
  testRevision = -1;
  cachedTestRuns = [];
  pollFails = 0;
  resetLogFollow(liveLogFollow);
}

function scheduleActiveProbe(delay = 1500) {
  stopActiveProbe();
  // Only watch THIS browser's jobs; never steal another person's build stream.
  activeProbeTimer = setTimeout(async () => {
    activeProbeTimer = null;
    try {
      const data = await api("/api/running-jobs?client_id=" + encodeURIComponent(clientId()));
      const jobs = (data && data.jobs) || [];
      jobs.forEach((job) => upsertMyJob(job.id, job.service_id || "任务", job.status || "running"));
      if (!activeJobId) {
        const first = jobs[0];
        if (first && first.id) {
          startPolling(first.id, first.service_id || "任务");
          return;
        }
      }
    } catch (_) {
      /* ignore transient probe failures */
    }
    scheduleActiveProbe();
  }, delay);
}

function startPolling(jobId, summary) {
  stopActiveProbe();
  stopPolling();
  const generation = pollGeneration;
  activeJobId = jobId;
  resetJobView();
  busy = true;
  // Concurrent: keep build buttons usable so this page can start another job.
  setButtonsDisabled(false);
  updateBatchUi();
  upsertMyJob(jobId, summary, "running");
  rememberLastJob(jobId, summary);
  setLiveStopVisible(true, false);
  showJob(summary + " · job " + jobId, "Connecting to active job…");

  const tick = async () => {
    const keepPolling = await pollJob(jobId, summary, generation, true);
    if (keepPolling && generation === pollGeneration) {
      pollTimer = setTimeout(tick, 1500);
    }
  };
  tick();
}

async function restoreLastJob(saved) {
  if (!saved || !saved.id || activeJobId) return;
  stopPolling();
  const generation = pollGeneration;
  resetJobView();
  const summary = saved.summary || "最近任务";
  showJob(summary + " · 恢复最近任务…", "Loading saved job…");
  const stillRunning = await pollJob(saved.id, summary, generation);
  if (stillRunning && generation === pollGeneration) startPolling(saved.id, summary);
}

async function pushItems(items, btn) {
  if (submitting) {
    showJob("正在提交上一个请求，请稍候");
    return;
  }
  if (!items || !items.length) {
    showJob("请先勾选要构建的微服务");
    return;
  }
  const invalidVersion = items.find((item) => item.requires_version && !DAEMON_VERSION_PATTERN.test(item.version || ""));
  if (invalidVersion) {
    showJob(
      invalidVersion.title + " · 版本格式错误",
      "Version is required and must match vMAJOR.MINOR.PATCH, for example v1.2.3"
    );
    const row = document.querySelector('.row-svc[data-id="' + invalidVersion.service_id + '"]');
    const input = row && row.querySelector("[data-version]");
    if (input) { input.focus(); input.setCustomValidity("请输入类似 v1.2.3 的版本号"); input.reportValidity(); }
    return;
  }
  items.filter((item) => item.requires_version).forEach((item) => rememberDaemonVersion(item.version));
  document.querySelectorAll("[data-version]").forEach((input) => input.setCustomValidity(""));
  submitting = true;
  stopActiveProbe();
  if (btn) btn.textContent = "提交中…";
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
        client_id: clientId(),
        items: items.map((it) => ({ service_id: it.service_id, branch: it.branch, version: it.version || "" })),
        login_command: loginCmd,
      }),
    });
    startPolling(resp.job_id, summary);
  } catch (e) {
    const msg = (e.data && (e.data.detail || e.data.error)) || e.message;
    showJob(summary + " · 未能启动", String(msg));
    scheduleActiveProbe();
  } finally {
    submitting = false;
    if (btn) btn.textContent = btn.dataset.defaultLabel || "构建并推送";
    document.querySelectorAll('[data-act="run"]').forEach((b) => (b.textContent = b.dataset.defaultLabel || "构建并推送"));
    setButtonsDisabled(false);
    updateBatchUi();
  }
}

async function pollJob(jobId, summary, generation = pollGeneration, trackRecent = false) {
  try {
    const query = new URLSearchParams({
      compact: "1",
      view: "ui",
      log_after: String(logCursor),
      test_revision: String(testRevision),
    });
    const job = await api("/api/jobs/" + jobId + "?" + query.toString());
    if (generation !== pollGeneration) return false;
    if (trackRecent) rememberLastJob(jobId, summary);
    pollFails = 0;
    if (Array.isArray(job.log) && job.log.length) liveLogLines.push(...job.log);
    logCursor = Number.isFinite(Number(job.log_cursor)) ? Number(job.log_cursor) : liveLogLines.length;
    if (Array.isArray(job.test_runs)) cachedTestRuns = job.test_runs;
    if (Number.isFinite(Number(job.test_revision))) testRevision = Number(job.test_revision);

    const renderJob = { ...job, test_runs: cachedTestRuns };
    const logEl = $("jobLog");
    if (logEl) {
      paintLog(logEl, displayJobLog(liveLogLines), liveLogFollow);
    }
    renderTestResult(renderJob);
    renderJobPipeline("liveJobPipeline", job.pipeline, job.status);
    const progress = job.progress ? " · " + job.progress : "";
    const current = job.current ? " · " + job.current : "";
    const stage = job.stage ? " · " + job.stage : "";
    if (job.status === "running" || job.status === "unknown") {
      upsertMyJob(jobId, summary, "running");
      setLiveStopVisible(true, job.stage === "stopping");
      $("jobMeta").textContent =
        summary +
        (job.stage === "stopping" ? " · 正在停止" : " · 进行中") +
        progress +
        current +
        stage;
      return true;
    }

    const finalStatus = job.status === "ok" ? "ok" : job.status === "stopped" ? "stopped" : "failed";
    upsertMyJob(jobId, summary, finalStatus);
    setLiveStopVisible(false, false);
    if (activeJobId === jobId) activeJobId = "";
    busy = false;
    setButtonsDisabled(false);
    document.querySelectorAll('[data-act="run"]').forEach((b) => (b.textContent = b.dataset.defaultLabel || "构建并推送"));
    updateBatchUi();
    const archiveHint = job.archive_dir || job.archive || "";
    if (job.status === "ok") {
      $("jobMeta").textContent =
        summary +
        " · 成功" +
        (archiveHint ? " · 归档 " + archiveHint : "") +
        (job.remote ? " · " + job.remote : "");
      refreshArtifacts();
    } else if (job.status === "stopped") {
      $("jobMeta").textContent = summary + " · 已停止";
    } else {
      $("jobMeta").textContent =
        summary +
        " · 失败 · " +
        (job.error || "") +
        (archiveHint ? " · 归档 " + archiveHint : "");
    }
    // Prefer next running job owned by this browser.
    const next = myJobs.find((j) => j.status === "running" && j.id !== jobId);
    if (next) {
      startPolling(next.id, next.summary || next.id);
      return false;
    }
    scheduleActiveProbe();
    return false;
  } catch (e) {
    if (generation !== pollGeneration) return false;
    pollFails += 1;
    if (pollFails < 80) {
      if ($("jobMeta")) {
        $("jobMeta").textContent = summary + " · 网络抖动，重连中 (" + pollFails + ")";
      }
      return true;
    }
    if (activeJobId === jobId) activeJobId = "";
    busy = false;
    setLiveStopVisible(false, false);
    setButtonsDisabled(false);
    updateBatchUi();
    showJob("轮询失败", e.message || String(e));
    scheduleActiveProbe();
    return false;
  }
}

function bind(id, fn) {
  const el = $(id);
  if (el) el.addEventListener("click", fn);
}

function updateHwCredStatus() {
  const el = $("hwCredStatus");
  if (!el) return;
  if (hwCloudCreds && hwCloudCreds.access_key && hwCloudCreds.secret_key) {
    el.textContent = "华为云凭证：已启用（仅当前页面，" + (hwCloudCreds.region || "—") + "）";
    el.style.color = "var(--ok)";
  } else {
    el.textContent = "华为云凭证：未设置";
    el.style.color = "";
  }
}

function setCceControlsEnabled(enabled) {
  ["btnCceRefreshClusters", "cceClusterSelect", "btnCceRefreshWorkloads"].forEach((id) => {
    const el = $(id);
    if (el) el.disabled = !enabled;
  });
}

function clearHwCloudCreds() {
  hwCloudCreds = null;
  const sk = $("hwSecretKey");
  if (sk) sk.value = "";
  updateHwCredStatus();
  setCceControlsEnabled(false);
  const clusterSelect = $("cceClusterSelect");
  if (clusterSelect) {
    clusterSelect.innerHTML = '<option value="">— 先刷新集群 —</option>';
    clusterSelect.disabled = true;
  }
  const body = $("cceWorkloadsBody");
  if (body) {
    body.innerHTML = '<tr><td colspan="6" class="hint">设置凭证并选择集群后刷新负载。</td></tr>';
  }
  const detail = $("cceWorkloadDetail");
  if (detail) {
    detail.hidden = true;
    detail.textContent = "";
  }
  const log = $("cceLog");
  if (log) log.textContent = "";
}

function applyHwCloudCreds() {
  const accessKey = ($("hwAccessKey") && $("hwAccessKey").value || "").trim();
  const secretKey = ($("hwSecretKey") && $("hwSecretKey").value || "").trim();
  const region = ($("hwRegion") && $("hwRegion").value || "").trim();
  const projectId = ($("hwProjectId") && $("hwProjectId").value || "").trim();
  if (!accessKey || !secretKey) {
    throw new Error("请填写 Access Key 和 Secret Key");
  }
  if (!region) {
    throw new Error("请选择 Region");
  }
  hwCloudCreds = {
    access_key: accessKey,
    secret_key: secretKey,
    region,
    project_id: projectId,
  };
  if ($("hwSecretKey")) $("hwSecretKey").value = "";
  updateHwCredStatus();
  setCceControlsEnabled(true);
  const log = $("cceLog");
  if (log) log.textContent = "凭证已启用（SK 已从输入框清除，仍保留在页面内存）。可刷新集群列表。";
}

function requireHwCloudCreds() {
  if (!hwCloudCreds || !hwCloudCreds.access_key || !hwCloudCreds.secret_key) {
    throw new Error("请先在左侧点击「启用本会话凭证」");
  }
  return hwCloudCreds;
}

async function hwCcePost(path, extra) {
  const creds = requireHwCloudCreds();
  return api(path, {
    method: "POST",
    body: JSON.stringify({
      access_key: creds.access_key,
      secret_key: creds.secret_key,
      region: creds.region,
      project_id: creds.project_id || "",
      ...(extra || {}),
    }),
  });
}

function formatCceError(err) {
  let msg = String((err && err.message) || err || "unknown error");
  const data = err && err.data;
  if (data && data.detail) {
    const detail =
      typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail, null, 2);
    msg += "\n" + detail;
  }
  return msg;
}

function setCceLog(text) {
  const el = $("cceLog");
  if (el) el.textContent = text || "";
}

async function initCceView() {
  const select = $("hwRegion");
  if (!select || cceRegionsLoaded) {
    updateHwCredStatus();
    setCceControlsEnabled(!!hwCloudCreds);
    return;
  }
  const data = await api("/api/cce/regions");
  const regions = (data && data.regions) || [];
  select.innerHTML = regions
    .map(
      (item) =>
        `<option value="${esc(item.id)}">${esc(item.label || item.id)} (${esc(item.id)})</option>`
    )
    .join("");
  if (!select.value && regions.length) {
    const preferred = regions.find((r) => r.id === "cn-southwest-2") || regions[0];
    select.value = preferred.id;
  }
  cceRegionsLoaded = true;
  updateHwCredStatus();
  setCceControlsEnabled(!!hwCloudCreds);
}

async function refreshCceClusters() {
  setCceLog("正在加载集群…");
  const data = await hwCcePost("/api/cce/clusters");
  const clusters = (data && data.clusters) || [];
  const select = $("cceClusterSelect");
  if (!select) return;
  if (!clusters.length) {
    select.innerHTML = '<option value="">（无集群）</option>';
    setCceLog("未找到集群。");
    return;
  }
  select.innerHTML = clusters
    .map((item) => {
      const label = [item.name, item.status, item.version].filter(Boolean).join(" · ");
      return `<option value="${esc(item.id)}">${esc(label || item.id)}</option>`;
    })
    .join("");
  select.disabled = false;
  setCceLog(
    "已加载 " +
      clusters.length +
      " 个集群（project: " +
      (data.project_name || data.project_id || "—") +
      "）。"
  );
}

function selectedCceClusterId() {
  const select = $("cceClusterSelect");
  return select ? String(select.value || "").trim() : "";
}

function renderCceWorkloads(rows) {
  const body = $("cceWorkloadsBody");
  if (!body) return;
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="6" class="hint">该命名空间下无 Deployment。</td></tr>';
    return;
  }
  body.innerHTML = rows
    .map((item) => {
      const images = Array.isArray(item.images) ? item.images.join(", ") : "—";
      return (
        `<tr>` +
        `<td class="mono">${esc(item.name || "—")}</td>` +
        `<td>${esc(item.replicas ?? "—")}</td>` +
        `<td>${esc(item.ready_replicas ?? "—")}</td>` +
        `<td class="mono">${esc(images)}</td>` +
        `<td>${esc(item.strategy || "—")}</td>` +
        `<td><button type="button" class="btn ghost cce-detail-btn" data-name="${esc(item.name || "")}">详情</button></td>` +
        `</tr>`
      );
    })
    .join("");
  body.querySelectorAll(".cce-detail-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const name = btn.getAttribute("data-name") || "";
      loadCceWorkloadDetail(name).catch((e) => setCceLog(formatCceError(e)));
    });
  });
}

async function refreshCceWorkloads() {
  const clusterId = selectedCceClusterId();
  if (!clusterId) {
    throw new Error("请先选择集群");
  }
  const namespace = ($("cceNamespace") && $("cceNamespace").value || "default").trim() || "default";
  setCceLog("正在加载负载…");
  const data = await hwCcePost("/api/cce/workloads", {
    cluster_id: clusterId,
    namespace,
    kind: "deployments",
  });
  renderCceWorkloads((data && data.workloads) || []);
  setCceLog("已加载 " + ((data && data.count) || 0) + " 个负载（" + namespace + "）。");
  const detail = $("cceWorkloadDetail");
  if (detail) {
    detail.hidden = true;
    detail.textContent = "";
  }
}

async function loadCceWorkloadDetail(name) {
  const clusterId = selectedCceClusterId();
  if (!clusterId) {
    throw new Error("请先选择集群");
  }
  const namespace = ($("cceNamespace") && $("cceNamespace").value || "default").trim() || "default";
  setCceLog("正在加载 " + name + " 详情…");
  const data = await hwCcePost("/api/cce/workloads/detail", {
    cluster_id: clusterId,
    namespace,
    kind: "deployments",
    name,
  });
  const detail = $("cceWorkloadDetail");
  if (detail) {
    detail.hidden = false;
    detail.textContent = JSON.stringify(data, null, 2);
  }
  setCceLog("已加载负载详情：" + name);
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
bind("btnRefreshArtifacts", () => {
  refreshArtifacts().catch(() => {});
});
bind("btnRefreshHistory", () => {
  refreshHistory().catch(() => {});
});
bind("btnHistoryBack", () => {
  closeHistoryDetail();
});
bind("btnStopJob", () => {
  if (!activeJobId) return;
  stopRunningJob(activeJobId, {
    button: $("btnStopJob"),
    onStopping: () => {
      if ($("jobMeta")) $("jobMeta").textContent = "正在停止任务…";
    },
  });
});
bind("btnHistoryStop", () => {
  if (!historyDetailJobId) return;
  stopRunningJob(historyDetailJobId, {
    button: $("btnHistoryStop"),
    onStopping: () => {
      if ($("historyDetailMeta")) $("historyDetailMeta").textContent = "正在停止任务…";
    },
  });
});

bind("btnHwApplyCreds", () => {
  try {
    applyHwCloudCreds();
  } catch (e) {
    updateHwCredStatus();
    const el = $("hwCredStatus");
    if (el) {
      el.textContent = String(e.message || e);
      el.style.color = "var(--danger)";
    }
  }
});
bind("btnHwClearCreds", () => clearHwCloudCreds());
const hwRegionEl = $("hwRegion");
if (hwRegionEl) {
  hwRegionEl.addEventListener("change", () => {
    if (hwCloudCreds) {
      hwCloudCreds.region = String(hwRegionEl.value || "").trim();
      updateHwCredStatus();
    }
  });
}
bind("btnCceRefreshClusters", () => {
  refreshCceClusters().catch((e) => setCceLog(formatCceError(e)));
});
bind("btnCceRefreshWorkloads", () => {
  refreshCceWorkloads().catch((e) => setCceLog(formatCceError(e)));
});

window.addEventListener("beforeunload", () => {
  hwCloudCreds = null;
});

document.querySelectorAll(".view-tab").forEach((btn) => {
  btn.addEventListener("click", () => setMainView(btn.dataset.view || "services"));
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
  busy = true;
  setButtonsDisabled(true);
  loadMyJobs();
  let rememberedView = "services";
  try {
    rememberedView = localStorage.getItem("robotCiMainView") || "services";
  } catch (_) {
    rememberedView = "services";
  }
  setMainView(rememberedView);
  initCceView().catch(() => {});
  showJob("Checking active jobs…", "Connecting to server…");
  try {
    const pageReady = Promise.all([refreshHealth(), refreshServices(), refreshArtifacts()]);
    let mine = [];
    try {
      const data = await api("/api/running-jobs?client_id=" + encodeURIComponent(clientId()));
      mine = (data && data.jobs) || [];
    } catch (e) {
      await pageReady;
      showJob("Unable to check active jobs", String(e.message || e));
      return;
    }
    await pageReady;

    mine.forEach((job) => upsertMyJob(job.id, job.service_id || "任务", "running"));
    if (mine.length) {
      startPolling(mine[0].id, mine[0].service_id || "任务");
      return;
    }

    busy = false;
    setButtonsDisabled(false);
    updateBatchUi();
    scheduleActiveProbe();

    const saved = rememberedLastJob();
    if (saved) {
      restoreLastJob(saved);
      return;
    }

    showJob(
      "就绪",
      "可同时发起多个构建。任务日志只显示当前这一次；历史记录在「构建历史」里分页查看。\n可勾选多个微服务后点「构建所选」，或点单行「构建并推送」。"
    );
  } catch (e) {
    $("serviceTable").innerHTML = '<p class="hint">无法连接助手，请先运行 start.bat</p>';
    showJob("助手未连接", String(e.message || e));
  }
})();

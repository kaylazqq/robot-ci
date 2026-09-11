const $ = (id) => document.getElementById(id);
const DAEMON_VERSION_KEY = "robotCiDaemonVersion";
const DAEMON_VERSION_PATTERN = /^v[0-9]+\.[0-9]+\.[0-9]+$/;
const SERVICE_KEY = "robotCiService";
const ALL_SERVICES_ID = "all";
const LOG_FOLLOW_SLOP_PX = 48;

let currentUser = "";
let nav = "build";
let tab = "pipeline";
let services = [];
let favoriteServiceIds = [];
let currentServiceId = "";
let currentJobId = "";
let currentJob = null;
let pinnedJobId = "";
let serviceActiveJobId = "";
let pollTimer = null;
let pollGeneration = 0;
let viewGeneration = 0;
let sessionLive = false;
let logCursor = 0;
let submitting = false;
let environments = [];
let envDraftOpen = false;
let envEditingId = "";
let runPreviewActive = false;
let runPreviewSelection = { deploy: false, test: false, environmentId: "" };
let pipelineTemplates = [];
let selectedTemplateId = "";
let runLayerMode = "run";
let runLayerTemplateId = "";
let cloneSourceId = "";
let previewBranchPicker = null;
let previewBranchLoadGeneration = 0;
let svcMenuPage = 1;
const SVC_PAGE_SIZE = 8;
const branchCache = {};
const branchLoads = {};
const BRANCH_LOAD_LIMIT = 3;
const branchLoadQueue = [];
let branchLoadsActive = 0;
const testTableState = new Map();
const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];
const artifactsPager = { page: 1, pageSize: 10 };
const historyPager = { page: 1, pageSize: 10 };
const stepLogFollow = { dragging: false, follow: true, pendingText: null, flush: null };

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
  const onDown = () => { state.dragging = true; };
  const onUp = () => {
    if (!state.dragging) return;
    state.dragging = false;
    state.follow = isLogNearBottom(el);
    if (typeof state.flush === "function") state.flush();
  };
  el.addEventListener("pointerdown", onDown);
  window.addEventListener("pointerup", onUp);
  el.addEventListener("scroll", () => {
    if (state.dragging) { state.follow = false; return; }
    state.follow = isLogNearBottom(el);
  });
}
function paintLog(el, text, state) {
  if (!el) return;
  if (state && !state.follow) {
    state.pendingText = text;
    state.flush = () => {
      if (state.follow && state.pendingText != null) {
        el.textContent = state.pendingText;
        el.scrollTop = el.scrollHeight;
        state.pendingText = null;
      }
    };
    return;
  }
  el.textContent = text;
  el.scrollTop = el.scrollHeight;
}

function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function clipTd(text, className) {
  const raw = text == null ? "" : String(text);
  const shown = raw || "—";
  const title = raw ? ' title="' + esc(raw) + '"' : "";
  return '<td class="' + className + '"' + title + '><span class="cell-clip-text">' + esc(shown) + "</span></td>";
}
const REFRESH_BUSY_MS = 360;
function beginRefresh(btn) {
  if (!btn) return;
  btn._refreshCount = (btn._refreshCount || 0) + 1;
  if (btn._refreshCount > 1) return;
  btn.classList.add("loading");
  btn.setAttribute("aria-busy", "true");
  const label = btn.querySelector("span");
  if (label) {
    if (label.dataset.idleLabel == null) label.dataset.idleLabel = label.textContent;
    label.textContent = "刷新中";
  }
}
function endRefresh(btn) {
  if (!btn) return;
  btn._refreshCount = Math.max(0, (btn._refreshCount || 1) - 1);
  if (btn._refreshCount) return;
  btn.classList.remove("loading");
  btn.setAttribute("aria-busy", "false");
  const label = btn.querySelector("span");
  if (label && label.dataset.idleLabel != null) label.textContent = label.dataset.idleLabel;
}
async function withRefresh(btn, work) {
  beginRefresh(btn);
  const started = Date.now();
  try {
    await work();
  } finally {
    const wait = REFRESH_BUSY_MS - (Date.now() - started);
    if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
    endRefresh(btn);
  }
}
function shortSha(sha) {
  const value = String(sha || "").trim();
  return value.length > 12 ? value.slice(0, 12) : value || "—";
}
function rememberedDaemonVersion() {
  try { return localStorage.getItem(DAEMON_VERSION_KEY) || ""; } catch (_) { return ""; }
}
function rememberDaemonVersion(value) {
  try { localStorage.setItem(DAEMON_VERSION_KEY, value || ""); } catch (_) {}
}

function isPublicAuthPath(path) {
  return path === "/api/auth/login" || path === "/api/auth/me" || path === "/api/auth/logout";
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !isPublicAuthPath(path)) {
    if (sessionLive) showLogin("登录已过期，请重新登录");
    const err = new Error("请先登录");
    err.status = 401;
    err.data = data;
    throw err;
  }
  if (!res.ok) {
    const err = new Error(data.error || data.detail || data.message || res.statusText);
    err.data = data;
    err.status = res.status;
    throw err;
  }
  return data;
}

function isAllServices() {
  return currentServiceId === ALL_SERVICES_ID;
}
function isKnownServiceId(id) {
  return !!id && (id === ALL_SERVICES_ID || services.some((item) => item.id === id));
}
function currentService() {
  if (isAllServices()) return null;
  return services.find((item) => item.id === currentServiceId) || null;
}
function serviceQuery() {
  if (!currentServiceId || isAllServices()) return "";
  return "&service_id=" + encodeURIComponent(currentServiceId);
}
function jobServiceIds(job) {
  if (!job) return [];
  if (Array.isArray(job.service_ids) && job.service_ids.length) {
    return job.service_ids.map((id) => String(id || "").trim()).filter(Boolean);
  }
  return String(job.service_id || "").split(",").map((id) => id.trim()).filter(Boolean);
}
function jobBelongsToService(job, serviceId) {
  if (!job || !serviceId || serviceId === ALL_SERVICES_ID) return false;
  return jobServiceIds(job).includes(serviceId);
}
function viewIsCurrent(gen, serviceId) {
  return sessionLive && gen === viewGeneration && serviceId === currentServiceId;
}

function setPill(el, text, cls) {
  if (!el) return;
  el.textContent = text;
  el.className = "pill" + (cls ? " " + cls : "");
}

function endSession() {
  sessionLive = false;
  stopPolling();
  viewGeneration += 1;
  branchLoadQueue.length = 0;
  currentJob = null;
  currentJobId = "";
  favoriteServiceIds = [];
  closeModal();
}

function showLogin(message) {
  if (sessionLive) endSession();
  cancelRunPreview();
  closeCloneWindow();
  closeEnvConfirm(false);
  closeEnvOverlay();
  $("appShell").hidden = true;
  $("loginGate").hidden = false;
  if ($("loginError")) {
    if (message) {
      $("loginError").hidden = false;
      $("loginError").textContent = message;
    } else {
      $("loginError").hidden = true;
      $("loginError").textContent = "";
    }
  }
}

function enterApp(username) {
  sessionLive = true;
  currentUser = username;
  if ($("loginGate")) $("loginGate").hidden = true;
  if ($("appShell")) $("appShell").hidden = false;
  if ($("userNameLabel")) $("userNameLabel").textContent = username;
  if ($("userAvatar")) $("userAvatar").textContent = String(username).slice(0, 1).toUpperCase();
}

function bindEyeButtons(root) {
  (root || document).querySelectorAll("[data-eye]").forEach((btn) => {
    if (btn.dataset.bound === "1") return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => {
      const input = (root || document).querySelector("#" + btn.getAttribute("data-eye"));
      if (!input) return;
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.classList.toggle("is-on", show);
      btn.setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
    });
  });
}
function passwordFieldHtml(id, label, autocomplete) {
  return (
    '<label class="label" for="' + id + '">' + label + "</label>" +
    '<div class="field-wrap">' +
    '<input id="' + id + '" class="input has-eye" type="password" autocomplete="' + autocomplete + '" />' +
    '<button type="button" class="eye-btn" data-eye="' + id + '" aria-label="显示密码">' +
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2.8 12S6.2 6.8 12 6.8 21.2 12 21.2 12 17.8 17.2 12 17.2 2.8 12 2.8 12z" fill="none" stroke="currentColor" stroke-width="1.7"/><circle cx="12" cy="12" r="2.4" fill="none" stroke="currentColor" stroke-width="1.7"/></svg>' +
    "</button></div>"
  );
}

function closeUserMenu() {
  const menu = $("userMenu");
  if (menu) menu.hidden = true;
  const btn = $("btnUserMenu");
  if (btn) btn.setAttribute("aria-expanded", "false");
}

function isBuildListTab(name) {
  return name === "history" || name === "artifacts" || name === "envs";
}
function normalizeBuildTab(next) {
  return isBuildListTab(next) ? next : "pipeline";
}
function allServicesFallbackTab(next) {
  if (next === "artifacts" || next === "envs") return next;
  return "history";
}

function setNav(next, skipHash) {
  nav = next === "swr" ? "swr" : "build";
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.nav === nav);
  });
  if ($("pageSwr")) $("pageSwr").hidden = nav !== "swr";
  if (nav !== "build" || tab !== "envs") {
    closeEnvConfirm(false);
    closeEnvOverlay();
  }
  if (nav !== "build") {
    cancelRunPreview();
    closeCloneWindow();
  }
  applyServiceChrome();
  renderFavoriteServices();
  if (!skipHash) writeHash();
}

function applyServiceChrome() {
  const all = isAllServices();
  document.querySelectorAll(".history-table, .artifacts-table").forEach((table) => {
    table.classList.toggle("show-service-column", all);
  });
  const pipeBtn = document.querySelector('.subtab[data-tab="pipeline"]');
  if (pipeBtn) pipeBtn.hidden = all;
  if ($("pageBuild")) $("pageBuild").hidden = nav !== "build";
  if (!$("viewPipeline")) return;
  $("viewPipeline").hidden = tab !== "pipeline";
  if ($("viewHistory")) $("viewHistory").hidden = tab !== "history";
  if ($("viewArtifacts")) $("viewArtifacts").hidden = tab !== "artifacts";
  if ($("viewEnvs")) $("viewEnvs").hidden = tab !== "envs";
  if ($("pipelineActions")) $("pipelineActions").hidden = all || tab !== "pipeline";
  if ($("jobMetaBar")) $("jobMetaBar").hidden = all || tab !== "pipeline";
  const createBtn = $("btnEnvCreate");
  if (createBtn) createBtn.hidden = all;
  renderFavoriteServices();
}

function jobIsLive(job) {
  return !!(job && (job.status === "running" || job.status === "queued"));
}

function setTab(next) {
  if (!sessionLive) return;
  if (isAllServices() && next !== "history" && next !== "artifacts" && next !== "envs") next = "history";
  const nextTab = normalizeBuildTab(next);
  const changed = tab !== nextTab;
  tab = nextTab;
  document.querySelectorAll(".subtab[data-tab]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
  applyServiceChrome();
  renderFavoriteServices();
  if (tab !== "envs") {
    closeEnvConfirm(false);
    closeEnvOverlay();
  }
  if (changed) {
    if (tab === "history") refreshHistory().catch(() => {});
    if (tab === "artifacts") refreshArtifacts().catch(() => {});
    if (tab === "envs") loadEnvironments().catch((e) => setEnvError(e.message));
    if (tab === "pipeline" && !isAllServices()) {
      loadPipelineTemplates().catch(() => {});
      if (pinnedJobId) {
        if (currentJobId !== pinnedJobId) openJob(pinnedJobId).catch(() => {});
      } else {
        loadServiceJob();
      }
    }
  }
  writeHash();
}

function writeHash() {
  let next;
  if (nav !== "build") {
    next = "#/" + nav;
  } else {
    const svc = encodeURIComponent(currentServiceId || "");
    if (tab === "pipeline" && pinnedJobId && !isAllServices()) {
      next = "#/build/" + svc + "/job/" + encodeURIComponent(pinnedJobId);
    } else if (tab === "pipeline") {
      next = "#/build/" + svc;
    } else {
      next = "#/build/" + svc + "/" + tab;
    }
  }
  if (location.hash === next) return;
  location.hash = next;
}

function readHash() {
  const raw = (location.hash || "").replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean);
  if (parts[0] === "cce" || parts[0] === "envs") return { nav: "build", service: "", tab: "envs", job: "" };
  if (parts[0] === "swr") return { nav: "swr", service: "", tab: "pipeline", job: "" };
  if (parts[0] === "run") {
    return { nav: "run", service: decodeURIComponent(parts[1] || ""), tab: "pipeline", job: "" };
  }
  const service = decodeURIComponent(parts[1] || "");
  let nextTab = "pipeline";
  let job = "";
  if (parts[2] === "history" || parts[2] === "artifacts" || parts[2] === "envs") nextTab = parts[2];
  else if (parts[2] === "job" && parts[3]) job = decodeURIComponent(parts[3]);
  return { nav: "build", service, tab: nextTab, job };
}

function formatClock(value) {
  return String(value || "").trim() || "—";
}
function formatDuration(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 1000) return Math.round(n) + "ms";
  const sec = Math.round(n / 1000);
  if (sec < 60) return sec + "s";
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m < 60) return m + "m" + String(s).padStart(2, "0") + "s";
  return Math.floor(m / 60) + "h" + String(m % 60).padStart(2, "0") + "m";
}
function formatDurationSec(sec) {
  if (sec == null || sec === "") return "—";
  return formatDuration(Number(sec) * 1000);
}

function updateActionButtons() {
  const svc = currentService();
  const live = jobIsLive(currentJob);
  const stopping = !!(currentJob && (currentJob.cancel_requested || currentJob.stage === "stopping"));
  if ($("btnRun")) $("btnRun").disabled = !svc || !!serviceActiveJobId || runPreviewActive || cloneLayerOpen();
  const tpl = selectedPipelineTemplate();
  const tplBusy = !svc || runPreviewActive || cloneLayerOpen();
  if ($("tplTrigger")) $("tplTrigger").disabled = tplBusy;
  if ($("btnTplEdit")) $("btnTplEdit").disabled = tplBusy || !tpl;
  if ($("btnTplCopy")) $("btnTplCopy").disabled = tplBusy || !tpl;
  if ($("btnTplDelete")) {
    $("btnTplDelete").disabled = tplBusy || !tpl || !!(tpl && tpl.builtin);
    $("btnTplDelete").hidden = !!(tpl && tpl.builtin) || !tpl;
  }
  if ($("btnStop")) $("btnStop").disabled = !live || stopping;
  if ($("btnParams")) $("btnParams").disabled = !(svc && svc.requires_version);
}

function renderJobMeta(job) {
  if (!$("metaBranch")) return;
  const live = jobIsLive(job);
  $("metaBranch").textContent = (job && job.branch) || "—";
  $("metaCommit").textContent = shortSha(job && job.commit_sha);
  $("metaOperator").textContent = (job && job.operator) || "—";
  $("metaStarted").textContent = formatClock(job && job.created_at);
  $("metaFinishedWrap").hidden = !job || live || !job.finished_at;
  $("metaDurationWrap").hidden = !job || live || job.duration_sec == null;
  $("metaFinished").textContent = formatClock(job && job.finished_at);
  $("metaDuration").textContent = formatDurationSec(job && job.duration_sec);
}

const PIPELINE_STATE_LABELS = {
  pending: "等待",
  queued: "排队中",
  running: "进行中",
  done: "成功",
  failed: "失败",
  warn: "失败",
  skipped: "跳过",
};
const PIPELINE_TASK_LABELS = {
  env: "检查环境",
  slot: "等待并发槽位",
  wait: "排队等待",
  acquire: "获得执行槽",
  swr: "SWR登录",
  ps: "依赖仓库",
  adir: "归档目录",
  clone: "克隆仓库",
  sha: "记录提交",
  plan: "加载build.yaml",
  runner: "启动测试",
  "ut-cases": "执行UT",
  "dt-cases": "执行DT",
  deps: "准备依赖",
  script: "执行构建脚本",
  docker: "Docker构建",
  "build:verify": "产物校验",
  tag: "标记镜像",
  push: "推送镜像",
  "push:verify": "推送确认",
  save: "归档镜像",
  "gamma:deploy": "gamma部署",
  "gamma:test": "gamma测试",
};
const HIDDEN_PIPELINE_TASKS = {
  prepare: new Set(["swr", "ps"]),
  sync: new Set(["fetch", "checkout"]),
  build: new Set(["deps"]),
  push: new Set(["retry"]),
  archive: new Set(["reclaim", "record", "publish"]),
};
function commandTestKind(command) {
  if (typeof command === "string") {
    return commandTestKind({ name: command });
  }
  const typed = String((command && command.test_type) || "").toLowerCase();
  if (typed === "ut" || typed === "dt") return typed;
  const name = String((command && (command.name || command.command)) || "");
  const hasUt = /\bUT\b/i.test(name);
  const hasDt = /\bDT\b/i.test(name);
  if (hasUt && hasDt) return "both";
  if (hasDt) return "dt";
  if (hasUt) return "ut";
  return "";
}
function currentTestRun(job) {
  const runs = (job && job.test_runs) || [];
  return runs[0] || null;
}
function detectTestSplit(job) {
  const declared = new Set((job && job.test_kinds) || []);
  if (declared.has("ut") && declared.has("dt")) return true;
  const commands = (currentTestRun(job) && currentTestRun(job).commands) || [];
  if (!commands.length) return false;
  if (commands.some((item) => commandTestKind(item) === "both")) return false;
  const kinds = new Set(commands.map(commandTestKind).filter(Boolean));
  return kinds.has("ut") && kinds.has("dt");
}
function kindStatus(run, kind, fallback) {
  const commands = ((run && run.commands) || []).filter((item) => commandTestKind(item) === kind);
  if (!commands.length) return fallback || "pending";
  if (commands.some((item) => Number(item.exit_code) !== 0)) return "failed";
  return "done";
}
function casesForKind(run, kind) {
  const commands = (run && run.commands) || [];
  const names = new Set(commands.filter((item) => commandTestKind(item) === kind).map((item) => item.name));
  return ((run && run.test_cases) || []).filter((item) => {
    if (item.test_type === kind) return true;
    if (names.has(item.command)) return true;
    return commandTestKind({ name: item.command, test_type: item.test_type }) === kind;
  });
}

function pipelineJobStatusLabel(status, stage, cancelRequested) {
  if (status === "stopped") return "已停止";
  if (status === "queued") return "排队中";
  if (status === "running" && (cancelRequested || stage === "stopping")) return "停止中";
  const map = { running: "执行中", queued: "排队中", ok: "成功", failed: "失败", stopped: "已停止", unknown: "未知" };
  return map[status] || status || "";
}
function rollupStatus(items) {
  const list = (items || []).map((item) => item.status || "pending");
  if (!list.length) return "pending";
  if (list.some((s) => s === "failed" || s === "warn")) return "failed";
  if (list.some((s) => s === "queued")) return "queued";
  if (list.some((s) => s === "running")) return "running";
  if (list.every((s) => s === "skipped")) return "skipped";
  if (list.every((s) => s === "done" || s === "skipped")) return "done";
  return "pending";
}
function decoratePipelineTask(task, logStep, stageId) {
  const id = task.id || "";
  const label = PIPELINE_TASK_LABELS[stageId + ":" + id] || PIPELINE_TASK_LABELS[id] || task.label || id;
  return {
    ...task,
    id,
    label,
    logStep: task.logStep || logStep || stageId,
    status: task.status === "warn" ? "failed" : (task.status || "pending"),
  };
}
function visiblePipelineTasks(step) {
  const hidden = HIDDEN_PIPELINE_TASKS[step.id] || new Set();
  const raw = Array.isArray(step.subtasks) && step.subtasks.length
    ? step.subtasks
    : [{ id: step.id, label: step.label || step.id, status: step.status || "pending" }];
  const tasks = raw
    .filter((task) => !hidden.has(task.id))
    .map((task) => decoratePipelineTask(task, step.id, step.id));
  if (step.id === "archive") {
    return [decoratePipelineTask({
      id: "save",
      label: "归档镜像",
      status: step.status || "pending",
    }, "archive", "archive")];
  }
  if (step.id === "gamma") {
    return tasks.filter((task) => task.status !== "skipped");
  }
  return tasks;
}
function buildPipelineStages(pipeline, jobStatus) {
  const stages = [];
  const syncStep = (pipeline.steps || []).find((step) => step.id === "sync");
  const prepareHidden = HIDDEN_PIPELINE_TASKS.prepare || new Set();
  const prepareItems = (Array.isArray(pipeline.prepare) ? pipeline.prepare : [])
    .filter((item) => !prepareHidden.has(item.id));
  const preTasks = prepareItems.map((item) => decoratePipelineTask(item, "prepare", "prepare"));
  if (syncStep) {
    visiblePipelineTasks(syncStep).forEach((task) => preTasks.push(task));
  }
  if (preTasks.length) {
    stages.push({
      id: "prepare",
      logStep: "prepare",
      label: "前置准备",
      status: rollupStatus(prepareItems.concat(syncStep || [])),
      tasks: preTasks,
    });
  }
  (pipeline.steps || []).forEach((step) => {
    if (step.id === "sync") return;
    const tasks = visiblePipelineTasks(step);
    if (step.id === "gamma" && (step.status === "skipped" || !tasks.length)) return;
    stages.push({
      id: step.id,
      logStep: step.id,
      label: step.label || step.id,
      status: step.status === "warn" ? "failed" : (step.status || "pending"),
      tasks,
    });
  });
  if (jobStatus === "stopped" || jobStatus === "failed") {
    stages.forEach((stage) => {
      if (stage.status === "running") stage.status = jobStatus === "failed" ? "failed" : "skipped";
      else if (stage.status === "pending") stage.status = "skipped";
      (stage.tasks || []).forEach((task) => {
        if (task.status === "running") task.status = jobStatus === "failed" ? "failed" : "skipped";
        else if (task.status === "pending") task.status = "skipped";
      });
    });
  }
  return stages;
}
function isSelectablePreviewTask(stageId, taskId) {
  return stageId === "gamma" && (taskId === "deploy" || taskId === "test");
}
function previewTaskChecked(taskId) {
  return taskId === "deploy" ? !!runPreviewSelection.deploy : !!runPreviewSelection.test;
}
function createPreviewCheck(taskId, doc) {
  doc = doc || document;
  const box = doc.createElement("button");
  box.type = "button";
  box.className = "pl-check" + (previewTaskChecked(taskId) ? " is-on" : "");
  box.setAttribute("aria-pressed", previewTaskChecked(taskId) ? "true" : "false");
  box.setAttribute("aria-label", taskId === "deploy" ? "gamma部署" : "gamma测试");
  box.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5 10 17.5 19 7" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  box.addEventListener("click", (ev) => {
    ev.stopPropagation();
    togglePreviewTask(taskId);
  });
  return box;
}
function togglePreviewTask(taskId) {
  if (taskId === "deploy") runPreviewSelection.deploy = !runPreviewSelection.deploy;
  if (taskId === "test") runPreviewSelection.test = !runPreviewSelection.test;
  if (!runPreviewSelection.deploy && !runPreviewSelection.test) {
    runPreviewSelection.environmentId = "";
  }
  renderRunPage();
}
function previewPipelineData() {
  const task = (id, label) => ({ id, label, status: "pending" });
  return {
    prepare: [task("env", "检查环境"), task("slot", "等待并发槽位"), task("adir", "归档目录")],
    steps: [
      { id: "sync", label: "拉代码", status: "pending", subtasks: [task("clone", "克隆仓库"), task("sha", "记录提交")] },
      { id: "test", label: "测试执行", status: "pending", subtasks: [task("plan", "加载build.yaml"), task("runner", "启动测试"), task("ut-cases", "执行UT"), task("dt-cases", "执行DT")] },
      { id: "build", label: "构建镜像", status: "pending", subtasks: [task("script", "执行构建脚本"), task("docker", "Docker构建"), task("verify", "产物校验")] },
      { id: "push", label: "推送 SWR", status: "pending", subtasks: [task("tag", "标记镜像"), task("push", "推送镜像"), task("verify", "推送确认")] },
      { id: "gamma", label: "gamma集成测试", status: "pending", subtasks: [task("deploy", "gamma部署"), task("test", "gamma测试")] },
      { id: "archive", label: "本地归档", status: "pending", subtasks: [task("save", "归档镜像")] },
    ],
  };
}
function setPreviewHint(text) {
  const el = $("previewRunHint");
  if (!el) return;
  el.hidden = !text;
  el.textContent = text || "";
}
function setPreviewBranchStatus(text, cls) {
  const el = $("previewBranchStatus");
  if (!el) return;
  el.hidden = !text;
  el.textContent = text || "";
  el.className = "branch-status" + (cls ? " " + cls : "");
}
function renderRunPage() {
  renderJobPipeline("runJobPipeline", previewPipelineData(), "", null, { preview: true });
}
function ensurePreviewBranchPicker() {
  const root = $("previewBranchPicker");
  if (!root) return null;
  if (!previewBranchPicker) {
    previewBranchPicker = createBranchPicker(root, {
      trigger: "#previewBranch",
      label: "#previewBranchValue",
      menu: "#previewBranchMenu",
    });
  }
  return previewBranchPicker;
}
async function loadPreviewBranches(force) {
  const svc = currentService();
  const picker = ensurePreviewBranchPicker();
  const retryBtn = $("btnRefreshPreviewBranches");
  if (!svc || !picker) return;
  const loadGeneration = ++previewBranchLoadGeneration;
  const isCurrentLoad = () => (
    runPreviewActive &&
    runLayerOpen() &&
    loadGeneration === previewBranchLoadGeneration &&
    currentServiceId === svc.id
  );
  const fallbackBranch = svc.default_branch || "main";
  const cached = branchCache[svc.id];
  const setLoading = (loading) => {
    if (!isCurrentLoad()) return;
    if (retryBtn) retryBtn.classList.toggle("loading", loading);
    if (retryBtn) retryBtn.disabled = loading;
    // 默认分支始终可用；读取缓存不能让用户的运行入口失去响应。
    picker.setDisabled(false);
    if (loading) setPreviewBranchStatus(force ? "正在从远程更新分支…" : "正在读取服务器缓存…");
  };
  const applyCached = (data) => {
    if (!isCurrentLoad()) return;
    picker.setOptions(
      data.branches,
      data.selected_branch || fallbackBranch,
      data.default_branch || fallbackBranch
    );
    const count = (data.branches || []).length;
    if (data.refresh_error) {
      setPreviewBranchStatus("远程刷新失败，已使用服务器缓存（" + count + " 个分支）", "error");
    } else if (force) {
      setPreviewBranchStatus("已更新 " + count + " 个分支", "ok");
    } else if (data.cached) {
      setPreviewBranchStatus("已读取服务器缓存（" + count + " 个分支）", "ok");
    } else {
      setPreviewBranchStatus("服务器暂无分支缓存，当前使用默认分支；点击刷新加载");
    }
  };
  if (!force && cached && cached.loaded) {
    const tpl = findPipelineTemplate(runLayerTemplateId);
    if (!isCurrentLoad()) return;
    applyCached({ ...cached, selected_branch: (tpl && tpl.branch) || cached.selected_branch || fallbackBranch });
    return;
  }
  setLoading(true);
  try {
    const data = await fetchServiceBranches(svc.id, !!force, true);
    const tpl = findPipelineTemplate(runLayerTemplateId);
    if (tpl && tpl.branch) data.selected_branch = tpl.branch;
    applyCached(data);
  } catch (e) {
    if (!isCurrentLoad()) return;
    picker.setOptions([fallbackBranch], fallbackBranch, fallbackBranch);
    setPreviewBranchStatus(e.message || "加载分支失败", "error");
    delete branchCache[svc.id];
  } finally {
    if (!isCurrentLoad()) return;
    if (retryBtn) retryBtn.classList.remove("loading");
    if (retryBtn) retryBtn.disabled = false;
    picker.setDisabled(false);
  }
}
function runLayerOpen() {
  const layer = $("runLayer");
  return !!(layer && !layer.hidden);
}
function closeRunPopup() {
  previewBranchPicker = null;
  const layer = $("runLayer");
  if (layer) layer.hidden = true;
  document.body.classList.remove("run-open");
}
function findPipelineTemplate(id) {
  return pipelineTemplates.find((item) => item.id === id) || null;
}
function selectedPipelineTemplate() {
  return findPipelineTemplate(selectedTemplateId);
}
function defaultPersonalTemplate() {
  return pipelineTemplates.find((item) => item.kind === "personal" && item.builtin) || pipelineTemplates[0] || null;
}
function applyTemplateDefaults(tpl) {
  runPreviewSelection = {
    deploy: !!(tpl && tpl.gamma_deploy),
    test: !!(tpl && tpl.gamma_test),
    environmentId: (tpl && tpl.environment_id) || "",
  };
}
function syncRunLayerChrome() {
  const tpl = findPipelineTemplate(runLayerTemplateId);
  const caption = $("runTplCaption");
  if (caption) {
    caption.hidden = !tpl || runLayerMode === "edit";
    caption.textContent = tpl ? ("流水线：" + tpl.name) : "";
  }
  const nameBlock = $("runTemplateNameBlock");
  const nameInput = $("runTemplateName");
  if (nameBlock) nameBlock.hidden = runLayerMode !== "edit";
  if (nameInput) nameInput.value = tpl ? (tpl.name || "") : "";
  const confirm = $("btnRunPreviewConfirm");
  if (confirm) confirm.textContent = runLayerMode === "edit" ? "保存" : "确认";
}
function openRunWindow(templateId) {
  const svc = currentService();
  if (!svc || serviceActiveJobId) return;
  const layer = $("runLayer");
  if (!layer) return;
  if (runLayerOpen() || cloneLayerOpen()) return;
  hideTplMenu();
  const tpl = findPipelineTemplate(templateId) || selectedPipelineTemplate() || defaultPersonalTemplate();
  runLayerMode = "run";
  runLayerTemplateId = tpl ? tpl.id : "";
  if (tpl) selectedTemplateId = tpl.id;
  renderPipelineTemplates();
  layer.hidden = false;
  document.body.classList.add("run-open");
  enterRunPage(tpl);
}
function openEditTemplate(templateId) {
  const svc = currentService();
  const tpl = findPipelineTemplate(templateId);
  const layer = $("runLayer");
  if (!svc || !tpl || !layer || runLayerOpen()) return;
  hideTplMenu();
  runLayerMode = "edit";
  runLayerTemplateId = tpl.id;
  selectedTemplateId = tpl.id;
  renderPipelineTemplates();
  layer.hidden = false;
  document.body.classList.add("run-open");
  enterRunPage(tpl);
}
function enterRunPage(tpl) {
  runPreviewActive = true;
  applyTemplateDefaults(tpl);
  previewBranchPicker = null;
  const svc = currentService();
  const fallbackBranch = (svc && svc.default_branch) || "main";
  const picker = ensurePreviewBranchPicker();
  if (picker) {
    picker.setOptions([fallbackBranch], fallbackBranch, fallbackBranch);
    picker.setDisabled(false);
  }
  const retryBtn = $("btnRefreshPreviewBranches");
  if (retryBtn) {
    retryBtn.classList.remove("loading");
    retryBtn.disabled = false;
  }
  setPreviewBranchStatus("");
  setPreviewHint("");
  syncRunLayerChrome();
  renderRunPage();
  updateActionButtons();
  loadEnvironments().then(() => {
    if (!runPreviewActive) return;
    if (tpl && tpl.environment_id && !environments.some((item) => item.id === tpl.environment_id)) {
      runPreviewSelection.environmentId = "";
    }
    renderRunPage();
  }).catch((e) => setPreviewHint(e.message || "加载环境失败"));
  loadPreviewBranches(false);
}
function cancelRunPreview() {
  previewBranchLoadGeneration += 1;
  if (!runPreviewActive && !runLayerOpen()) return;
  runPreviewActive = false;
  runLayerMode = "run";
  runLayerTemplateId = "";
  runPreviewSelection = { deploy: false, test: false, environmentId: "" };
  setPreviewHint("");
  closeRunPopup();
  applyServiceChrome();
  applyJob(currentJob);
  updateActionButtons();
}
function confirmRunPreview() {
  if (runLayerMode === "edit") {
    saveEditedTemplate().catch((e) => setPreviewHint(e.message || "保存失败"));
    return;
  }
  const wantsGamma = runPreviewSelection.deploy || runPreviewSelection.test;
  if (wantsGamma && !runPreviewSelection.environmentId) {
    setPreviewHint("请先选择环境");
    return;
  }
  const branch = (previewBranchPicker && previewBranchPicker.value) || "";
  startRun(branch);
}
async function saveEditedTemplate() {
  if (!runLayerTemplateId) return;
  const name = (($("runTemplateName") && $("runTemplateName").value) || "").trim();
  if (!name) {
    setPreviewHint("请填写流水线名称");
    return;
  }
  const branch = (previewBranchPicker && previewBranchPicker.value) || "";
  const data = await api("/api/pipeline-templates/" + encodeURIComponent(runLayerTemplateId), {
    method: "POST",
    body: JSON.stringify({
      name,
      branch,
      gamma_deploy: !!runPreviewSelection.deploy,
      gamma_test: !!runPreviewSelection.test,
      environment_id: runPreviewSelection.environmentId || "",
    }),
  });
  if (data.template) {
    const idx = pipelineTemplates.findIndex((item) => item.id === data.template.id);
    if (idx >= 0) pipelineTemplates[idx] = data.template;
    else pipelineTemplates.push(data.template);
  }
  cancelRunPreview();
  await loadPipelineTemplates();
}
function hideTplMenu() {
  const menu = $("tplMenu");
  const trigger = $("tplTrigger");
  if (menu) menu.hidden = true;
  if (trigger) trigger.setAttribute("aria-expanded", "false");
}
function renderPipelineTemplates() {
  const label = $("tplTriggerLabel");
  const menu = $("tplMenu");
  const tpl = selectedPipelineTemplate() || defaultPersonalTemplate();
  if (label) label.textContent = tpl ? (tpl.name || "未命名流水线") : "选择流水线";
  if (menu) {
    menu.innerHTML = pipelineTemplates.map((item) => (
      '<button type="button" role="option" data-tpl-id="' + esc(item.id) + '"' +
      (item.id === selectedTemplateId ? ' class="active"' : "") + ">" +
      esc(item.name || "未命名流水线") +
      "</button>"
    )).join("");
    menu.querySelectorAll("[data-tpl-id]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const id = btn.getAttribute("data-tpl-id") || "";
        if (id) selectedTemplateId = id;
        hideTplMenu();
        renderPipelineTemplates();
      });
    });
  }
  updateActionButtons();
}
async function loadPipelineTemplates() {
  if (isAllServices() || !currentServiceId) {
    pipelineTemplates = [];
    selectedTemplateId = "";
    renderPipelineTemplates();
    return;
  }
  const [tplData, envData] = await Promise.all([
    api("/api/pipeline-templates?service_id=" + encodeURIComponent(currentServiceId)),
    api("/api/environments?service_id=" + encodeURIComponent(currentServiceId)),
  ]);
  pipelineTemplates = tplData.templates || [];
  environments = envData.environments || [];
  if (tab === "envs") renderEnvironments();
  if (!selectedTemplateId || !pipelineTemplates.some((item) => item.id === selectedTemplateId)) {
    const personal = defaultPersonalTemplate();
    selectedTemplateId = personal ? personal.id : "";
  }
  renderPipelineTemplates();
}
function cloneLayerOpen() {
  const layer = $("cloneLayer");
  return !!(layer && !layer.hidden);
}
function setCloneHint(text) {
  const el = $("cloneHint");
  if (!el) return;
  el.hidden = !text;
  el.textContent = text || "";
}
function openCloneWindow(templateId) {
  const svc = currentService();
  const tpl = findPipelineTemplate(templateId) || selectedPipelineTemplate();
  const layer = $("cloneLayer");
  if (!svc || !tpl || !layer || runLayerOpen() || cloneLayerOpen()) return;
  hideTplMenu();
  cloneSourceId = tpl.id;
  selectedTemplateId = tpl.id;
  renderPipelineTemplates();
  const nameInput = $("cloneTemplateName");
  if (nameInput) nameInput.value = (tpl.name || "流水线") + "_copy";
  setCloneHint("");
  layer.hidden = false;
  document.body.classList.add("clone-open");
  updateActionButtons();
  if (nameInput) nameInput.focus();
}
function closeCloneWindow() {
  if (!cloneLayerOpen() && !cloneSourceId) return;
  cloneSourceId = "";
  setCloneHint("");
  const layer = $("cloneLayer");
  if (layer) layer.hidden = true;
  document.body.classList.remove("clone-open");
  updateActionButtons();
}
async function confirmClone() {
  if (!cloneSourceId) return;
  const name = (($("cloneTemplateName") && $("cloneTemplateName").value) || "").trim();
  if (!name) {
    setCloneHint("请填写流水线名称");
    return;
  }
  const data = await api("/api/pipeline-templates/" + encodeURIComponent(cloneSourceId) + "/copy", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
  if (data.template) selectedTemplateId = data.template.id;
  closeCloneWindow();
  await loadPipelineTemplates();
}
function confirmDeleteTemplate(id) {
  const tpl = findPipelineTemplate(id);
  if (!tpl || tpl.builtin) return;
  const wrap = document.createElement("div");
  wrap.innerHTML =
    "<p>确定删除流水线「" + esc(tpl.name) + "」？</p>" +
    '<div class="row"><button type="button" class="btn ghost" data-close-modal>取消</button>' +
    '<button type="button" class="btn danger" id="btnTplDeleteOk">删除</button></div>';
  openModal("删除流水线", wrap, { narrow: true });
  wrap.querySelector("#btnTplDeleteOk").addEventListener("click", async () => {
    try {
      await api("/api/pipeline-templates/" + encodeURIComponent(id) + "/delete", { method: "POST", body: "{}" });
      closeModal();
      if (selectedTemplateId === id) selectedTemplateId = "";
      await loadPipelineTemplates();
    } catch (e) {
      alert(e.message || "删除失败");
    }
  });
}
function createStatusIcon(status, kind, doc) {
  doc = doc || document;
  const icon = doc.createElement("span");
  icon.className = (kind || "pl-icon") + " is-" + (status || "pending");
  icon.textContent = status === "done" ? "✓" : (status === "failed" || status === "warn") ? "✕" : status === "skipped" ? "–" : (status === "running" || status === "queued") ? "●" : "";
  return icon;
}
function createStageColumn(stage, incomingComplete, outgoingComplete, onStep, preview, doc) {
  doc = doc || document;
  const status = stage.status || "pending";
  const col = doc.createElement("div");
  col.className = "pl-col is-" + status + (stage.id === "gamma" ? " is-gamma" : "");
  if (!preview) {
    col.addEventListener("click", () => onStep && onStep(stage.id, stage.tasks && stage.tasks[0] && stage.tasks[0].id));
  }
  const cap = doc.createElement("div");
  cap.className = "pl-caption";
  const title = doc.createElement("div");
  title.className = "pl-title";
  title.textContent = stage.label || stage.id || "";
  const dur = doc.createElement("div");
  dur.className = "pl-dur is-" + status;
  if (preview) dur.innerHTML = "&nbsp;";
  else dur.textContent = PIPELINE_STATE_LABELS[status] || status;
  cap.append(title, dur);
  col.appendChild(cap);
  const spine = doc.createElement("div");
  spine.className = "pl-spine";
  const left = doc.createElement("div");
  left.className = "pl-rail left " + (incomingComplete ? "is-ok" : "is-wait");
  const right = doc.createElement("div");
  right.className = "pl-rail right " + (outgoingComplete ? "is-ok" : "is-wait");
  spine.append(left, createStatusIcon(status, "pl-stage-icon", doc), right);
  col.appendChild(spine);
  const drop = doc.createElement("div");
  drop.className = "pl-drop";
  const diamond = doc.createElement("div");
  diamond.className = "pl-diamond";
  diamond.append(doc.createElement("span"), doc.createElement("span"));
  drop.appendChild(diamond);
  const tree = doc.createElement("div");
  tree.className = "pl-tree";
  if (preview && stage.id === "gamma") {
    const envRow = doc.createElement("div");
    envRow.className = "pl-task pl-env-row";
    envRow.appendChild(doc.createElement("span"));
    envRow.appendChild(createStatusIcon("pending", "pl-task-icon", doc));
    envRow.appendChild(createGammaEnvPicker(doc));
    tree.appendChild(envRow);
  }
  (stage.tasks || []).forEach((task) => {
    const row = doc.createElement("div");
    row.className = "pl-task is-" + (task.status || "pending");
    row.appendChild(doc.createElement("span"));
    if (preview && isSelectablePreviewTask(stage.id, task.id)) {
      row.classList.add("is-selectable");
      row.appendChild(createPreviewCheck(task.id, doc));
    } else {
      row.appendChild(createStatusIcon(task.status, "pl-task-icon", doc));
    }
    const name = doc.createElement("span");
    name.className = "pl-task-name";
    name.textContent = task.label || task.id || "";
    row.appendChild(name);
    row.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (preview && isSelectablePreviewTask(stage.id, task.id)) {
        togglePreviewTask(task.id);
        return;
      }
      onStep && onStep(stage.id, task.id);
    });
    tree.appendChild(row);
  });
  drop.appendChild(tree);
  col.appendChild(drop);
  return col;
}
function selectedEnvName() {
  const env = environments.find((item) => item.id === runPreviewSelection.environmentId);
  return env ? (env.name || "未命名环境") : "";
}
function createGammaEnvPicker(doc) {
  doc = doc || document;
  const win = doc.defaultView || window;
  const wrap = doc.createElement("div");
  wrap.className = "pl-env-pick";
  const picker = doc.createElement("div");
  picker.className = "pl-env-picker";
  const trigger = doc.createElement("button");
  trigger.type = "button";
  trigger.className = "input branch-trigger pl-env-trigger";
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");
  const label = doc.createElement("span");
  label.className = "pl-env-trigger-label";
  const empty = !environments.length;
  label.textContent = selectedEnvName() || (empty ? "还没有环境" : "选择环境");
  trigger.disabled = empty;
  trigger.appendChild(label);
  const chevron = doc.createElementNS("http://www.w3.org/2000/svg", "svg");
  chevron.setAttribute("viewBox", "0 0 24 24");
  chevron.setAttribute("aria-hidden", "true");
  chevron.innerHTML = '<path d="M7 10l5 5 5-5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>';
  trigger.appendChild(chevron);
  const menu = doc.createElement("div");
  menu.className = "branch-menu";
  menu.hidden = true;
  menu.setAttribute("role", "listbox");
  const close = () => {
    menu.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (menu.parentNode) menu.remove();
  };
  const positionMenu = () => {
    const rect = trigger.getBoundingClientRect();
    const width = Math.max(rect.width, 240);
    menu.classList.add("branch-menu-portal");
    menu.style.left = rect.left + "px";
    menu.style.top = (rect.bottom + 6) + "px";
    menu.style.width = width + "px";
    menu.style.maxHeight = Math.max(120, Math.min(280, win.innerHeight - rect.bottom - 12)) + "px";
    doc.body.appendChild(menu);
  };
  environments.forEach((env) => {
    const option = doc.createElement("button");
    option.type = "button";
    option.className = "branch-option" + (runPreviewSelection.environmentId === env.id ? " active" : "");
    option.setAttribute("role", "option");
    option.textContent = env.name || "未命名环境";
    option.addEventListener("click", (ev) => {
      ev.stopPropagation();
      runPreviewSelection.environmentId = env.id;
      label.textContent = env.name || "未命名环境";
      menu.querySelectorAll(".branch-option").forEach((item) => item.classList.toggle("active", item === option));
      setPreviewHint("");
      close();
      trigger.focus();
    });
    menu.appendChild(option);
  });
  trigger.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (trigger.disabled) return;
    const opening = menu.hidden;
    if (opening) positionMenu();
    else close();
    menu.hidden = !opening;
    trigger.setAttribute("aria-expanded", String(opening));
  });
  picker.addEventListener("focusout", () => {
    setTimeout(() => {
      if (!picker.contains(doc.activeElement) && !menu.contains(doc.activeElement)) close();
    }, 0);
  });
  picker.append(trigger, menu);
  wrap.appendChild(picker);
  return wrap;
}
function createEndpoint(kind, lit, doc) {
  doc = doc || document;
  const col = doc.createElement("div");
  col.className = "pl-end is-" + kind;
  const cap = doc.createElement("div");
  cap.className = "pl-caption";
  const title = doc.createElement("div");
  title.className = "pl-title";
  title.textContent = kind === "start" ? "Start" : "End";
  const dur = doc.createElement("div");
  dur.className = "pl-dur";
  dur.innerHTML = "&nbsp;";
  cap.append(title, dur);
  col.appendChild(cap);
  const spine = doc.createElement("div");
  spine.className = "pl-spine";
  const left = doc.createElement("div");
  left.className = "pl-rail left " + (kind === "start" ? "is-none" : (lit ? "is-ok" : "is-wait"));
  const right = doc.createElement("div");
  right.className = "pl-rail right " + (kind === "end" ? "is-none" : (lit ? "is-ok" : "is-wait"));
  const icon = doc.createElement("span");
  icon.className = "pl-stage-icon is-end" + (lit ? " is-ok" : " is-wait");
  spine.append(left, icon, right);
  col.appendChild(spine);
  return col;
}
function renderJobPipeline(containerId, pipeline, jobStatus, onStep, opts) {
  const root = $(containerId);
  if (!root) return;
  const preview = !!(opts && opts.preview);
  const steps = pipeline && Array.isArray(pipeline.steps) ? pipeline.steps : [];
  if (!pipeline || !steps.length) {
    root.hidden = true;
    root.replaceChildren();
    return;
  }
  root.hidden = false;
  root.replaceChildren();
  const meta = pipeline.meta || {};
  const status = jobStatus || meta.status || "";
  const stages = buildPipelineStages(pipeline, status);
  const doc = root.ownerDocument || document;
  const scroll = doc.createElement("div");
  scroll.className = "pl-scroll";
  const graph = doc.createElement("div");
  graph.className = "pl-graph";
  const isComplete = (stage) => Boolean(stage && stage.status === "done");
  graph.appendChild(createEndpoint("start", !preview, doc));
  stages.forEach((stage, index) => {
    const previous = stages[index - 1];
    const incomingComplete = !preview && (index === 0 || isComplete(previous));
    const outgoingComplete = !preview && isComplete(stage);
    graph.appendChild(createStageColumn(stage, incomingComplete, outgoingComplete, onStep, preview, doc));
  });
  graph.appendChild(createEndpoint("end", !preview && isComplete(stages[stages.length - 1]) && status === "ok", doc));
  scroll.appendChild(graph);
  root.appendChild(scroll);
}

function historyStatusLabel(status) {
  return ({ ok: "成功", failed: "失败", running: "执行中", queued: "排队中", stopped: "已停止" })[status] || status || "未知";
}
function historyStatusClass(status) {
  return ({ ok: "art-status-ok", failed: "art-status-failed", running: "art-status-running", queued: "art-status-queued", stopped: "art-status-stopped" })[status] || "";
}
function historyDurationLabel(item) {
  if (jobIsLive(item)) return "—";
  return formatDurationSec(item && item.duration_sec);
}

function renderPager(el, pager, meta, onChange) {
  if (!el) return;
  const total = Number((meta && meta.total) || 0);
  const pageCount = Math.max(1, Number((meta && meta.page_count) || 1));
  pager.page = Math.min(Math.max(1, Number((meta && meta.page) || pager.page) || 1), pageCount);
  if (!total) { el.hidden = true; el.replaceChildren(); return; }
  el.hidden = false;
  el.replaceChildren();
  const sizeWrap = document.createElement("label");
  sizeWrap.className = "test-page-size";
  sizeWrap.append("每页 ");
  const size = document.createElement("select");
  size.className = "input";
  PAGE_SIZE_OPTIONS.forEach((n) => {
    const opt = document.createElement("option");
    opt.value = String(n);
    opt.textContent = String(n);
    if (n === pager.pageSize) opt.selected = true;
    size.appendChild(opt);
  });
  size.addEventListener("change", () => {
    pager.pageSize = Number(size.value) || 10;
    pager.page = 1;
    onChange();
  });
  sizeWrap.append(size, " 条");
  const prev = document.createElement("button");
  prev.type = "button";
  prev.className = "btn ghost";
  prev.textContent = "上一页";
  prev.disabled = pager.page <= 1;
  prev.addEventListener("click", () => { pager.page -= 1; onChange(); });
  const next = document.createElement("button");
  next.type = "button";
  next.className = "btn ghost";
  next.textContent = "下一页";
  next.disabled = pager.page >= pageCount;
  next.addEventListener("click", () => { pager.page += 1; onChange(); });
  const pos = document.createElement("span");
  pos.textContent = "第 " + pager.page + " / " + pageCount + " 页（" + total + " 条）";
  el.append(sizeWrap, prev, pos, next);
}

function renderHistory(items, meta) {
  const body = $("historyBody");
  if (!body) return;
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="' + (isAllServices() ? 8 : 7) + '" class="hint">暂无构建记录。</td></tr>';
  } else {
    body.innerHTML = items.map((item) => (
      "<tr>" +
      '<td class="hist-time">' + esc(item.created_at || "—") + "</td>" +
      '<td class="hist-service-cell">' + esc(item.service_id || "—") + "</td>" +
      clipTd(item.branch, "hist-branch") +
      '<td class="' + historyStatusClass(item.status) + '">' + esc(historyStatusLabel(item.status)) + "</td>" +
      '<td class="mono">' + esc(shortSha(item.commit_sha)) + "</td>" +
      '<td class="hist-dur">' + esc(historyDurationLabel(item)) + "</td>" +
      "<td>" + esc(item.operator || "—") + "</td>" +
      '<td class="hist-act"><button type="button" class="btn ghost" data-open-job="' + esc(item.id) + '">查看</button></td>' +
      "</tr>"
    )).join("");
    body.querySelectorAll("[data-open-job]").forEach((btn) => {
      btn.addEventListener("click", () => openJob(btn.getAttribute("data-open-job"), { fromHistory: true }));
    });
  }
  renderPager($("historyPager"), historyPager, meta, () => refreshHistory().catch(() => {}));
}

function renderArtifacts(items, meta) {
  const body = $("artifactsBody");
  if (!body) return;
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="' + (isAllServices() ? 7 : 6) + '" class="hint">暂无产物。</td></tr>';
  } else {
    body.innerHTML = items.map((item) => {
      const expired = item.expired;
      const fileCell = expired
        ? '<span class="art-expired">已失效</span>'
        : (item.download_url ? '<a href="' + esc(item.download_url) + '">' + esc(item.package_name || "下载") + "</a>" : "—");
      return (
        '<tr class="' + (expired ? "expired" : "") + '">' +
        "<td>" + esc(item.created_at || "—") + "</td>" +
        '<td class="art-service-cell">' + esc(item.title || item.service_id || "—") + "</td>" +
        clipTd(item.branch, "art-branch") +
        '<td class="mono">' + esc(shortSha(item.commit_sha)) + "</td>" +
        clipTd(item.image_ref || item.image, "art-image mono") +
        "<td>" + fileCell + "</td>" +
        "<td>" + esc(item.operator || "—") + "</td>" +
        "</tr>"
      );
    }).join("");
  }
  renderPager($("artifactsPager"), artifactsPager, meta, () => refreshArtifacts().catch(() => {}));
}

async function refreshHistory() {
  if (!sessionLive) return;
  await withRefresh($("btnRefreshHistory"), async () => {
    const gen = viewGeneration;
    const sid = currentServiceId;
    try {
      const data = await api("/api/jobs?page=" + historyPager.page + "&page_size=" + historyPager.pageSize + serviceQuery());
      if (!viewIsCurrent(gen, sid)) return;
      const got = data.service_id || "";
      const want = isAllServices() ? "" : sid;
      if (got !== want) return;
      renderHistory(data.jobs || [], data);
    } catch (_) {
      if (!viewIsCurrent(gen, sid)) return;
      if ($("historyBody")) $("historyBody").innerHTML = '<tr><td colspan="' + (isAllServices() ? 8 : 7) + '" class="hint">加载失败，请刷新。</td></tr>';
    }
  });
}
async function refreshArtifacts() {
  if (!sessionLive) return;
  await withRefresh($("btnRefreshArtifacts"), async () => {
    const gen = viewGeneration;
    const sid = currentServiceId;
    try {
      const data = await api("/api/artifacts?page=" + artifactsPager.page + "&page_size=" + artifactsPager.pageSize + serviceQuery());
      if (!viewIsCurrent(gen, sid)) return;
      const got = data.service_id || "";
      const want = isAllServices() ? "" : sid;
      if (got !== want) return;
      renderArtifacts(data.artifacts || [], data);
    } catch (_) {
      if (!viewIsCurrent(gen, sid)) return;
      if ($("artifactsBody")) $("artifactsBody").innerHTML = '<tr><td colspan="' + (isAllServices() ? 7 : 6) + '" class="hint">加载失败，请刷新。</td></tr>';
    }
  });
}

async function refreshPipeline() {
  if (!sessionLive || isAllServices() || !currentServiceId) return;
  await withRefresh($("btnRefreshPipeline"), async () => {
    const target = pinnedJobId || currentJobId;
    if (target) {
      await openJob(target);
      await refreshServiceOccupancy();
      return;
    }
    await loadServiceJob();
  });
}

function serviceTitle(item) {
  return (item && (item.title || item.id)) || "";
}
function isFavoriteService(id) {
  return favoriteServiceIds.includes(id);
}
function renderFavoriteServices() {
  const container = $("favoriteServices");
  if (!container) return;
  const favorites = favoriteServiceIds
    .map((id) => services.find((item) => item.id === id))
    .filter(Boolean);
  container.hidden = nav !== "build" || runPreviewActive || !favorites.length;
  container.replaceChildren();
  favorites.forEach((item) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "favorite-service-chip" + (item.id === currentServiceId ? " active" : "");
    chip.title = serviceTitle(item);
    chip.textContent = serviceTitle(item);
    chip.addEventListener("click", () => selectService(item.id));
    container.appendChild(chip);
  });
}
async function toggleServiceFavorite(serviceId) {
  const data = await api("/api/service-favorites", {
    method: "POST",
    body: JSON.stringify({ service_id: serviceId }),
  });
  if (Array.isArray(data.service_ids)) favoriteServiceIds = data.service_ids;
  renderFavoriteServices();
  renderServiceMenu();
}
function sortedServices() {
  return services.slice().sort((a, b) =>
    serviceTitle(a).localeCompare(serviceTitle(b), "en", { sensitivity: "base" })
  );
}
function serviceMatches(item, query) {
  const parts = String(query || "").trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!parts.length) return true;
  const hay = [item.id, item.title, item.image, item.repo, item.github].join(" ").toLowerCase();
  return parts.every((part) => hay.includes(part));
}
function hideServiceMenu() {
  if ($("serviceMenu")) $("serviceMenu").hidden = true;
  if ($("serviceTrigger")) $("serviceTrigger").setAttribute("aria-expanded", "false");
}
function showServiceMenu() {
  hideTplMenu();
  const menu = $("serviceMenu");
  const trigger = $("serviceTrigger");
  if (!menu || !trigger) return;
  svcMenuPage = 1;
  if ($("serviceSearch")) $("serviceSearch").value = "";
  menu.hidden = false;
  trigger.setAttribute("aria-expanded", "true");
  renderServiceMenu();
  if ($("serviceSearch")) $("serviceSearch").focus();
}
function renderServiceMenu() {
  const list = $("serviceMenuList");
  const pager = $("servicePager");
  if (!list || !$("serviceMenu")) return;
  $("serviceMenu").hidden = false;
  const query = $("serviceSearch") ? $("serviceSearch").value : "";
  const q = String(query || "").trim().toLowerCase();
  const showAll = !q || "全部微服务".includes(q) || q === "all";
  const items = sortedServices().filter((item) => serviceMatches(item, query));
  const pageCount = Math.max(1, Math.ceil(items.length / SVC_PAGE_SIZE));
  svcMenuPage = Math.min(Math.max(1, svcMenuPage), pageCount);
  const pageItems = items.slice((svcMenuPage - 1) * SVC_PAGE_SIZE, svcMenuPage * SVC_PAGE_SIZE);
  list.replaceChildren();
  if (showAll) {
    const allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className = (isAllServices() ? "active " : "") + "is-all";
    allBtn.textContent = "全部微服务";
    allBtn.addEventListener("mousedown", (ev) => ev.preventDefault());
    allBtn.addEventListener("click", () => {
      selectService(ALL_SERVICES_ID);
      hideServiceMenu();
    });
    list.appendChild(allBtn);
  }
  if (!pageItems.length && !showAll) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "无匹配微服务";
    list.appendChild(empty);
  } else {
    pageItems.forEach((item) => {
      const row = document.createElement("div");
      row.className = "svc-menu-item";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "svc-menu-service" + (item.id === currentServiceId ? " active" : "");
      btn.textContent = serviceTitle(item);
      btn.addEventListener("mousedown", (ev) => ev.preventDefault());
      btn.addEventListener("click", () => {
        selectService(item.id);
        hideServiceMenu();
      });
      const favorite = document.createElement("button");
      favorite.type = "button";
      favorite.className = "svc-favorite-button" + (isFavoriteService(item.id) ? " is-favorite" : "");
      favorite.title = isFavoriteService(item.id) ? "取消收藏" : "收藏微服务";
      favorite.setAttribute("aria-label", favorite.title);
      favorite.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.8l2.5 5.1 5.6.8-4.1 4 .9 5.6-4.9-2.6-4.9 2.6.9-5.6-4.1-4 5.6-.8L12 3.8z" fill="currentColor" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>';
      favorite.addEventListener("mousedown", (ev) => ev.preventDefault());
      favorite.addEventListener("click", (ev) => {
        ev.stopPropagation();
        toggleServiceFavorite(item.id).catch(() => {});
      });
      row.append(btn, favorite);
      list.appendChild(row);
    });
  }
  if (pager) {
    pager.replaceChildren();
    const total = document.createElement("span");
    total.textContent = "共 " + items.length + " 条";
    const pages = document.createElement("div");
    pages.className = "pages";
    const prev = document.createElement("button");
    prev.type = "button";
    prev.textContent = "<";
    prev.disabled = svcMenuPage <= 1;
    prev.dataset.page = String(svcMenuPage - 1);
    pages.appendChild(prev);
    const start = Math.max(1, svcMenuPage - 3);
    const end = Math.min(pageCount, start + 6);
    for (let page = start; page <= end; page += 1) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = String(page);
      btn.dataset.page = String(page);
      btn.className = page === svcMenuPage ? "active" : "";
      pages.appendChild(btn);
    }
    const next = document.createElement("button");
    next.type = "button";
    next.textContent = ">";
    next.disabled = svcMenuPage >= pageCount;
    next.dataset.page = String(svcMenuPage + 1);
    pages.appendChild(next);
    pager.append(total, pages);
  }
}
function fillServiceSelect() {
  if ($("serviceTriggerLabel")) {
    $("serviceTriggerLabel").textContent = isAllServices()
      ? "全部微服务"
      : (currentService() ? serviceTitle(currentService()) : "选择微服务");
  }
  renderFavoriteServices();
}
function clearStaleLists() {
  const historyColumns = isAllServices() ? 8 : 7;
  const artifactColumns = isAllServices() ? 7 : 6;
  if ($("historyBody")) $("historyBody").innerHTML = '<tr><td colspan="' + historyColumns + '" class="hint">加载中…</td></tr>';
  if ($("artifactsBody")) $("artifactsBody").innerHTML = '<tr><td colspan="' + artifactColumns + '" class="hint">加载中…</td></tr>';
  const histPager = $("historyPager");
  const artPager = $("artifactsPager");
  if (histPager) histPager.hidden = true;
  if (artPager) artPager.hidden = true;
}
function activateService(id) {
  if (!id) {
    fillServiceSelect();
    applyServiceChrome();
    return false;
  }
  if (id === currentServiceId) {
    fillServiceSelect();
    applyServiceChrome();
    return false;
  }
  cancelRunPreview();
  closeCloneWindow();
  currentServiceId = id;
  selectedTemplateId = "";
  pipelineTemplates = [];
  pinnedJobId = "";
  serviceActiveJobId = "";
  viewGeneration += 1;
  historyPager.page = 1;
  artifactsPager.page = 1;
  fillServiceSelect();
  try { localStorage.setItem(SERVICE_KEY, currentServiceId); } catch (_) {}
  applyServiceChrome();
  updateActionButtons();
  clearStaleLists();
  if (currentJob && (isAllServices() || !jobBelongsToService(currentJob, currentServiceId))) {
    applyJob(null, { loading: !isAllServices() && tab === "pipeline" });
  }
  return true;
}
function selectService(id) {
  if (!sessionLive) return;
  const changed = activateService(id);
  if (!changed) return;
  closeEnvOverlay();
  if (isAllServices()) {
    stopPolling();
    applyJob(null);
    if (tab === "pipeline") {
      setTab("history");
      return;
    }
    writeHash();
    if (tab === "history") refreshHistory().catch(() => {});
    else if (tab === "artifacts") refreshArtifacts().catch(() => {});
    else if (tab === "envs") loadEnvironments().catch((e) => setEnvError(e.message));
    return;
  }
  writeHash();
  refreshServiceOccupancy().catch(() => {});
  if (tab === "pipeline") {
    loadPipelineTemplates().catch(() => {});
    loadServiceJob();
  }
  else {
    stopPolling();
    if (tab === "history") refreshHistory().catch(() => {});
    else if (tab === "artifacts") refreshArtifacts().catch(() => {});
    else if (tab === "envs") loadEnvironments().catch((e) => setEnvError(e.message));
  }
}
function bindServicePicker() {
  const trigger = $("serviceTrigger");
  const input = $("serviceSearch");
  const menu = $("serviceMenu");
  if (!trigger || trigger.dataset.bound === "1") return;
  trigger.dataset.bound = "1";
  trigger.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if ($("serviceMenu") && !$("serviceMenu").hidden) hideServiceMenu();
    else showServiceMenu();
  });
  if (menu) {
    menu.addEventListener("click", (ev) => ev.stopPropagation());
    menu.addEventListener("mousedown", (ev) => ev.stopPropagation());
  }
  const pager = $("servicePager");
  if (pager && pager.dataset.bound !== "1") {
    pager.dataset.bound = "1";
    pager.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const btn = ev.target.closest("button");
      if (!btn || btn.disabled) return;
      const next = Number(btn.dataset.page);
      if (!Number.isFinite(next) || next < 1) return;
      svcMenuPage = next;
      renderServiceMenu();
    });
  }
  if (input) {
    input.addEventListener("input", () => { svcMenuPage = 1; renderServiceMenu(); });
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") hideServiceMenu();
      if (ev.key === "Enter") {
        const q = String(input.value || "").trim().toLowerCase();
        if (q && ("全部微服务".includes(q) || q === "all")) {
          ev.preventDefault();
          selectService(ALL_SERVICES_ID);
          hideServiceMenu();
          return;
        }
        const first = sortedServices().find((item) => serviceMatches(item, input.value));
        if (first) {
          ev.preventDefault();
          selectService(first.id);
          hideServiceMenu();
        }
      }
    });
  }
}

async function refreshServices() {
  const data = await api("/api/services");
  services = data.services || [];
  favoriteServiceIds = Array.isArray(data.favorites) ? data.favorites : [];
  if ($("targetRepo") && data.registry) $("targetRepo").textContent = data.registry + "/" + data.org;
  if (!isKnownServiceId(currentServiceId)) {
    try { currentServiceId = localStorage.getItem(SERVICE_KEY) || ""; } catch (_) {}
    if (!isKnownServiceId(currentServiceId)) currentServiceId = (sortedServices()[0] && sortedServices()[0].id) || "";
  }
  fillServiceSelect();
  applyServiceChrome();
  updateActionButtons();
}

async function refreshHealth() {
  await api("/api/health");
}

function stopPolling() {
  pollGeneration += 1;
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
}

function applyJob(job, { loading } = {}) {
  if (job && !sessionLive) return;
  if (job && !isAllServices() && currentServiceId && !jobBelongsToService(job, currentServiceId)) return;
  currentJob = job;
  currentJobId = job && job.id || "";
  renderJobMeta(job);
  updateActionButtons();
  const empty = $("pipelineEmpty");
  const root = $("liveJobPipeline");
  const hasJob = !!(job && job.id);
  if (!hasJob) {
    if (root) { root.hidden = true; root.replaceChildren(); }
    if (empty) {
      empty.hidden = false;
      empty.textContent = loading ? "正在加载流水线…" : "当前服务无构建历史记录";
    }
    return;
  }
  if (empty) empty.hidden = true;
  renderJobPipeline("liveJobPipeline", job.pipeline, job.status, (stageId, taskId) => {
    openStepModal(stageId, taskId);
  });
}

async function openJob(jobId, { fromHistory } = {}) {
  if (!jobId || !sessionLive) return false;
  const gen = viewGeneration;
  const sidBefore = currentServiceId;
  stopPolling();
  const job = await api("/api/jobs/" + jobId + "?compact=1");
  if (!sessionLive) return false;
  const sid = jobServiceIds(job)[0] || "";
  if (fromHistory) {
    if (sid && sid !== currentServiceId && services.some((item) => item.id === sid)) {
      activateService(sid);
    }
    pinnedJobId = jobId;
    applyJob(job);
    setTab("pipeline");
    if (jobIsLive(job)) startPolling(jobId);
    refreshServiceOccupancy().catch(() => {});
    return true;
  }
  if (!viewIsCurrent(gen, sidBefore)) return false;
  if (!isAllServices() && sid && sid !== currentServiceId) return false;
  applyJob(job);
  if (jobIsLive(job)) startPolling(jobId);
  return true;
}

async function refreshServiceOccupancy() {
  if (!sessionLive || isAllServices() || !currentServiceId) {
    serviceActiveJobId = "";
    updateActionButtons();
    return;
  }
  try {
    const running = await api("/api/running-job?service_id=" + encodeURIComponent(currentServiceId));
    serviceActiveJobId = (running && running.id) || "";
  } catch (_) {
    serviceActiveJobId = "";
  }
  updateActionButtons();
}

async function loadServiceJob() {
  const gen = viewGeneration;
  const sid = currentServiceId;
  stopPolling();
  pinnedJobId = "";
  if (!sessionLive || !sid || sid === ALL_SERVICES_ID) {
    applyJob(null);
    return;
  }
  const btn = $("btnRefreshPipeline");
  beginRefresh(btn);
  const started = Date.now();
  if (currentJob && !jobBelongsToService(currentJob, sid)) applyJob(null, { loading: true });
  else if (!currentJob) applyJob(null, { loading: true });
  try {
    const running = await api("/api/running-job?service_id=" + encodeURIComponent(sid));
    if (!viewIsCurrent(gen, sid)) return;
    serviceActiveJobId = (running && running.id) || "";
    updateActionButtons();
    if (running && running.id && await openJob(running.id)) return;
    if (!viewIsCurrent(gen, sid)) return;
    const hist = await api("/api/jobs?service_id=" + encodeURIComponent(sid) + "&page=1&page_size=1");
    if (!viewIsCurrent(gen, sid)) return;
    const latest = hist.jobs && hist.jobs[0];
    if (latest && latest.id && await openJob(latest.id)) return;
    if (!viewIsCurrent(gen, sid)) return;
    applyJob(null);
  } catch (e) {
    if (!viewIsCurrent(gen, sid)) return;
    const empty = $("pipelineEmpty");
    const root = $("liveJobPipeline");
    if (root) { root.hidden = true; root.replaceChildren(); }
    if (empty) {
      empty.hidden = false;
      empty.textContent = "流水线加载失败，请稍后重试";
    }
  } finally {
    const wait = REFRESH_BUSY_MS - (Date.now() - started);
    if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
    endRefresh(btn);
  }
}

function startPolling(jobId) {
  stopPolling();
  if (!sessionLive) return;
  const generation = pollGeneration;
  const tick = async () => {
    if (!sessionLive || generation !== pollGeneration) return;
    try {
      const job = await api("/api/jobs/" + jobId + "?compact=1&view=ui&log_after=0");
      if (!sessionLive || generation !== pollGeneration) return;
      if (!isAllServices() && !jobBelongsToService(job, currentServiceId)) return;
      applyJob(job);
      if (jobIsLive(job)) pollTimer = setTimeout(tick, 1500);
      else refreshServiceOccupancy().catch(() => {});
    } catch (_) {
      if (sessionLive && generation === pollGeneration) pollTimer = setTimeout(tick, 2500);
    }
  };
  tick();
}

function closeModal() {
  document.querySelectorAll(".branch-menu-portal").forEach((menu) => menu.remove());
  $("modalRoot").hidden = true;
  $("modalBox").classList.remove("narrow", "wide");
  $("modalBody").replaceChildren();
}
function openModal(title, body, { narrow, wide } = {}) {
  $("modalTitle").textContent = title;
  $("modalBox").classList.toggle("narrow", !!narrow);
  $("modalBox").classList.toggle("wide", !!wide);
  $("modalBody").replaceChildren(body);
  $("modalRoot").hidden = false;
}

function runQueuedBranchLoads() {
  while (branchLoadsActive < BRANCH_LOAD_LIMIT && branchLoadQueue.length) {
    const item = branchLoadQueue.shift();
    branchLoadsActive += 1;
    Promise.resolve()
      .then(item.task)
      .then(item.resolve, item.reject)
      .finally(() => {
        branchLoadsActive -= 1;
        runQueuedBranchLoads();
      });
  }
}
function queueBranchLoad(task, priority) {
  return new Promise((resolve, reject) => {
    const item = { task, resolve, reject };
    if (priority) branchLoadQueue.unshift(item);
    else branchLoadQueue.push(item);
    runQueuedBranchLoads();
  });
}
function fetchServiceBranches(serviceId, force, priority) {
  if (!sessionLive) return Promise.reject(new Error("请先登录"));
  if (!force && branchCache[serviceId] && branchCache[serviceId].loaded) {
    return Promise.resolve(branchCache[serviceId]);
  }
  if (!force && branchLoads[serviceId]) return branchLoads[serviceId];
  const req = queueBranchLoad(() => {
    if (!sessionLive) return Promise.reject(new Error("请先登录"));
    const path = "/api/services/" + encodeURIComponent(serviceId) + "/branches" + (force ? "?refresh=1" : "");
    if (typeof AbortController === "undefined") return api(path);
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 30000);
    return api(path, { signal: controller.signal }).catch((error) => {
      if (timedOut) throw new Error("刷新分支超时，请稍后重试");
      throw error;
    }).finally(() => clearTimeout(timer));
  }, !!priority).then((data) => {
    if (!sessionLive) return data;
    const cached = {
      loaded: true,
      branches: data.branches || [],
      selected_branch: data.selected_branch || "",
      default_branch: data.default_branch || "",
      cached: data.cached === true || force,
      refresh_error: data.refresh_error || "",
    };
    branchCache[serviceId] = cached;
    delete branchLoads[serviceId];
    return cached;
  }).catch((err) => {
    delete branchLoads[serviceId];
    throw err;
  });
  branchLoads[serviceId] = req;
  return req;
}
function createBranchPicker(root, selectors) {
  const doc = root.ownerDocument || document;
  const win = doc.defaultView || window;
  const trigger = root.querySelector((selectors && selectors.trigger) || "#runBranch");
  const label = root.querySelector((selectors && selectors.label) || "#runBranchValue");
  const menu = root.querySelector((selectors && selectors.menu) || "#runBranchMenu");
  let value = "";
  const close = () => {
    menu.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    menu.remove();
  };
  const positionMenu = () => {
    const rect = trigger.getBoundingClientRect();
    const viewportGap = 12;
    const maxHeight = Math.max(120, Math.min(360, win.innerHeight - rect.bottom - viewportGap));
    menu.classList.add("branch-menu-portal");
    menu.style.left = rect.left + "px";
    menu.style.top = (rect.bottom + 6) + "px";
    menu.style.width = rect.width + "px";
    menu.style.maxHeight = maxHeight + "px";
    doc.body.appendChild(menu);
  };
  const setValue = (next) => {
    value = next;
    label.textContent = next;
    menu.querySelectorAll(".branch-option").forEach((option) => {
      const active = option.dataset.value === next;
      option.classList.toggle("active", active);
      option.setAttribute("aria-selected", String(active));
    });
  };
  trigger.addEventListener("click", () => {
    if (trigger.disabled) return;
    const opening = menu.hidden;
    if (opening) positionMenu();
    else close();
    menu.hidden = !opening;
    trigger.setAttribute("aria-expanded", String(opening));
  });
  root.addEventListener("focusout", () => {
    setTimeout(() => {
      if (!root.contains(doc.activeElement) && !menu.contains(doc.activeElement)) close();
    }, 0);
  });
  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      trigger.focus();
    }
  });
  return {
    setOptions(branches, selectedBranch, fallback) {
      const list = branches && branches.length ? branches : [selectedBranch || fallback || "main"];
      const wanted = list.includes(selectedBranch) ? selectedBranch : (list.includes(fallback) ? fallback : list[0]);
      menu.replaceChildren();
      list.forEach((name) => {
        const option = doc.createElement("button");
        option.type = "button";
        option.className = "branch-option";
        option.dataset.value = name;
        option.setAttribute("role", "option");
        option.textContent = name;
        option.addEventListener("click", () => {
          setValue(name);
          close();
          trigger.focus();
        });
        menu.appendChild(option);
      });
      setValue(wanted);
    },
    setDisabled(disabled) {
      trigger.disabled = disabled;
      if (disabled) close();
    },
    get value() { return value; },
  };
}

async function openRunDialog() {
  const svc = currentService();
  if (!svc) return;
  const wrap = document.createElement("div");
  wrap.innerHTML =
    '<p class="hint">微服务 ' + esc(svc.title) + '</p>' +
    '<label class="label">代码库地址</label>' +
    '<p class="run-repo" id="runRepo">' + esc(svc.github || svc.repo || "") + '</p>' +
    '<label class="label" for="runBranch">分支</label>' +
    '<div class="run-branch-row">' +
    '<div class="branch-picker" id="runBranchPicker">' +
    '<button type="button" class="input branch-trigger" id="runBranch" aria-haspopup="listbox" aria-expanded="false">' +
    '<span id="runBranchValue"></span>' +
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10l5 5 5-5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
    '</button>' +
    '<div class="branch-menu" id="runBranchMenu" role="listbox" hidden></div>' +
    '</div>' +
    '<button type="button" class="icon-btn" id="btnRefreshBranches" title="刷新" aria-label="刷新">' +
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.6 12A7.4 7.4 0 0 1 16.8 6.3M19.4 12A7.4 7.4 0 0 1 7.2 17.7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M16.2 3.8v4.4h-4.4M7.8 20.2v-4.4h4.4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
    '</button>' +
    '</div>' +
    '<p class="branch-status" id="runBranchStatus"></p>' +
    '<div class="row"><button type="button" class="btn ghost" data-close-modal>取消</button>' +
    '<button type="button" class="btn primary" id="btnRunConfirm">确定</button></div>';
  const branchPicker = createBranchPicker(wrap.querySelector("#runBranchPicker"));
  const statusEl = wrap.querySelector("#runBranchStatus");
  const retryBtn = wrap.querySelector("#btnRefreshBranches");
  const fallbackBranch = svc.default_branch || "main";
  const cached = branchCache[svc.id];
  const alreadyLoaded = !!(cached && cached.loaded);
  const selected = (cached && cached.selected_branch) || fallbackBranch;
  branchPicker.setOptions(cached && cached.branches, selected, (cached && cached.default_branch) || fallbackBranch);
  openModal("运行流水线", wrap, { narrow: true });
  if (!alreadyLoaded) {
    statusEl.textContent = "正在加载中";
    retryBtn.classList.add("loading");
    branchPicker.setDisabled(true);
  }
  const setLoading = (loading) => {
    retryBtn.classList.toggle("loading", loading);
    branchPicker.setDisabled(loading);
    if (loading) {
      statusEl.textContent = "正在加载中";
      statusEl.className = "branch-status";
    }
  };
  const applyCached = (data, updated) => {
    branchPicker.setOptions(
      data.branches,
      data.selected_branch || fallbackBranch,
      data.default_branch || fallbackBranch
    );
    const n = (data.branches || []).length;
    statusEl.textContent = updated
      ? ("已更新" + n + "个分支")
      : data.cached
        ? ("已读取服务器缓存 " + n + " 个分支")
        : "服务器暂无分支缓存，当前使用默认分支；点击刷新加载";
    statusEl.className = "branch-status ok";
  };
  const loadBranches = async (force) => {
    if (!force && branchCache[svc.id] && branchCache[svc.id].loaded) {
      applyCached(branchCache[svc.id], false);
      return;
    }
    setLoading(true);
    try {
      const data = await fetchServiceBranches(svc.id, force, true);
      applyCached(data, force);
    } catch (e) {
      branchPicker.setOptions([fallbackBranch], fallbackBranch, fallbackBranch);
      statusEl.textContent = e.message || "加载分支失败";
      statusEl.className = "branch-status error";
      delete branchCache[svc.id];
    } finally {
      retryBtn.classList.remove("loading");
      branchPicker.setDisabled(false);
    }
  };
  wrap.querySelector("#btnRefreshBranches").addEventListener("click", () => {
    loadBranches(true);
  });
  loadBranches(false);
  wrap.querySelector("#btnRunConfirm").addEventListener("click", () => startRun(branchPicker.value));
}

async function startRun(branch) {
  const svc = currentService();
  if (!svc || submitting) return;
  if (svc.requires_version && !DAEMON_VERSION_PATTERN.test(rememberedDaemonVersion() || svc.last_version || "")) {
    closeModal();
    openParamsDialog("运行前请先配置 Daemon version");
    return;
  }
    submitting = true;
  try {
    const resp = await api("/api/push", {
      method: "POST",
      body: JSON.stringify({
        service_id: svc.id,
        branch: branch || svc.default_branch || "main",
        version: rememberedDaemonVersion() || svc.last_version || "",
        login_command: ($("loginCmd") && $("loginCmd").value || "").trim(),
        optional_steps: {
          gamma_deploy: !!runPreviewSelection.deploy,
          gamma_test: !!runPreviewSelection.test,
          environment_id: runPreviewSelection.environmentId || "",
        },
      }),
    });
    closeModal();
    closeRunPopup();
    runPreviewActive = false;
    runLayerMode = "run";
    runLayerTemplateId = "";
    applyServiceChrome();
    pinnedJobId = "";
    setTab("pipeline");
    await openJob(resp.job_id);
    refreshServiceOccupancy().catch(() => {});
  } catch (e) {
    const code = e.data && e.data.error_code;
    if (code === "service_busy" || code === "concurrency_limit") {
      alert(e.message || "当前无法启动新的构建");
    } else if (e.status === 503) {
      alert(e.message || "服务暂时繁忙，请稍后重试");
    } else {
      alert(e.message || "启动失败");
    }
  } finally {
    submitting = false;
  }
}

function openStopDialog() {
  if (!currentJobId || !jobIsLive(currentJob)) return;
  const wrap = document.createElement("div");
  wrap.innerHTML =
    '<p>确认停止当前任务？停止后无法恢复，需要重新运行。</p>' +
    '<div class="row"><button type="button" class="btn ghost" data-close-modal>取消</button>' +
    '<button type="button" class="btn danger" id="btnStopConfirm">确认停止</button></div>';
  openModal("停止当前任务", wrap, { narrow: true });
  wrap.querySelector("#btnStopConfirm").addEventListener("click", async () => {
    try {
      await api("/api/jobs/" + currentJobId + "/stop", { method: "POST", body: "{}" });
      if (currentJob) {
        currentJob.cancel_requested = true;
        currentJob.stage_before_stop = currentJob.stage;
        currentJob.stage = "stopping";
        applyJob(currentJob);
      }
      closeModal();
      refreshServiceOccupancy().catch(() => {});
    } catch (e) {
      alert(e.message || "停止失败");
    }
  });
}

function openParamsDialog(notice) {
  const svc = currentService();
  const wrap = document.createElement("div");
  const example = (svc && svc.version_example) || "v1.2.3";
  const last = (svc && svc.last_version) || "";
  const cached = rememberedDaemonVersion();
  wrap.innerHTML =
    (notice ? '<p class="hint error">' + esc(notice) + "</p>" : "") +
    '<div class="param-daemon-row">' +
    '<label class="label" for="paramVersion">Daemon version（例如 ' + esc(example) + "）</label>" +
    '<input class="input" id="paramVersion" />' +
    "</div>" +
    '<p class="hint">Daemon版本变化时需要修改版本号，无变化时可不修改</p>' +
    '<div class="row"><button type="button" class="btn ghost" data-close-modal>取消</button>' +
    '<button type="button" class="btn primary" id="btnParamSave">保存</button></div>';
  wrap.querySelector("#paramVersion").value = cached || last || "";
  openModal("参数配置 · " + ((svc && svc.title) || "multica-server"), wrap, { narrow: true });
  wrap.querySelector("#btnParamSave").addEventListener("click", () => {
    const value = wrap.querySelector("#paramVersion").value.trim();
    if (value && !DAEMON_VERSION_PATTERN.test(value)) {
      alert("格式必须为 vMAJOR.MINOR.PATCH，例如 " + example);
      return;
    }
    if (value) rememberDaemonVersion(value);
    closeModal();
  });
}

function decodeUnicodeEscapes(value) {
  return String(value || "").replace(/(?:\\u[0-9a-fA-F]{4})+/g, (sequence) => {
    try { return JSON.parse('"' + sequence + '"'); } catch (_) { return sequence; }
  });
}
function shortCaseName(name) {
  const value = decodeUnicodeEscapes(name);
  if (value.startsWith("Test") || value.startsWith("test_")) return value;
  const parts = value.split(".");
  return parts.length > 2 ? parts.slice(-2).join(".") : value;
}
function filterRun(run, kind) {
  if (!run || !kind) return run;
  const cases = casesForKind(run, kind);
  const commands = (run.commands || []).filter((item) => commandTestKind(item) === kind);
  const passed = cases.filter((item) => item.status === "passed").length;
  const skipped = cases.filter((item) => item.status === "skipped").length;
  const errors = cases.filter((item) => item.status === "error").length;
  const failed = Math.max(0, cases.length - passed - skipped - errors);
  const status = errors ? "error" : (failed || commands.some((item) => Number(item.exit_code) !== 0) ? "failed" : "passed");
  return {
    ...run,
    status,
    commands,
    test_cases: cases,
    summary: { total: cases.length, passed, failed, errors, skipped },
  };
}
function renderTestRun(root, run, heading) {
  if (!run) {
    root.textContent = "暂无测试结果。";
    return;
  }
  const summary = run.summary || {};
  const testStatus = run.status || "error";
  const isOk = testStatus === "passed";
  const head = document.createElement("div");
  head.className = "test-result-head";
  const title = document.createElement("strong");
  title.className = "test-status " + (isOk ? "ok" : "bad");
  title.textContent = (heading || "测试结果") + "：" + (isOk ? "通过" : "未通过");
  const overview = document.createElement("span");
  overview.className = "test-result-summary";
  overview.textContent = "总用例 " + (summary.total || 0) + " · 通过 " + (summary.passed || 0) +
    " · 失败 " + (summary.failed || 0) + " · 错误 " + (summary.errors || 0);
  head.append(title, overview);
  root.appendChild(head);
  const cases = Array.isArray(run.test_cases) ? run.test_cases : [];
  if (!cases.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "暂无用例明细。";
    root.appendChild(empty);
    return;
  }
  const key = heading || "all";
  const state = testTableState.get(key) || { page: 1, pageSize: 20 };
  const pageCount = Math.max(1, Math.ceil(cases.length / state.pageSize));
  state.page = Math.min(Math.max(1, state.page), pageCount);
  testTableState.set(key, state);
  const table = document.createElement("table");
  table.className = "test-table";
  table.innerHTML = "<thead><tr><th>用例</th><th>结果</th><th>耗时</th></tr></thead>";
  const body = document.createElement("tbody");
  const start = (state.page - 1) * state.pageSize;
  cases.slice(start, start + state.pageSize).forEach((item) => {
    const row = document.createElement("tr");
    const name = document.createElement("td");
    name.textContent = shortCaseName(item.name);
    const status = document.createElement("td");
    const passed = item.status === "passed";
    status.textContent = passed ? "通过" : item.status === "skipped" ? "跳过" : "失败";
    status.className = "test-status " + (passed ? "ok" : item.status === "skipped" ? "" : "bad");
    const dur = document.createElement("td");
    dur.textContent = item.duration_ms == null ? "—" : formatDuration(item.duration_ms);
    row.append(name, status, dur);
    body.appendChild(row);
  });
  table.appendChild(body);
  root.appendChild(table);
  const pager = document.createElement("div");
  pager.className = "test-pager";
  const sizeWrap = document.createElement("label");
  sizeWrap.className = "test-page-size";
  sizeWrap.append("每页 ");
  const size = document.createElement("select");
  size.className = "input";
  [10, 20, 50].forEach((n) => {
    const opt = document.createElement("option");
    opt.value = String(n);
    opt.textContent = String(n);
    if (n === state.pageSize) opt.selected = true;
    size.appendChild(opt);
  });
  size.addEventListener("change", () => {
    state.pageSize = Number(size.value) || 20;
    state.page = 1;
    testTableState.set(key, state);
    root.replaceChildren();
    renderTestRun(root, run, heading);
  });
  sizeWrap.append(size, " 条");
  const prev = document.createElement("button");
  prev.type = "button";
  prev.className = "btn ghost";
  prev.textContent = "上一页";
  prev.disabled = state.page <= 1;
  prev.addEventListener("click", () => {
    state.page -= 1;
    testTableState.set(key, state);
    root.replaceChildren();
    renderTestRun(root, run, heading);
  });
  const next = document.createElement("button");
  next.type = "button";
  next.className = "btn ghost";
  next.textContent = "下一页";
  next.disabled = state.page >= pageCount;
  next.addEventListener("click", () => {
    state.page += 1;
    testTableState.set(key, state);
    root.replaceChildren();
    renderTestRun(root, run, heading);
  });
  const pos = document.createElement("span");
  pos.textContent = "第 " + state.page + " / " + pageCount + " 页（" + cases.length + " 条）";
  pager.append(sizeWrap, prev, pos, next);
  root.appendChild(pager);
}

function flattenPipelineSteps(pipeline) {
  return buildPipelineStages(pipeline || { steps: [] }, currentJob && currentJob.status);
}
function appendStepButton(list, label, status, className, active, onClick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = (className || "") + (active ? " active" : "");
  if (status) {
    const dot = document.createElement("span");
    dot.className = "step-dot is-" + (status || "pending");
    btn.appendChild(dot);
  }
  const text = document.createElement("span");
  text.textContent = label;
  btn.appendChild(text);
  btn.addEventListener("click", onClick);
  list.appendChild(btn);
}
function appendStepHeading(list, label, status, className) {
  const row = document.createElement("div");
  row.className = "step-heading " + (className || "");
  const dot = document.createElement("span");
  dot.className = "step-dot is-" + (status || "pending");
  const text = document.createElement("span");
  text.textContent = label;
  row.append(dot, text);
  list.appendChild(row);
}
function isCaseTaskId(id) {
  return id === "ut-cases" || id === "dt-cases";
}
function parseCaseSelection(selected) {
  const match = String(selected || "").match(/^(ut-cases|dt-cases):(results|log)$/);
  if (!match) return null;
  return { taskId: match[1], view: match[2] };
}
function normalizeStepSelection(taskId, stage) {
  if (isCaseTaskId(taskId)) return taskId + ":results";
  return taskId || (stage.tasks && stage.tasks[0] && stage.tasks[0].id) || stage.id;
}

async function openStepModal(stageId, taskId) {
  if (!currentJobId) return;
  const stages = flattenPipelineSteps(currentJob && currentJob.pipeline);
  const stage = stages.find((item) => item.id === stageId) || stages[0];
  if (!stage) return;
  const wrap = document.createElement("div");
  wrap.className = "step-modal";
  const list = document.createElement("div");
  list.className = "step-list";
  const pane = document.createElement("div");
  pane.className = "step-log-pane";
  const tableWrap = document.createElement("div");
  tableWrap.className = "step-tests";
  tableWrap.hidden = true;
  const logEl = document.createElement("pre");
  logEl.className = "log step-log";
  pane.append(tableWrap, logEl);
  wrap.append(list, pane);
  openModal("步骤详情", wrap, { wide: true });

  let selectedTask = normalizeStepSelection(taskId, stage);
  if (parseCaseSelection(selectedTask)) logEl.hidden = true;
  const follow = newLogFollowState();
  bindLogFollow(logEl, follow);

  const paintList = () => {
    list.replaceChildren();
    appendStepButton(list, stage.label || stage.id, stage.status, "", selectedTask === stage.id, () => {
      selectedTask = stage.id;
      paintList();
      loadStep();
    });
    (stage.tasks || []).forEach((task) => {
      if (isCaseTaskId(task.id)) {
        appendStepHeading(list, task.label || task.id, task.status, "sub");
        appendStepButton(list, "测试结果", "", "sub nested", selectedTask === task.id + ":results", () => {
          selectedTask = task.id + ":results";
          paintList();
          loadStep();
        });
        appendStepButton(list, "日志详情", "", "sub nested", selectedTask === task.id + ":log", () => {
          selectedTask = task.id + ":log";
          paintList();
          loadStep();
        });
        return;
      }
      appendStepButton(list, task.label || task.id, task.status, "sub", selectedTask === task.id, () => {
        selectedTask = task.id;
        paintList();
        loadStep();
      });
    });
  };

  const loadStep = async () => {
    const caseSel = parseCaseSelection(selectedTask);
    const selected = caseSel
      ? (stage.tasks || []).find((task) => task.id === caseSel.taskId)
      : (stage.tasks || []).find((task) => task.id === selectedTask);
    const logStep = (selected && selected.logStep) || stage.logStep || stage.id;
    const sub = caseSel ? caseSel.taskId : (selectedTask && selectedTask !== stage.id ? selectedTask : "");
    try {
      const job = await api("/api/jobs/" + currentJobId + "?step=" + encodeURIComponent(logStep) + (sub ? "&sub=" + encodeURIComponent(sub) : "") + "&view=ui");
      const lines = Array.isArray(job.log) ? job.log : [];
      const run = (job.test_runs && job.test_runs[0]) || currentTestRun(currentJob);
      tableWrap.replaceChildren();
      if (caseSel && caseSel.view === "results") {
        logEl.hidden = true;
        tableWrap.hidden = false;
        if (caseSel.taskId === "ut-cases") renderTestRun(tableWrap, filterRun(run, "ut"), "UT 测试结果");
        else renderTestRun(tableWrap, filterRun(run, "dt"), "DT 测试结果");
        return;
      }
      tableWrap.hidden = true;
      logEl.hidden = false;
      const failedSelection = selected || stage;
      const emptyLog = failedSelection && failedSelection.status === "failed" && job.error
        ? "失败原因：\n" + job.error
        : (sub ? "该小步骤暂无独立日志。" : "该步骤暂无日志。");
      paintLog(logEl, lines.length ? lines.join("\n") : emptyLog, follow);
    } catch (e) {
      tableWrap.hidden = true;
      logEl.hidden = false;
      paintLog(logEl, e.message || "加载日志失败", follow);
    }
  };

  paintList();
  loadStep();
}

function openPasswordDialog() {
  const wrap = document.createElement("div");
  wrap.innerHTML =
    passwordFieldHtml("oldPass", "当前密码", "current-password") +
    passwordFieldHtml("newPass", "新密码", "new-password") +
    passwordFieldHtml("newPass2", "确认新密码", "new-password") +
    '<div class="row"><button type="button" class="btn ghost" data-close-modal>取消</button>' +
    '<button type="button" class="btn primary" id="btnPassSave">保存</button></div>';
  openModal("修改密码", wrap, { narrow: true });
  bindEyeButtons(wrap);
  wrap.querySelector("#btnPassSave").addEventListener("click", async () => {
    const next = wrap.querySelector("#newPass").value;
    const again = wrap.querySelector("#newPass2").value;
    if (next !== again) {
      alert("两次输入的新密码不一致");
      return;
    }
    try {
      await api("/api/auth/password", {
        method: "POST",
        body: JSON.stringify({
          old_password: wrap.querySelector("#oldPass").value,
          new_password: next,
        }),
      });
      closeModal();
    } catch (e) {
      alert(e.message || "修改失败");
    }
  });
}

async function loginSWR() {
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
async function checkSWR() {
  $("loginLog").textContent = "检查中…";
  try {
    const data = await api("/api/login/check", { method: "POST", body: "{}" });
    $("loginLog").textContent = data.ok ? "登录有效" : "未登录或已过期";
  } catch (e) {
    $("loginLog").textContent = e.message;
  }
}
async function logoutSWR() {
  try {
    const data = await api("/api/login/logout", { method: "POST", body: "{}" });
    $("loginLog").textContent = data.output || "已退出";
  } catch (e) {
    $("loginLog").textContent = e.message;
  }
}

function setEnvError(text) {
  const el = $("envError");
  if (!el) return;
  el.hidden = !text;
  el.textContent = text || "";
}
function setEnvFormError(text) {
  const el = $("envFormError");
  if (!el) return;
  el.hidden = !text;
  el.textContent = text || "";
}
function envIconPencil() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 16.8 15.6 6.2a1.4 1.4 0 0 1 2 0l1.2 1.2a1.4 1.4 0 0 1 0 2L8.2 20H5v-3.2z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>';
}
function envIconTrash() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 8h10M9.5 8V6.6A1.6 1.6 0 0 1 11.1 5h1.8A1.6 1.6 0 0 1 14.5 6.6V8M8.4 8.8l.6 10.2h6l.6-10.2" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
}
function envIconPlus() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 7v10M7 12h10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
}
function emptyEnvDraft() {
  const svc = currentService();
  const sid = svc ? svc.id : "";
  return {
    name: "",
    service_id: sid,
    region: "cn-southwest-2",
    cluster_name: "",
    workload_name: sid,
    jump_host: "",
    nodes: [""],
  };
}
function collectEnvForm() {
  const nodes = Array.from(document.querySelectorAll("[data-env-node]"))
    .map((el) => (el.value || "").trim())
    .filter(Boolean);
  return {
    name: ($("envName") && $("envName").value || "").trim(),
    service_id: currentServiceId && !isAllServices() ? currentServiceId : "",
    region: ($("envRegion") && $("envRegion").value) || "cn-southwest-2",
    cluster_name: ($("envCluster") && $("envCluster").value || "").trim(),
    workload_name: ($("envWorkload") && $("envWorkload").value || "").trim(),
    jump_host: ($("envJump") && $("envJump").value || "").trim(),
    jump_password: ($("envJumpPassword") && $("envJumpPassword").value) || "",
    node_password: ($("envNodePassword") && $("envNodePassword").value) || "",
    nodes,
  };
}
function envNodeRowsHtml(nodes) {
  const list = nodes && nodes.length ? nodes : [""];
  return list.map((node) => (
    '<div class="env-node-row">' +
    '<input class="input" data-env-node type="text" value="' + esc(node) + '" placeholder="例如 172.31.8.33" />' +
    '<button type="button" class="btn ghost danger" data-env-remove-node>删除</button>' +
    "</div>"
  )).join("");
}
function envFormHtml(env) {
  const data = env || emptyEnvDraft();
  const creating = !data.id;
  const jumpPassType = data.has_jump_password ? "password" : "text";
  const nodePassType = data.has_node_password ? "password" : "text";
  const jumpPassPh = data.has_jump_password ? "已保存，留空不修改" : "跳板机 SSH 密码";
  const nodePassPh = data.has_node_password ? "已保存，留空不修改" : "CCE 节点 SSH 密码";
  const secretHint = data.has_jump_password && data.has_node_password
    ? "已保存的密码不会回显。要更换时重新输入，留空则保持不变。"
    : "仅首次填写时明文可见，保存后任何人都不会再看到密码。";
  const owned = serviceTitle(services.find((item) => item.id === data.service_id) || { id: data.service_id, title: data.service_id });
  return (
    '<div class="env-form-card">' +
    '<div class="env-meta-row"><span class="env-meta-label">所属微服务:</span><strong class="env-meta-value">' + esc(owned || "—") + "</strong></div>" +
    '<label class="label" for="envRegion">Region</label>' +
    '<select id="envRegion" class="input"><option value="cn-southwest-2">贵阳一</option></select>' +
    '<label class="label" for="envCluster">集群名称</label>' +
    '<input id="envCluster" class="input" value="' + esc(data.cluster_name || "") + '" required />' +
    '<label class="label" for="envWorkload">负载名称</label>' +
    '<input id="envWorkload" class="input" value="' + esc(data.workload_name || "") + '" placeholder="CCE 集群中的微服务名称" required />' +
    '<label class="label" for="envJump">跳板机</label>' +
    '<input id="envJump" class="input" value="' + esc(data.jump_host || "") + '" placeholder="用于 SSH 到 CCE 集群" required />' +
    '<label class="label" for="envJumpPassword">跳板机密码</label>' +
    '<input id="envJumpPassword" class="input" type="' + jumpPassType + '" value="" placeholder="' + esc(jumpPassPh) + '" autocomplete="new-password" spellcheck="false"' + (creating ? " required" : "") + " />" +
    '<div class="label">节点列表</div>' +
    '<div id="envNodeList">' + envNodeRowsHtml(data.nodes) + "</div>" +
    '<div class="row"><button type="button" class="btn ghost" id="btnEnvAddNode">' + envIconPlus() + " 添加节点</button></div>" +
    '<label class="label" for="envNodePassword">节点密码</label>' +
    '<input id="envNodePassword" class="input" type="' + nodePassType + '" value="" placeholder="' + esc(nodePassPh) + '" autocomplete="new-password" spellcheck="false"' + (creating ? " required" : "") + " />" +
    '<p class="env-secret-hint">' + secretHint + "</p>" +
    '<label class="label" for="envName">环境名称</label>' +
    '<input id="envName" class="input" value="' + esc(data.name || "") + '" required />' +
    '<div class="row env-form-actions">' +
    '<button type="button" class="btn primary" id="btnEnvSave">保存</button>' +
    '<button type="button" class="btn ghost" id="btnEnvCancel">取消</button>' +
    "</div></div>"
  );
}
function envCardHtml(env) {
  return (
    '<article class="env-card" data-env-id="' + esc(env.id) + '">' +
    '<div class="env-card-head">' +
    '<div class="env-card-name">' + esc(env.name || "未命名环境") + "</div>" +
    '<div class="env-card-actions">' +
    '<button type="button" class="env-icon-btn" data-env-edit="' + esc(env.id) + '" title="编辑">' + envIconPencil() + "</button>" +
    '<button type="button" class="env-icon-btn danger" data-env-delete="' + esc(env.id) + '" title="删除">' + envIconTrash() + "</button>" +
    "</div></div>" +
    '<div class="env-card-meta">' +
    '<div class="env-meta-row"><span class="env-meta-label">微服务:</span><strong class="env-meta-value">' + esc(serviceTitle(services.find((item) => item.id === env.service_id) || { id: env.service_id, title: env.service_id }) || "—") + "</strong></div>" +
    '<div class="env-meta-row"><span class="env-meta-label">Region:</span><strong class="env-meta-value">' + esc(env.region_label || env.region || "—") + "</strong></div>" +
    '<div class="env-meta-row"><span class="env-meta-label">集群:</span><strong class="env-meta-value">' + esc(env.cluster_name || "—") + "</strong></div>" +
    '<div class="env-meta-row"><span class="env-meta-label">负载:</span><strong class="env-meta-value">' + esc(env.workload_name || "—") + "</strong></div>" +
    "</div></article>"
  );
}
function bindEnvForm() {
  const list = $("envNodeList");
  if (list) {
    list.addEventListener("click", (ev) => {
      const add = ev.target.closest("[data-env-add-node]");
      const remove = ev.target.closest("[data-env-remove-node]");
      if (add) {
        const row = document.createElement("div");
        row.className = "env-node-row";
        row.innerHTML = '<input class="input" data-env-node type="text" placeholder="例如 172.31.8.33" />' +
          '<button type="button" class="btn ghost danger" data-env-remove-node>删除</button>';
        list.appendChild(row);
        return;
      }
      if (remove) {
        const row = remove.closest(".env-node-row");
        if (row && list.querySelectorAll(".env-node-row").length > 1) row.remove();
      }
    });
  }
  bind("btnEnvAddNode", () => {
    const list = $("envNodeList");
    if (!list) return;
    const row = document.createElement("div");
    row.className = "env-node-row";
    row.innerHTML = '<input class="input" data-env-node type="text" placeholder="例如 172.31.8.33" />' +
      '<button type="button" class="btn ghost danger" data-env-remove-node>删除</button>';
    list.appendChild(row);
  });
  bind("btnEnvSave", () => saveEnvironment().catch((e) => setEnvFormError(e.message)));
  bind("btnEnvCancel", () => closeEnvOverlay());
}
function syncEnvOverlayLock() {
  const formOpen = $("envOverlay") && !$("envOverlay").hidden;
  const confirmOpen = $("envConfirm") && !$("envConfirm").hidden;
  document.body.classList.toggle("env-overlay-open", !!(formOpen || confirmOpen));
}
function closeEnvOverlay() {
  const overlay = $("envOverlay");
  if (!overlay) return;
  overlay.hidden = true;
  envDraftOpen = false;
  envEditingId = "";
  setEnvFormError("");
  const body = $("envOverlayBody");
  if (body) body.replaceChildren();
  syncEnvOverlayLock();
}
function openEnvOverlay(env) {
  const overlay = $("envOverlay");
  const body = $("envOverlayBody");
  const title = $("envOverlayTitle");
  if (!overlay || !body || !title) return;
  envDraftOpen = !env || !env.id;
  envEditingId = env && env.id ? env.id : "";
  title.textContent = envEditingId ? "编辑环境" : "创建环境";
  setEnvError("");
  setEnvFormError("");
  body.innerHTML = envFormHtml(env || emptyEnvDraft());
  bindEnvForm();
  overlay.hidden = false;
  syncEnvOverlayLock();
  if ($("envRegion")) $("envRegion").focus();
}
function upsertEnvironment(item) {
  if (!item || !item.id) return;
  const idx = environments.findIndex((env) => env.id === item.id);
  if (idx >= 0) environments[idx] = { ...environments[idx], ...item };
  else environments = [item, ...environments];
}

function renderEnvironments() {
  const grid = $("envGrid");
  if (!grid) return;
  if (!environments.length) {
    grid.innerHTML = isAllServices()
      ? '<p class="env-empty">请先选择一个微服务，再管理它的环境。</p>'
      : "";
    return;
  }
  grid.innerHTML = environments.map(envCardHtml).join("");
  grid.querySelectorAll("[data-env-edit]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-env-edit") || "";
      const env = environments.find((item) => item.id === id);
      if (env) openEnvOverlay(env);
    });
  });
  grid.querySelectorAll("[data-env-delete]").forEach((btn) => {
    btn.addEventListener("click", () => deleteEnvironment(btn.getAttribute("data-env-delete") || "").catch((e) => setEnvError(e.message)));
  });
}
async function loadEnvironments() {
  setEnvError("");
  if (isAllServices() || !currentServiceId) {
    environments = [];
    renderEnvironments();
    return;
  }
  const data = await api("/api/environments?service_id=" + encodeURIComponent(currentServiceId));
  environments = data.environments || [];
  renderEnvironments();
}
function openEnvCreate() {
  if (isAllServices() || !currentService()) {
    setEnvError("请先选择一个微服务，再创建它的环境。");
    return;
  }
  openEnvOverlay(emptyEnvDraft());
}
async function saveEnvironment() {
  setEnvFormError("");
  const payload = collectEnvForm();
  if (!envEditingId) {
    if (!payload.service_id) {
      setEnvFormError("请先选择一个微服务");
      return;
    }
    if (!payload.jump_password) {
      setEnvFormError("跳板机密码必填");
      return;
    }
    if (!payload.node_password) {
      setEnvFormError("节点密码必填");
      return;
    }
  }
  const path = envEditingId ? "/api/environments/" + encodeURIComponent(envEditingId) : "/api/environments";
  const data = await api(path, { method: "POST", body: JSON.stringify(payload) });
  upsertEnvironment(data.environment);
  closeEnvOverlay();
  renderEnvironments();
  try {
    await loadEnvironments();
  } catch (e) {
    setEnvError(e.message);
  }
}
let envConfirmResolver = null;
function closeEnvConfirm(ok) {
  const overlay = $("envConfirm");
  if (overlay) overlay.hidden = true;
  syncEnvOverlayLock();
  const resolve = envConfirmResolver;
  envConfirmResolver = null;
  if (resolve) resolve(!!ok);
}
function askEnvConfirm(message) {
  return new Promise((resolve) => {
    if (envConfirmResolver) envConfirmResolver(false);
    envConfirmResolver = resolve;
    const text = $("envConfirmText");
    if (text) text.textContent = message;
    if ($("envConfirm")) $("envConfirm").hidden = false;
    syncEnvOverlayLock();
    if ($("btnEnvConfirmCancel")) $("btnEnvConfirmCancel").focus();
  });
}
async function deleteEnvironment(id) {
  if (!id) return;
  const env = environments.find((item) => item.id === id);
  const name = (env && env.name) || id;
  const ok = await askEnvConfirm("确定删除环境「" + name + "」？删除后无法恢复。");
  if (!ok) return;
  setEnvError("");
  await api("/api/environments/" + encodeURIComponent(id) + "/delete", { method: "POST", body: "{}" });
  if (envEditingId === id) envEditingId = "";
  environments = environments.filter((item) => item.id !== id);
  renderEnvironments();
  try {
    await loadEnvironments();
  } catch (e) {
    setEnvError(e.message);
  }
}

function bind(id, fn) {
  const el = $(id);
  if (el) el.addEventListener("click", fn);
}

$("loginForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("loginError").hidden = true;
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username: $("authUser").value.trim(), password: $("authPass").value }),
    });
    await bootApp(data.username);
  } catch (e) {
    $("loginError").hidden = false;
    $("loginError").textContent = e.message || "登录失败";
  }
});

document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => setNav(btn.dataset.nav));
});
document.querySelectorAll(".subtab[data-tab]").forEach((btn) => {
  btn.addEventListener("click", () => setTab(btn.dataset.tab));
});
bindServicePicker();
bindEyeButtons(document);
bind("btnRun", () => openRunWindow());
bind("btnTplEdit", () => {
  const tpl = selectedPipelineTemplate();
  if (tpl) openEditTemplate(tpl.id);
});
bind("btnTplCopy", () => {
  const tpl = selectedPipelineTemplate();
  if (tpl) openCloneWindow(tpl.id);
});
bind("btnCloneConfirm", () => confirmClone().catch((e) => setCloneHint(e.message || "克隆失败")));
document.querySelectorAll("[data-clone-close]").forEach((el) => {
  el.addEventListener("click", () => closeCloneWindow());
});
bind("btnTplDelete", () => {
  const tpl = selectedPipelineTemplate();
  if (tpl) confirmDeleteTemplate(tpl.id);
});
function bindTplPicker() {
  const trigger = $("tplTrigger");
  const menu = $("tplMenu");
  if (!trigger || trigger.dataset.bound === "1") return;
  trigger.dataset.bound = "1";
  trigger.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (!menu) return;
    const opening = menu.hidden;
    if (opening) {
      hideServiceMenu();
      menu.hidden = false;
    } else {
      menu.hidden = true;
    }
    trigger.setAttribute("aria-expanded", String(opening));
  });
  if (menu) menu.addEventListener("click", (ev) => ev.stopPropagation());
}
bindTplPicker();
bind("btnRunPreviewConfirm", () => confirmRunPreview());
bind("btnRunPreviewCancel", () => cancelRunPreview());
bind("btnRefreshPreviewBranches", () => loadPreviewBranches(true));
bind("btnStop", () => openStopDialog());
bind("btnParams", () => openParamsDialog());
bind("btnRefreshPipeline", () => refreshPipeline().catch(() => {}));
bind("btnRefreshHistory", () => refreshHistory().catch(() => {}));
bind("btnRefreshArtifacts", () => refreshArtifacts().catch(() => {}));
bind("btnLogin", () => loginSWR());
bind("btnCheckLogin", () => checkSWR());
bind("btnLogout", () => logoutSWR());
bind("btnSaveToken", async () => {
  await api("/api/config/token", { method: "POST", body: JSON.stringify({ github_token: $("tokenInput").value.trim() }) });
  $("tokenInput").value = "";
  await refreshHealth();
});
bind("btnUserMenu", () => {
  const menu = $("userMenu");
  menu.hidden = !menu.hidden;
  $("btnUserMenu").setAttribute("aria-expanded", String(!menu.hidden));
});
bind("btnChangePass", () => { closeUserMenu(); openPasswordDialog(); });
bind("btnAuthLogout", async () => {
  closeUserMenu();
  showLogin();
  try {
    await api("/api/auth/logout", { method: "POST", body: "{}" });
  } catch (_) {}
});
bind("btnEnvCreate", () => openEnvCreate());
document.querySelectorAll("[data-env-close]").forEach((el) => el.addEventListener("click", closeEnvOverlay));
bind("btnEnvConfirmOk", () => closeEnvConfirm(true));
document.querySelectorAll("[data-env-confirm-cancel]").forEach((el) => {
  el.addEventListener("click", () => closeEnvConfirm(false));
});
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  if ($("envConfirm") && !$("envConfirm").hidden) {
    closeEnvConfirm(false);
    return;
  }
  if ($("envOverlay") && !$("envOverlay").hidden) {
    closeEnvOverlay();
    return;
  }
  if (cloneLayerOpen()) {
    closeCloneWindow();
    return;
  }
  if (runPreviewActive) cancelRunPreview();
});
document.querySelectorAll("[data-close-modal]").forEach((el) => el.addEventListener("click", closeModal));
$("modalRoot").addEventListener("click", (ev) => {
  if (ev.target && ev.target.getAttribute("data-close-modal") != null) closeModal();
});
document.addEventListener("click", (ev) => {
  if (!ev.target.closest(".user-wrap")) closeUserMenu();
  if (ev.target.isConnected && !ev.target.closest("#svcPicker")) hideServiceMenu();
  if (ev.target.isConnected && !ev.target.closest("#tplPicker")) hideTplMenu();
});
window.addEventListener("message", (ev) => {
  if (ev.origin !== location.origin) return;
  const data = ev.data;
  if (!data || data.type !== "robot-ci-job-started" || !data.jobId) return;
  if (data.serviceId && data.serviceId !== currentServiceId && isKnownServiceId(data.serviceId)) {
    activateService(data.serviceId);
  }
  pinnedJobId = data.jobId;
  setTab("pipeline");
  openJob(data.jobId).catch(() => {});
  refreshServiceOccupancy().catch(() => {});
});
window.addEventListener("hashchange", async () => {
  if (!sessionLive) return;
  const parsed = readHash();
  if (parsed.nav === "run") return;
  if (
    parsed.nav === nav &&
    parsed.service === (currentServiceId || "") &&
    parsed.tab === tab &&
    (parsed.job || "") === (pinnedJobId || "")
  ) return;
  if (parsed.nav !== nav) setNav(parsed.nav);
  if (parsed.nav !== "build") return;
  if (parsed.service && parsed.service !== currentServiceId && isKnownServiceId(parsed.service)) {
    activateService(parsed.service);
    if (isAllServices()) {
      stopPolling();
      applyJob(null);
      if (tab === "pipeline" || parsed.tab === "pipeline") setTab(allServicesFallbackTab(parsed.tab));
      else if (parsed.tab !== tab) setTab(parsed.tab);
      else if (tab === "history") refreshHistory().catch(() => {});
      else if (tab === "artifacts") refreshArtifacts().catch(() => {});
      else if (tab === "envs") loadEnvironments().catch((e) => setEnvError(e.message));
      return;
    }
    if (parsed.job) {
      pinnedJobId = parsed.job;
      if (parsed.tab !== tab) setTab(parsed.tab);
      loadPipelineTemplates().catch(() => {});
      await openJob(parsed.job);
      return;
    }
    if (parsed.tab !== tab) setTab(parsed.tab);
    if (tab === "pipeline") {
      loadPipelineTemplates().catch(() => {});
      await loadServiceJob();
    }
    else {
      stopPolling();
      refreshServiceOccupancy().catch(() => {});
      if (tab === "history") refreshHistory().catch(() => {});
      else if (tab === "artifacts") refreshArtifacts().catch(() => {});
      else if (tab === "envs") loadEnvironments().catch((e) => setEnvError(e.message));
    }
    return;
  }
  if (parsed.job && parsed.job !== pinnedJobId) {
    pinnedJobId = parsed.job;
    if (parsed.tab !== tab) setTab(parsed.tab);
    await openJob(parsed.job);
    return;
  }
  if (parsed.tab !== tab) setTab(parsed.tab);
});

async function bootApp(username) {
  const parsed = readHash();
  enterApp(username);
  if (parsed.service) currentServiceId = parsed.service;
  await Promise.all([refreshHealth(), refreshServices()]);
  setNav(parsed.nav, true);
  if (nav === "build") {
    applyServiceChrome();
    if (isAllServices()) {
      setTab(allServicesFallbackTab(parsed.tab));
    } else if (parsed.job) {
      pinnedJobId = parsed.job;
      setTab("pipeline");
      loadPipelineTemplates().catch(() => {});
      await openJob(parsed.job);
      refreshServiceOccupancy().catch(() => {});
    } else {
      setTab(parsed.tab);
      if (tab === "pipeline") {
        loadPipelineTemplates().catch(() => {});
        await loadServiceJob();
      } else refreshServiceOccupancy().catch(() => {});
    }
  } else {
    writeHash();
  }
}

(async function boot() {
  try {
    const me = await api("/api/auth/me");
    if (me.user) await bootApp(me.user);
    else showLogin();
  } catch (_) {
    showLogin();
  }
})();

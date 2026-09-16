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
let templateActiveJobId = "";
let pollTimer = null;
let pollGeneration = 0;
let viewGeneration = 0;
let sessionLive = false;
let logCursor = 0;
let submitting = false;
let environments = [];
let servicePermissions = [];
let envDraftOpen = false;
let envEditingId = "";
let runPreviewActive = false;
let runPreviewSelection = { gammaDeploy: false, deploy: false, test: false, environmentId: "", gammaEnvironmentId: "", deploymentMode: "" };
let pipelineTemplates = [];
let selectedTemplateId = "";
let runLayerMode = "run";
let runLayerTemplateId = "";
let cloneSourceId = "";
let previewBranchPicker = null;
let previewBranchLoadGeneration = 0;
let previewBranchServiceId = "";
let svcMenuPage = 1;
const SVC_PAGE_SIZE = 8;
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
function pipelineUrl(value) {
  return String(value || '').replace(/^http:\/\/119\.8\.233\.58:8080\/(?=(?:runs|batches|artifacts)\/)/, 'http://119.8.233.58/pipeline/');
}
function renderLogText(el, text) {
  const value = String(text == null ? "" : text);
  if (el.textContent === value) return;
  const fragment = document.createDocumentFragment();
  const urls = /https?:\/\/[^\s<>"'`\u3000\u201c\u201d\u2018\u2019]+/gi;
  let offset = 0;
  for (const match of value.matchAll(urls)) {
    let href = match[0].replace(/[.,;:!?，。；：！？、）】》]+$/u, "");
    // Keep balanced parentheses in URLs, but leave prose delimiters outside.
    while (href.endsWith(")") && (href.match(/\)/g) || []).length > (href.match(/\(/g) || []).length) href = href.slice(0, -1);
    try {
      const parsed = new URL(href);
      if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) continue;
    } catch (_) { continue; }
    fragment.append(document.createTextNode(value.slice(offset, match.index)));
    const link = document.createElement("a");
    link.href = pipelineUrl(href);
    link.textContent = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.referrerPolicy = "no-referrer";
    fragment.append(link);
    offset = match.index + href.length;
  }
  fragment.append(document.createTextNode(value.slice(offset)));
  el.replaceChildren(fragment);
}

function paintLog(el, text, state) {
  if (!el) return;
  if (state && !state.follow) {
    state.pendingText = text;
    state.flush = () => {
      if (state.follow && state.pendingText != null) {
        renderLogText(el, state.pendingText);
        el.scrollTop = el.scrollHeight;
        state.pendingText = null;
      }
    };
    return;
  }
  renderLogText(el, text);
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
function jobTemplateId(job) {
  return String((job && job.template_id) || "").trim();
}
function jobBelongsToSelectedTemplate(job) {
  const tid = selectedTemplateId || "";
  if (!tid || !job) return true;
  const jobTid = jobTemplateId(job);
  // Legacy jobs without template_id stay visible only on the personal builtin pipeline.
  if (!jobTid) {
    const tpl = selectedPipelineTemplate();
    return !!(tpl && tpl.kind === "personal" && tpl.builtin);
  }
  return jobTid === tid;
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
  return name === "history" || name === "artifacts" || name === "envs" || name === "permissions";
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
  if ($("viewPermissions")) $("viewPermissions").hidden = tab !== "permissions";
  if ($("pipelineActions")) $("pipelineActions").hidden = all || tab !== "pipeline";
  if ($("jobMetaBar")) $("jobMetaBar").hidden = all || tab !== "pipeline";
  const createBtn = $("btnEnvCreate");
  if (createBtn) {
    createBtn.hidden = all;
    createBtn.disabled = !all && (!servicePermissions.length || !canManageEnvironments());
  }
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
    if (tab === "permissions") loadPermissions().catch(() => {});
    if (tab === "pipeline" && !isAllServices()) {
      loadPipelineTemplates()
        .catch(() => {})
        .then(() => {
          if (tab !== "pipeline" || isAllServices()) return;
          if (pinnedJobId) {
            if (currentJobId !== pinnedJobId) openJob(pinnedJobId, { fromHistory: true }).catch(() => {});
          } else {
            loadServiceJob();
          }
        });
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
  if (["history", "artifacts", "envs", "permissions"].includes(parts[2])) nextTab = parts[2];
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
  // Only block Run when THIS pipeline template already has a live job.
  if ($("btnRun")) $("btnRun").disabled = !svc || !!templateActiveJobId || runPreviewActive || cloneLayerOpen();
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
  if (typed === "ut" || typed === "dt" || typed === "gamma") return typed;
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
  if (step.id === "rollback") {
    return [decoratePipelineTask({
      id: "rollback",
      label: "一键回滚",
      status: step.status || "pending",
    }, "rollback", "rollback")];
  }
  if (step.id === "gamma") {
    return tasks.filter((task) => task.status !== "skipped");
  }
  return tasks;
}
function buildPipelineStages(pipeline, jobStatus, stopping) {
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
  if (jobStatus === "stopped" || jobStatus === "failed" || stopping) {
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
  return (stageId === "gamma" && (taskId === "deploy" || taskId === "test")) || (stageId === "release" && taskId === "deploy");
}
function previewTaskChecked(stageId, taskId) {
  if (stageId === "release") return !!runPreviewSelection.deploy;
  return taskId === "deploy" ? !!runPreviewSelection.gammaDeploy : !!runPreviewSelection.test;
}
function createPreviewCheck(stageId, taskId, doc) {
  doc = doc || document;
  const box = doc.createElement("button");
  box.type = "button";
  box.className = "pl-check" + (previewTaskChecked(stageId, taskId) ? " is-on" : "");
  box.setAttribute("aria-pressed", previewTaskChecked(stageId, taskId) ? "true" : "false");
  box.setAttribute("aria-label", stageId === "release" ? "生产发布" : taskId === "deploy" ? "gamma部署" : "gamma测试");
  box.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5 10 17.5 19 7" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  box.addEventListener("click", (ev) => {
    ev.stopPropagation();
    togglePreviewTask(stageId, taskId);
  });
  return box;
}
function togglePreviewTask(stageId, taskId) {
  if (stageId === "release") runPreviewSelection.deploy = !runPreviewSelection.deploy;
  else if (taskId === "deploy") { runPreviewSelection.gammaDeploy=!runPreviewSelection.gammaDeploy; if(!runPreviewSelection.gammaDeploy) runPreviewSelection.test=false; }
  else if (taskId === "test") { runPreviewSelection.test=!runPreviewSelection.test; if(runPreviewSelection.test) runPreviewSelection.gammaDeploy=true; }
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
      { id: "archive", label: "本地归档", status: "pending", subtasks: [task("save", "归档镜像")] },
      { id: "gamma", label: "gamma集成测试", status: "pending", subtasks: [task("deploy", "gamma部署"), task("test", "gamma测试")] },
      { id: "release", label: "生产发布", status: "pending", subtasks: [task("deploy", "生产发布")] },
      { id: "rollback", label: "一键回滚", status: "skipped", subtasks: [task("rollback", "一键回滚")] },
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
  setLoading(true);
  try {
    const data = await fetchServiceBranches(svc.id, !!force, true);
    if (data.service_id && data.service_id !== svc.id) {
      throw new Error("分支缓存服务不匹配，请重新打开运行页");
    }
    const tpl = findPipelineTemplate(runLayerTemplateId);
    if (tpl && tpl.branch) data.selected_branch = tpl.branch;
    applyCached(data);
  } catch (e) {
    if (!isCurrentLoad()) return;
    picker.setOptions([fallbackBranch], fallbackBranch, fallbackBranch);
    setPreviewBranchStatus(e.message || "加载分支失败", "error");
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
function resetPreviewBranchMenuDom() {
  const root = $("previewBranchPicker");
  const menu = $("previewBranchMenu");
  if (!menu) return;
  menu.hidden = true;
  menu.classList.remove("branch-menu-portal");
  menu.style.left = "";
  menu.style.top = "";
  menu.style.width = "";
  menu.style.maxHeight = "";
  const trigger = $("previewBranch");
  if (trigger) trigger.setAttribute("aria-expanded", "false");
  if (root && menu.parentElement !== root) root.appendChild(menu);
}
function closeRunPopup() {
  // Keep the singleton picker; rebinding createBranchPicker on every enter
  // stacks click handlers and makes open/close alternate.
  if (previewBranchPicker && typeof previewBranchPicker.close === "function") {
    previewBranchPicker.close();
  } else {
    resetPreviewBranchMenuDom();
  }
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
    gammaDeploy: !!(tpl && tpl.gamma_deploy),
    deploy: !!(tpl && tpl.production_release),
    test: !!(tpl && tpl.gamma_test),
    environmentId: (tpl && tpl.environment_id) || "",
    gammaEnvironmentId: (tpl && tpl.environment_id) || "",
    deploymentMode: "",
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
  if (!svc || templateActiveJobId) return;
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
  resetPreviewBranchMenuDom();
  const svc = currentService();
  previewBranchServiceId = (svc && svc.id) || "";
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
  previewBranchServiceId = "";
  runLayerMode = "run";
  runLayerTemplateId = "";
  runPreviewSelection = { gammaDeploy: false, deploy: false, test: false, environmentId: "", gammaEnvironmentId: "", deploymentMode: "" };
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
  if (runPreviewSelection.test && (runPreviewSelection.gammaEnvironmentId === 'ci-e2e' || (runPreviewSelection.mode || 'browser-e2e') === 'browser-e2e') && !(runPreviewSelection.suites || ['E01','E02','E03']).length) {
    setPreviewHint('请至少选择一个 E2E 用例'); return;
  }
  if (runPreviewSelection.gammaDeploy && !runPreviewSelection.gammaEnvironmentId) {
    setPreviewHint("请为 gamma 集成测试选择环境");
    return;
  }
  if (runPreviewSelection.deploy && (!runPreviewSelection.environmentId || !runPreviewSelection.deploymentMode)) {
    setPreviewHint("请为生产发布选择环境和部署方式");
    return;
  }
  const branch = (previewBranchPicker && previewBranchPicker.value) || "";
  if (!branch || previewBranchServiceId !== currentServiceId) {
    setPreviewHint("微服务已切换，请重新打开运行页后选择分支");
    return;
  }
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
      gamma_deploy: !!runPreviewSelection.gammaDeploy,
      production_release: !!runPreviewSelection.deploy,
      gamma_test: !!runPreviewSelection.test,
      environment_id: runPreviewSelection.environmentId || "",
      release_environment_id: runPreviewSelection.environmentId || "",
      gamma_environment_id: runPreviewSelection.gammaEnvironmentId || "",
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
        if (id && id !== selectedTemplateId) {
          selectedTemplateId = id;
          templateActiveJobId = "";
          pinnedJobId = "";
          stopPolling();
          applyJob(null, { loading: true });
          hideTplMenu();
          renderPipelineTemplates();
          if (!isAllServices() && currentServiceId) loadServiceJob();
          return;
        }
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
function createStatusIcon(status, kind, doc, opts) {
  doc = doc || document;
  const icon = doc.createElement("span");
  const cont = !!(opts && opts.continueIcon) && (status === "queued" || status === "waiting");
  icon.className = (kind || "pl-icon") + " is-" + (status || "pending") + (cont ? " is-continue" : "");
  if (cont) {
    icon.setAttribute("aria-label", "等待继续");
    icon.textContent = "";
    return icon;
  }
  icon.textContent = status === "done" ? "✓" : (status === "failed" || status === "warn") ? "✕" : status === "skipped" ? "–" : (status === "running" || status === "queued") ? "●" : "";
  return icon;
}
function createEllipsisColumn(doc) {
  doc = doc || document;
  const col = doc.createElement("div");
  col.className = "pl-col pl-ellipsis";
  const cap = doc.createElement("div");
  cap.className = "pl-caption";
  const title = doc.createElement("div");
  title.className = "pl-title";
  title.textContent = "···";
  title.title = "执行测试 / 构建镜像 / 推送 SWR";
  const dur = doc.createElement("div");
  dur.className = "pl-dur";
  dur.innerHTML = "&nbsp;";
  cap.append(title, dur);
  const spine = doc.createElement("div");
  spine.className = "pl-spine";
  const left = doc.createElement("div");
  left.className = "pl-rail left is-wait";
  const right = doc.createElement("div");
  right.className = "pl-rail right is-wait";
  const icon = doc.createElement("span");
  icon.className = "pl-ellipsis-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.append(doc.createElement("span"), doc.createElement("span"), doc.createElement("span"));
  spine.append(left, icon, right);
  col.append(cap, spine);
  return col;
}
function createStageColumn(stage, incomingComplete, outgoingComplete, onStep, preview, doc) {
  doc = doc || document;
  const status = stage.status || "pending";
  const col = doc.createElement("div");
  col.className = "pl-col is-" + status;
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
  spine.append(left, createStatusIcon(status, "pl-stage-icon", doc, { continueIcon: status === "queued" }), right);
  col.appendChild(spine);
  const drop = doc.createElement("div");
  drop.className = "pl-drop";
  const diamond = doc.createElement("div");
  diamond.className = "pl-diamond";
  diamond.append(doc.createElement("span"), doc.createElement("span"));
  drop.appendChild(diamond);
  const tree = doc.createElement("div");
  tree.className = "pl-tree";
  if (preview && stage.id === "release" && runPreviewSelection.deploy) {
    [createGammaEnvPicker(doc, stage.id), createReleaseModePicker(doc)].forEach((picker) => {
      const optionsRow = doc.createElement("div");
      optionsRow.className = "pl-task pl-env-row pl-release-options";
      optionsRow.appendChild(doc.createElement("span"));
      optionsRow.appendChild(createStatusIcon("pending", "pl-task-icon", doc));
      optionsRow.appendChild(picker);
      tree.appendChild(optionsRow);
    });
  } else if (preview && stage.id === "gamma" && runPreviewSelection.gammaDeploy) {
    const envRow = doc.createElement("div");
    envRow.className = "pl-task pl-env-row";
    envRow.appendChild(doc.createElement("span"));
    envRow.appendChild(createStatusIcon("pending", "pl-task-icon", doc));
    envRow.appendChild(createGammaEnvPicker(doc, stage.id));
    tree.appendChild(envRow);
  }
  (stage.tasks || []).forEach((task) => {
    const row = doc.createElement("div");
    row.className = "pl-task is-" + (task.status || "pending");
    row.appendChild(doc.createElement("span"));
    if (preview && isSelectablePreviewTask(stage.id, task.id)) {
      row.classList.add("is-selectable");
      row.appendChild(createPreviewCheck(stage.id, task.id, doc));
    } else {
      row.appendChild(createStatusIcon(task.status, "pl-task-icon", doc, {
        continueIcon: task.status === "queued",
      }));
    }
    const name = doc.createElement("span");
    name.className = "pl-task-name";
    name.textContent = task.label || task.id || "";
    row.appendChild(name);
    row.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (preview && isSelectablePreviewTask(stage.id, task.id)) {
        togglePreviewTask(stage.id, task.id);
        return;
      }
      onStep && onStep(stage.id, task.id);
    });
    tree.appendChild(row);
  });
  if (preview && stage.id === 'gamma' && runPreviewSelection.test) {
    // Repository CID gamma is temporarily disabled; keep production :80 browser E2E only.
    runPreviewSelection.mode = 'browser-e2e';
  }
  if (preview && stage.id === 'gamma' && runPreviewSelection.test && runPreviewSelection.mode === 'browser-e2e') {
    const group = doc.createElement('div'); group.className = 'gamma-suite-options';
    for (const [id, title] of [['E01','创建机器人'],['E02','私聊与追问'],['E03','群聊 @'],['E04','停止与取消'],['E05','失败与恢复'],['E06','重复事件与幂等']]) {
      const label = doc.createElement('label'), box = doc.createElement('input');
      box.type='checkbox'; box.value=id; box.checked=(runPreviewSelection.suites || ['E01','E02','E03']).includes(id);
      box.addEventListener('change', () => {
        const values=new Set(runPreviewSelection.suites || ['E01','E02','E03']);
        box.checked ? values.add(id) : values.delete(id); runPreviewSelection.suites=[...values];
      });
      label.append(box, doc.createTextNode(id+' '+title)); group.append(label);
    }
    const label=doc.createElement('label'), box=doc.createElement('input');
    box.type='checkbox'; box.checked=runPreviewSelection.baseline !== false;
    box.addEventListener('change', () => {runPreviewSelection.baseline=box.checked;});
    label.append(box,doc.createTextNode('基线对照'));
    group.append(label);
    tree.append(group);
  }
  drop.appendChild(tree);
  col.appendChild(drop);
  return col;
}
function selectedEnvName(kind) {
  const id = kind === "gamma" ? runPreviewSelection.gammaEnvironmentId : runPreviewSelection.environmentId;
  const env = environments.find((item) => item.id === id);
  return env ? (env.name || "未命名环境") : "";
}
function createGammaEnvPicker(doc, kind) {
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
  const choices = environments;
  const empty = !choices.length;
  label.textContent = selectedEnvName(kind) || (empty ? "还没有环境" : "选择环境");
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
  choices.forEach((env) => {
    const option = doc.createElement("button");
    option.type = "button";
    const selectedId = kind === "gamma" ? runPreviewSelection.gammaEnvironmentId : runPreviewSelection.environmentId;
    option.className = "branch-option" + (selectedId === env.id ? " active" : "");
    option.setAttribute("role", "option");
    option.textContent = env.name || "未命名环境";
    option.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (kind === "gamma") runPreviewSelection.gammaEnvironmentId = env.id;
      else runPreviewSelection.environmentId = env.id;
      label.textContent = env.name || "未命名环境";
      menu.querySelectorAll(".branch-option").forEach((item) => item.classList.toggle("active", item === option));
      setPreviewHint("");
      close();
      trigger.focus();
      renderRunPage();
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
function createReleaseModePicker(doc) {
  doc = doc || document;
  const win = doc.defaultView || window;
  const choices = [{ id: 'parallel', name: '平滑升级' }, { id: 'inplace', name: '替换升级', disabled: true }];
  const wrap = doc.createElement('div'); wrap.className = 'pl-env-pick release-mode-pick';
  const picker = doc.createElement('div'); picker.className = 'pl-env-picker';
  const trigger = doc.createElement('button'); trigger.type = 'button';
  trigger.className = 'input branch-trigger pl-env-trigger';
  trigger.setAttribute('aria-haspopup', 'listbox'); trigger.setAttribute('aria-expanded', 'false');
  const label = doc.createElement('span'); label.className = 'pl-env-trigger-label';
  label.textContent = (choices.find((item) => item.id === runPreviewSelection.deploymentMode) || {}).name || '选择部署方式';
  trigger.appendChild(label);
  const chevron = doc.createElementNS('http://www.w3.org/2000/svg', 'svg');
  chevron.setAttribute('viewBox', '0 0 24 24'); chevron.setAttribute('aria-hidden', 'true');
  chevron.innerHTML = '<path d="M7 10l5 5 5-5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>';
  trigger.appendChild(chevron);
  const menu = doc.createElement('div'); menu.className = 'branch-menu'; menu.hidden = true; menu.setAttribute('role', 'listbox');
  const close = () => { menu.hidden = true; trigger.setAttribute('aria-expanded', 'false'); if (menu.parentNode) menu.remove(); };
  const positionMenu = () => {
    const rect = trigger.getBoundingClientRect();
    menu.classList.add('branch-menu-portal'); menu.style.left = rect.left + 'px'; menu.style.top = (rect.bottom + 6) + 'px';
    menu.style.width = rect.width + 'px'; menu.style.maxHeight = Math.max(120, Math.min(280, win.innerHeight - rect.bottom - 12)) + 'px';
    doc.body.appendChild(menu);
  };
  choices.forEach((choice) => {
    const option = doc.createElement('button'); option.type = 'button';
    option.className = 'branch-option' + ((runPreviewSelection.deploymentMode || 'parallel') === choice.id ? ' active' : '');
    option.setAttribute('role', 'option'); option.textContent = choice.name;
    option.disabled = !!choice.disabled;
    option.addEventListener('click', (ev) => {
      ev.stopPropagation(); runPreviewSelection.deploymentMode = choice.id; label.textContent = choice.name;
      close(); trigger.focus(); renderRunPage();
    });
    menu.appendChild(option);
  });
  trigger.addEventListener('click', (ev) => {
    ev.stopPropagation(); const opening = menu.hidden; if (opening) positionMenu(); else close();
    menu.hidden = !opening; trigger.setAttribute('aria-expanded', String(opening));
  });
  picker.addEventListener('focusout', () => setTimeout(() => {
    if (!picker.contains(doc.activeElement) && !menu.contains(doc.activeElement)) close();
  }, 0));
  picker.append(trigger, menu); wrap.appendChild(picker); return wrap;
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
function createApprovalGate(gate, incomingComplete, outgoingComplete, jobId, doc) {
  doc = doc || document;
  const status = gate.status || "pending";
  const col = doc.createElement("div");
  col.className = "pl-gate is-" + status + (status === "queued" ? " is-actionable" : "");
  const cap = doc.createElement("div");
  cap.className = "pl-caption";
  const title = doc.createElement("div");
  title.className = "pl-title";
  title.textContent = "人工卡点";
  const dur = doc.createElement("div");
  dur.className = "pl-dur is-" + status;
  dur.textContent = status === "queued" ? "等待中" : status === "done" ? "已通过" : "";
  cap.append(title, dur);
  const spine = doc.createElement("div");
  spine.className = "pl-spine";
  const left = doc.createElement("div");
  left.className = "pl-rail left " + (incomingComplete ? "is-ok" : "is-wait");
  const right = doc.createElement("div");
  right.className = "pl-rail right " + (outgoingComplete ? "is-ok" : "is-wait");
  spine.append(left, createStatusIcon(status, "pl-stage-icon", doc, { continueIcon: true }), right);
  col.append(cap, spine);
  col.title = status === "queued"
    ? "等待生产发布权限责任人确认，点击继续"
    : (gate.operator ? "审批人：" + gate.operator + (gate.approved_at ? " · " + gate.approved_at : "") : "");
  if (status === "queued") {
    col.tabIndex = 0;
    col.setAttribute("role", "button");
    const approve = async () => {
      if (col.classList.contains("is-busy")) return;
      col.classList.add("is-busy");
      try {
        await api("/api/jobs/" + encodeURIComponent(jobId) + "/approval", { method: "POST", body: JSON.stringify({ action: "continue" }) });
        await openJob(jobId);
      } catch (e) { alert(e.message || "无权继续该人工卡点"); }
      finally { col.classList.remove("is-busy"); }
    };
    col.addEventListener("click", approve);
    col.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); approve(); } });
  }
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
  const stages = buildPipelineStages(pipeline, status, !!(opts && opts.stopping));
  const doc = root.ownerDocument || document;
  const scroll = doc.createElement("div");
  scroll.className = "pl-scroll";
  const graph = doc.createElement("div");
  graph.className = "pl-graph";
  const isRailDone = (node) => Boolean(node && node.status === "done");
  const gates = preview || !Array.isArray(pipeline.gates) ? [] : pipeline.gates;
  const nodes = [];
  const previewCollapse = new Set(["test", "build", "push"]);
  let collapsedPreview = false;
  stages.forEach((stage) => {
    if (preview && previewCollapse.has(stage.id)) {
      if (!collapsedPreview) {
        nodes.push({ kind: "ellipsis", data: { id: "ellipsis", status: "pending" } });
        collapsedPreview = true;
      }
      return;
    }
    gates.filter((gate) => gate.before === stage.id).forEach((gate) => nodes.push({ kind: "gate", data: gate }));
    nodes.push({ kind: "stage", data: stage });
  });
  const progressed = !preview && stages.some((stage) => {
    const st = stage && stage.status;
    return st && st !== "pending" && st !== "skipped";
  });
  graph.appendChild(createEndpoint("start", progressed, doc));
  nodes.forEach((node, index) => {
    const stage = node.data;
    const previous = nodes[index - 1] && nodes[index - 1].data;
    const incomingComplete = !preview && (index === 0 ? progressed : isRailDone(previous));
    const outgoingComplete = !preview && isRailDone(stage);
    if (node.kind === "ellipsis") {
      graph.appendChild(createEllipsisColumn(doc));
      return;
    }
    graph.appendChild(node.kind === "gate"
      ? createApprovalGate(stage, incomingComplete, outgoingComplete, meta.job_id || currentJobId, doc)
      : createStageColumn(stage, incomingComplete, outgoingComplete, onStep, preview, doc));
  });
  graph.appendChild(createEndpoint("end", !preview && status === "ok" && stages.some((stage) => isRailDone(stage)), doc));
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
  const cols = isAllServices() ? 9 : 8;
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="' + cols + '" class="hint">暂无构建记录。</td></tr>';
  } else {
    body.innerHTML = items.map((item) => (
      "<tr>" +
      '<td class="hist-time">' + esc(item.created_at || "—") + "</td>" +
      '<td class="hist-service-cell">' + esc(item.service_id || "—") + "</td>" +
      clipTd(item.template_name || "—", "hist-pipeline") +
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
      if ($("historyBody")) $("historyBody").innerHTML = '<tr><td colspan="' + (isAllServices() ? 9 : 8) + '" class="hint">加载失败，请刷新。</td></tr>';
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
    // Prefer pinned history jobs; otherwise reload the selected pipeline's latest.
    if (pinnedJobId) {
      await openJob(pinnedJobId, { fromHistory: true });
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
  const historyColumns = isAllServices() ? 9 : 8;
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
  servicePermissions = [];
  pinnedJobId = "";
  serviceActiveJobId = "";
  templateActiveJobId = "";
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
    loadPipelineTemplates()
      .catch(() => {})
      .then(() => {
        if (currentServiceId !== id || tab !== "pipeline") return;
        loadServiceJob();
      });
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

function applyJob(job, { loading, force } = {}) {
  if (job && !sessionLive) return;
  if (job && !isAllServices() && currentServiceId && !jobBelongsToService(job, currentServiceId)) return;
  if (job && !force && !isAllServices() && !jobBelongsToSelectedTemplate(job)) return;
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
      empty.textContent = loading ? "正在加载流水线…" : "当前分支无构建历史记录";
    }
    return;
  }
  if (empty) empty.hidden = true;
  renderJobPipeline("liveJobPipeline", job.pipeline, job.status, (stageId, taskId) => {
    openStepModal(stageId, taskId);
  }, { stopping: !!(job.cancel_requested || job.stage === "stopping") });
  if (job.gamma_e2e) {
    const row=document.createElement('div'); row.className='gamma-result-link';
    const state=document.createElement('span');
    const gammaStatus=job.gamma_e2e.conclusion === 'failure' ? '未通过' : job.gamma_e2e.conclusion === 'success' ? '通过' : ({queued:'排队中',running:'执行中',interrupted:'已中断',blocked:'环境阻塞',detached:'已脱离构建等待'}[job.gamma_e2e.state] || job.gamma_e2e.state || '等待');
    state.textContent = job.cancel_requested
      ? (jobIsLive(job) ? '已请求停止，正在终止 Gamma 任务' : '构建已停止 · 已退出 Gamma 等待')
      : '构建产物已生成 · Gamma ' + gammaStatus + (job.gamma_e2e.stage ? ' · ' + job.gamma_e2e.stage : '');
    row.append(state);
    const url=pipelineUrl(job.gamma_e2e.url);
    if (url.startsWith('http://119.8.233.58/pipeline/batches/gamma-')) {
      const link=document.createElement('a');link.href=url;link.target='_blank';link.rel='noreferrer';
      link.textContent='查看 Gamma E2E 流程与报告';row.append(link);
    }
    $('liveJobPipeline').append(row);
  }
}

async function openJob(jobId, { fromHistory } = {}) {
  if (!jobId || !sessionLive) return false;
  const gen = viewGeneration;
  const sidBefore = currentServiceId;
  const tidBefore = selectedTemplateId || "";
  stopPolling();
  const job = await api("/api/jobs/" + jobId + "?compact=1");
  if (!sessionLive) return false;
  const sid = jobServiceIds(job)[0] || "";
  if (fromHistory) {
    if (sid && sid !== currentServiceId && services.some((item) => item.id === sid)) {
      activateService(sid);
    }
    if (!isAllServices() && currentServiceId) {
      await loadPipelineTemplates().catch(() => {});
    }
    const jobTid = jobTemplateId(job);
    if (jobTid) {
      selectedTemplateId = jobTid;
      renderPipelineTemplates();
    } else {
      const personal = defaultPersonalTemplate();
      if (personal) {
        selectedTemplateId = personal.id;
        renderPipelineTemplates();
      }
    }
    pinnedJobId = jobId;
    templateActiveJobId = jobIsLive(job) ? jobId : "";
    applyJob(job, { force: true });
    setTab("pipeline");
    if (jobIsLive(job)) startPolling(jobId);
    refreshServiceOccupancy().catch(() => {});
    return true;
  }
  if (!viewIsCurrent(gen, sidBefore)) return false;
  if ((selectedTemplateId || "") !== tidBefore) return false;
  if (!isAllServices() && sid && sid !== currentServiceId) return false;
  if (!isAllServices() && !jobBelongsToSelectedTemplate(job)) return false;
  applyJob(job);
  if (jobIsLive(job)) startPolling(jobId);
  return true;
}

async function refreshServiceOccupancy() {
  if (!sessionLive || isAllServices() || !currentServiceId) {
    serviceActiveJobId = "";
    templateActiveJobId = "";
    updateActionButtons();
    return;
  }
  const tid = selectedTemplateId || "";
  try {
    const running = await api(
      "/api/running-job?service_id=" + encodeURIComponent(currentServiceId) +
      (tid ? "&template_id=" + encodeURIComponent(tid) : "")
    );
    templateActiveJobId = (running && running.id) || "";
    // Keep a service-level occupancy hint for UI chrome that still needs it.
    if (tid) {
      const any = await api("/api/running-job?service_id=" + encodeURIComponent(currentServiceId));
      serviceActiveJobId = (any && any.id) || "";
    } else {
      serviceActiveJobId = templateActiveJobId;
    }
  } catch (_) {
    serviceActiveJobId = "";
    templateActiveJobId = "";
  }
  updateActionButtons();
}

async function loadServiceJob() {
  const gen = viewGeneration;
  const sid = currentServiceId;
  // Wait for templates so personal pipeline id is known before filtering.
  if (!pipelineTemplates.length && sid && sid !== ALL_SERVICES_ID) {
    await loadPipelineTemplates().catch(() => {});
  }
  const tid = selectedTemplateId || "";
  stopPolling();
  pinnedJobId = "";
  if (!sessionLive || !sid || sid === ALL_SERVICES_ID) {
    applyJob(null);
    return;
  }
  const btn = $("btnRefreshPipeline");
  beginRefresh(btn);
  const started = Date.now();
  applyJob(null, { loading: true });
  const stillHere = () => viewIsCurrent(gen, sid) && (selectedTemplateId || "") === tid;
  try {
    const qs = "service_id=" + encodeURIComponent(sid) +
      (tid ? "&template_id=" + encodeURIComponent(tid) : "");
    const running = await api("/api/running-job?" + qs);
    if (!stillHere()) return;
    templateActiveJobId = (running && running.id) || "";
    updateActionButtons();
    if (running && running.id && await openJob(running.id)) return;
    if (!stillHere()) return;
    const hist = await api("/api/jobs?" + qs + "&page=1&page_size=1");
    if (!stillHere()) return;
    const latest = hist.jobs && hist.jobs[0];
    if (latest && latest.id && await openJob(latest.id)) return;
    if (!stillHere()) return;
    applyJob(null);
  } catch (e) {
    if (!stillHere()) return;
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
  const expectedService = currentServiceId;
  const expectedTemplate = selectedTemplateId || "";
  const tick = async () => {
    if (!sessionLive || generation !== pollGeneration) return;
    if (currentServiceId !== expectedService) return;
    if ((selectedTemplateId || "") !== expectedTemplate) return;
    try {
      const job = await api("/api/jobs/" + jobId + "?compact=1&view=ui&log_after=0");
      if (!sessionLive || generation !== pollGeneration) return;
      if (currentServiceId !== expectedService) return;
      if ((selectedTemplateId || "") !== expectedTemplate) return;
      if (!isAllServices() && !jobBelongsToService(job, currentServiceId)) return;
      if (!isAllServices() && !jobBelongsToSelectedTemplate(job)) return;
      // Ignore stale polls after the UI already moved to another job.
      if (currentJobId && currentJobId !== jobId && pinnedJobId !== jobId) return;
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
  if (!force && branchLoads[serviceId]) return branchLoads[serviceId];
  const request = queueBranchLoad(() => {
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
  }, !!priority).then((data) => ({
    service_id: data.service_id || serviceId,
    branches: data.branches || [],
    selected_branch: data.selected_branch || "",
    default_branch: data.default_branch || "",
    cached: data.cached === true,
    refresh_error: data.refresh_error || "",
  }));
  if (force) return request;
  const shared = request.finally(() => {
    if (branchLoads[serviceId] === shared) delete branchLoads[serviceId];
  });
  branchLoads[serviceId] = shared;
  return shared;
}
function createBranchPicker(root, selectors) {
  const doc = root.ownerDocument || document;
  const win = doc.defaultView || window;
  const trigger = root.querySelector((selectors && selectors.trigger) || "#runBranch");
  const label = root.querySelector((selectors && selectors.label) || "#runBranchValue");
  const menu = root.querySelector((selectors && selectors.menu) || "#runBranchMenu");
  if (!trigger || !label || !menu) return null;
  let value = "";
  const close = () => {
    menu.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    // The list is temporarily portalled to <body> when opened so it is not
    // clipped by a dialog. Put the same node back after closing; removing it
    // breaks the next service's picker and leaves stale branch text visible.
    if (menu.parentElement !== root) root.appendChild(menu);
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
    close,
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
  branchPicker.setOptions([fallbackBranch], fallbackBranch, fallbackBranch);
  openModal("运行流水线", wrap, { narrow: true });
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
    setLoading(true);
    try {
      const data = await fetchServiceBranches(svc.id, force, true);
      applyCached(data, force);
    } catch (e) {
      branchPicker.setOptions([fallbackBranch], fallbackBranch, fallbackBranch);
      statusEl.textContent = e.message || "加载分支失败";
      statusEl.className = "branch-status error";
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
  const selectedBranch = String(branch || "").trim();
  if (!selectedBranch) {
    alert("请选择分支后再运行");
    return;
  }
  if (svc.requires_version && !DAEMON_VERSION_PATTERN.test(rememberedDaemonVersion() || svc.last_version || "")) {
    closeModal();
    openParamsDialog("运行前请先配置 Daemon version");
    return;
  }
    submitting = true;
  try {
    const tpl = findPipelineTemplate(runLayerTemplateId) || selectedPipelineTemplate();
    const resp = await api("/api/push", {
      method: "POST",
      body: JSON.stringify({
        service_id: svc.id,
        branch: selectedBranch,
        version: rememberedDaemonVersion() || svc.last_version || "",
        login_command: ($("loginCmd") && $("loginCmd").value || "").trim(),
        template_id: (tpl && tpl.id) || selectedTemplateId || "",
        template_name: (tpl && tpl.name) || "",
        optional_steps: {
          gamma_deploy: !!runPreviewSelection.gammaDeploy,
          production_release: !!runPreviewSelection.deploy,
          gamma_test: !!runPreviewSelection.test,
          environment_id: runPreviewSelection.environmentId || "",
          release_environment_id: runPreviewSelection.environmentId || "",
          gamma_environment_id: runPreviewSelection.gammaEnvironmentId || "",
          deployment_mode: runPreviewSelection.deploymentMode || "parallel",
          gamma_suites: runPreviewSelection.suites || ['E01','E02','E03'],
          gamma_mode: runPreviewSelection.mode || 'browser-e2e',
          gamma_baseline: runPreviewSelection.baseline !== false,
        },
      }),
    });
    closeModal();
    closeRunPopup();
    runPreviewActive = false;
    runLayerMode = "run";
    runLayerTemplateId = "";
    if (tpl && tpl.id) selectedTemplateId = tpl.id;
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
function testCasePriority(status) {
  if (status === "passed" || status === "skipped") return 1;
  return 0;
}
function orderedTestCases(cases) {
  return (cases || []).map((item, index) => ({ item, index }))
    .sort((left, right) => testCasePriority(left.item.status) - testCasePriority(right.item.status) || left.index - right.index)
    .map((entry) => entry.item);
}
function openTestFailureDetail(item, heading, onBack) {
  const wrap = document.createElement("div");
  wrap.className = "test-failure-detail";
  const name = document.createElement("strong");
  name.textContent = shortCaseName(item.name);
  const meta = document.createElement("p");
  meta.className = "hint";
  const location = item.file ? " · " + item.file : "";
  const command = item.command ? " · " + item.command : "";
  meta.textContent = (item.status === "error" ? "错误" : "失败") + location + command;
  const detail = document.createElement("pre");
  detail.className = "log test-failure-log";
  detail.textContent = item.detail || "该用例未提供独立失败摘要。";
  wrap.append(name, meta, detail);
  if (typeof onBack === "function") {
    const actions = document.createElement("div");
    actions.className = "row";
    const back = document.createElement("button");
    back.type = "button";
    back.className = "btn ghost";
    back.textContent = "返回测试结果";
    back.addEventListener("click", onBack);
    actions.appendChild(back);
    wrap.appendChild(actions);
  }
  openModal((heading || "测试") + "失败详情", wrap);
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
function renderTestRun(root, run, heading, onViewLog) {
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
  const cases = orderedTestCases(Array.isArray(run.test_cases) ? run.test_cases : []);
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
    const skipped = item.status === "skipped";
    const abnormal = !passed && !skipped;
    const label = passed ? "通过" : skipped ? "跳过" : item.status === "error" ? "错误" : "失败";
    if (abnormal) {
      const detailButton = document.createElement("button");
      detailButton.type = "button";
      detailButton.className = "test-status bad test-failure-button";
      detailButton.textContent = label;
      detailButton.title = "查看失败摘要";
      detailButton.addEventListener("click", () => openTestFailureDetail(item, heading, onViewLog));
      status.appendChild(detailButton);
    } else {
      status.textContent = label;
      status.className = "test-status " + (passed ? "ok" : "");
    }
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
    renderTestRun(root, run, heading, onViewLog);
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
    renderTestRun(root, run, heading, onViewLog);
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
    renderTestRun(root, run, heading, onViewLog);
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
function isCaseTaskId(id, stageId) {
  return id === "ut-cases" || id === "dt-cases" || (stageId === "gamma" && id === "test");
}
function parseCaseSelection(selected) {
  const match = String(selected || "").match(/^(ut-cases|dt-cases|gamma-test):(results|log)$/);
  if (!match) return null;
  return { taskId: match[1], view: match[2] };
}
function normalizeStepSelection(taskId, stage) {
  if (isCaseTaskId(taskId, stage && stage.id)) {
    return (stage && stage.id === "gamma" ? "gamma-test" : taskId) + ":results";
  }
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
  logEl.textContent = "加载中…";
  pane.append(tableWrap, logEl);
  wrap.append(list, pane);
  openModal("步骤详情", wrap, { wide: true });

  // Use job-stored rollout status only — never block the left step list on CCE.
  const releaseState = String(((((currentJob || {}).gamma_rollouts || [])[0] || {}).status) || "");
  if (stage.id === "release" && taskId === "offline" && !["old_deleted", "rolled_back", "superseded"].includes(releaseState)) taskId = "release-compare:results";
  if (stage.id === "release" && taskId === "compare") taskId = "release-compare:results";
  if (stage.id === "rollback") taskId = "rollback:results";
  let selectedTask = normalizeStepSelection(taskId, stage);
  if (parseCaseSelection(selectedTask)) logEl.hidden = true;
  const follow = newLogFollowState();
  bindLogFollow(logEl, follow);

  const showReleaseComparison = async (mode) => {
    tableWrap.hidden = true;
    logEl.hidden = true;
    pane.querySelectorAll(".step-rollouts").forEach((item) => item.remove());
    const envId = ((((currentJob || {}).gamma_rollouts || [])[0] || {}).environment_id) ||
      (((currentJob || {}).optional_steps || {}).release_environment_id || "");
    const rolloutPanel = document.createElement("div");
    rolloutPanel.className = "env-rollouts step-rollouts";
    rolloutPanel.dataset.envRolloutList = envId || "";
    rolloutPanel.dataset.jobId = currentJobId;
    rolloutPanel.dataset.compareMode = mode;
    rolloutPanel.innerHTML = '<p class="env-secret-hint">加载中…</p>';
    pane.appendChild(rolloutPanel);
    if (!envId) {
      rolloutPanel.innerHTML = '<p class="hint error">未找到发布环境，无法加载版本对比。</p>';
      return;
    }
    await loadEnvRollouts(envId, rolloutPanel, mode);
  };

  const paintList = () => {
    list.replaceChildren();
    appendStepButton(list, stage.label || stage.id, stage.status, "", selectedTask === stage.id, () => {
      selectedTask = stage.id === "rollback" ? "rollback:results" : stage.id;
      paintList();
      loadStep();
    });
    if (stage.id === "rollback") {
      appendStepButton(list, "版本对比", "", "sub", selectedTask === "rollback:results", () => {
        selectedTask = "rollback:results"; paintList(); loadStep();
      });
      appendStepButton(list, "日志详情", "", "sub", selectedTask === "rollback:log", () => {
        selectedTask = "rollback:log"; paintList(); loadStep();
      });
      return;
    }
    (stage.tasks || []).forEach((task) => {
      const caseTaskId = stage.id === "gamma" && task.id === "test" ? "gamma-test" : task.id;
      if (isCaseTaskId(task.id, stage.id)) {
        appendStepHeading(list, task.label || task.id, task.status, "sub");
        appendStepButton(list, "测试结果", "", "sub nested", selectedTask === caseTaskId + ":results", () => {
          selectedTask = caseTaskId + ":results";
          paintList();
          loadStep();
        });
        appendStepButton(list, "日志详情", "", "sub nested", selectedTask === caseTaskId + ":log", () => {
          selectedTask = caseTaskId + ":log";
          paintList();
          loadStep();
        });
        return;
      }
      if (stage.id === "release" && task.id === "compare") {
        appendStepHeading(list, "版本对比", task.status, "sub");
        appendStepButton(list, "版本对比", "", "sub nested", selectedTask === "release-compare:results", () => {
          selectedTask = "release-compare:results"; paintList(); loadStep();
        });
        appendStepButton(list, "日志详情", "", "sub nested", selectedTask === "release-compare:log", () => {
          selectedTask = "release-compare:log"; paintList(); loadStep();
        });
        return;
      }
      appendStepButton(list, task.label || task.id, task.status, "sub", selectedTask === task.id, () => {
        selectedTask = stage.id === "release" && task.id === "offline" && !["old_deleted", "rolled_back"].includes(releaseState)
          ? "release-compare:results" : task.id;
        paintList();
        loadStep();
      });
    });
  };

  const loadStep = async () => {
    pane.querySelectorAll(".step-rollouts").forEach((item) => item.remove());
    if (selectedTask === "release-compare:results" || selectedTask === "rollback:results") {
      await showReleaseComparison(selectedTask.startsWith("rollback") ? "rollback" : "release");
      return;
    }
    logEl.hidden = false;
    logEl.textContent = "加载中…";
    tableWrap.hidden = true;
    const caseSel = parseCaseSelection(selectedTask);
    const selected = caseSel
      ? (stage.tasks || []).find((task) => task.id === (caseSel.taskId === "gamma-test" ? "test" : caseSel.taskId))
      : (stage.tasks || []).find((task) => task.id === selectedTask);
    const specialLog = selectedTask === "release-compare:log" || selectedTask === "rollback:log";
    const logStep = specialLog ? (selectedTask.startsWith("rollback") ? "rollback" : "release") : ((selected && selected.logStep) || stage.logStep || stage.id);
    const sub = specialLog ? (selectedTask.startsWith("rollback") ? "log" : "compare") : caseSel ? (caseSel.taskId === "gamma-test" ? "test" : caseSel.taskId) : (selectedTask && selectedTask !== stage.id ? selectedTask : "");
    try {
      const job = await api("/api/jobs/" + currentJobId + "?step=" + encodeURIComponent(logStep) + (sub ? "&sub=" + encodeURIComponent(sub) : "") + "&view=ui");
      const lines = Array.isArray(job.log) ? job.log : [];
      const run = caseSel && caseSel.taskId === "gamma-test"
        ? ((job.gamma_runs && job.gamma_runs[0]) || (currentJob.gamma_runs && currentJob.gamma_runs[0]))
        : ((job.test_runs && job.test_runs[0]) || currentTestRun(currentJob));
      tableWrap.replaceChildren();
      if (caseSel && caseSel.view === "results") {
        logEl.hidden = true;
        tableWrap.hidden = false;
        const showCaseResults = () => {
          selectedTask = caseSel.taskId + ":results";
          openModal("步骤详情", wrap, { wide: true });
          paintList();
          loadStep();
        };
        if (caseSel.taskId === "ut-cases") renderTestRun(tableWrap, filterRun(run, "ut"), "UT 测试结果", showCaseResults);
        else if (caseSel.taskId === "dt-cases") renderTestRun(tableWrap, filterRun(run, "dt"), "DT 测试结果", showCaseResults);
        else renderTestRun(tableWrap, filterRun(run, "gamma"), "Gamma 测试结果", showCaseResults);
        return;
      }
      tableWrap.hidden = true;
      logEl.hidden = false;
      const failedSelection = selected || stage;
      const emptyLog = failedSelection && failedSelection.status === "failed" && job.error
        ? "失败原因：\n" + job.error
        : (sub ? "该小步骤暂无独立日志。" : "该步骤暂无日志。");
      paintLog(logEl, lines.length ? lines.join("\n") : emptyLog, follow);
      pane.querySelectorAll(".step-rollouts").forEach((item) => item.remove());
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
    environment_type: "dev",
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
    environment_type: ($("envType") && $("envType").value) || "dev",
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
    '<label class="label" for="envType">环境标签</label><select id="envType" class="input"><option value="dev"' + (data.environment_type !== 'production' ? ' selected' : '') + '>dev</option><option value="production"' + (data.environment_type === 'production' ? ' selected' : '') + '>production</option></select>' +
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
function envReadonlyHtml(env) {
  const owned = serviceTitle(services.find((item) => item.id === env.service_id) || { id: env.service_id, title: env.service_id });
  const nodes = Array.isArray(env.nodes) && env.nodes.length ? env.nodes : [];
  const row = (label, value) => '<div class="env-view-row"><span>' + esc(label) + '</span><strong>' + esc(value || '—') + '</strong></div>';
  return '<div class="env-view-card">' +
    row('所属微服务', owned) + row('环境标签', env.environment_type || 'dev') +
    row('Region', env.region_label || env.region) + row('集群名称', env.cluster_name) +
    row('配置负载', env.workload_name) + row('当前活动负载', env.active_workload_name || env.workload_name) +
    row('跳板机', env.jump_host) + row('跳板机密码', env.has_jump_password ? '已配置（不显示明文）' : '未配置') +
    '<div class="env-view-row env-view-nodes"><span>节点列表</span><strong>' +
      (nodes.length ? nodes.map((node) => '<code>' + esc(node) + '</code>').join('') : '—') + '</strong></div>' +
    row('节点密码', env.has_node_password ? '已配置（不显示明文）' : '未配置') +
    row('创建人', env.created_by) + row('创建时间', env.created_at) + row('更新时间', env.updated_at) +
    '<p class="env-secret-hint">只读查看不包含跳板机密码和节点密码明文。</p>' +
    '<div class="row env-form-actions"><button type="button" class="btn primary" id="btnEnvViewClose">关闭</button></div></div>';
}
function envCardHtml(env) {
  const locked = !canManageEnvironments();
  const editTitle = locked ? "没有环境管理权限" : "编辑";
  const deleteTitle = locked ? "没有环境管理权限" : "删除";
  const lockAttrs = locked ? " disabled aria-disabled=\"true\"" : "";
  return (
    '<article class="env-card" data-env-id="' + esc(env.id) + '" role="button" tabindex="0" aria-label="查看环境 ' + esc(env.name || '未命名环境') + '">' +
    '<div class="env-card-head">' +
    '<div class="env-card-name">' + esc(env.name || "未命名环境") + ' <span class="env-tag">' + esc(env.environment_type || 'dev') + '</span></div>' +
    '<div class="env-card-actions">' +
    '<button type="button" class="env-icon-btn" data-env-edit="' + esc(env.id) + '" title="' + esc(editTitle) + '"' + lockAttrs + '>' + envIconPencil() + "</button>" +
    '<button type="button" class="env-icon-btn danger" data-env-delete="' + esc(env.id) + '" title="' + esc(deleteTitle) + '"' + lockAttrs + '>' + envIconTrash() + "</button>" +
    "</div></div>" +
    '<div class="env-card-meta">' +
    '<div class="env-meta-row"><span class="env-meta-label">微服务:</span><strong class="env-meta-value">' + esc(serviceTitle(services.find((item) => item.id === env.service_id) || { id: env.service_id, title: env.service_id }) || "—") + "</strong></div>" +
    '<div class="env-meta-row"><span class="env-meta-label">Region:</span><strong class="env-meta-value">' + esc(env.region_label || env.region || "—") + "</strong></div>" +
    '<div class="env-meta-row"><span class="env-meta-label">集群:</span><strong class="env-meta-value">' + esc(env.cluster_name || "—") + "</strong></div>" +
    '<div class="env-meta-row"><span class="env-meta-label">负载:</span><strong class="env-meta-value">' + esc(env.workload_name || "—") + "</strong></div>" +
    '</div></article>'
  );
}

function rolloutWorkloadHtml(rollout, side, item, allowScale) {
  const old = side === 'old';
  const exists = !!item.exists;
  const replicas = Number.isFinite(Number(item.desired_replicas)) ? Number(item.desired_replicas) : 0;
  let image = Array.isArray(item.images) && item.images.length ? item.images.join(', ') : '';
  if (!image) {
    if (!old && rollout && rollout.image) image = String(rollout.image);
    else if (Array.isArray(item.images)) image = '—';
    else image = '—';
  }
  return '<div class="env-rollout-workload ' + (old ? 'is-old' : 'is-new') + '">' +
    '<strong>' + (old ? '旧版本负载' : '新版本负载') + '</strong><span>' + esc(item.name || '—') + '</span>' +
    '<small>镜像：' + esc(image) + '</small><small>实例：' + (exists ? replicas + '（Ready ' + Number(item.ready_replicas || 0) + '）' : '已不存在') + '</small>' +
    (exists && allowScale ? '<div class="env-rollout-scale"><input class="input" type="number" min="0" max="1000" value="' + replicas + '" data-rollout-replicas="' + side + '" /><button type="button" class="btn ghost" data-rollout-scale="' + side + '">设置实例数</button></div>' : '') +
    '</div>';
}

function renderEnvRollouts(panel, rows, error, mode) {
  if (!panel) return;
  mode = mode || panel.dataset.compareMode || 'release';
  if (error) { panel.innerHTML = '<p class="hint error">' + esc(error) + '</p>'; return; }
  if (!rows || !rows.length) { panel.innerHTML = '<p class="env-secret-hint">暂无由 CI 创建的平滑发布负载。</p>'; return; }
  panel.innerHTML = rows.map((row) => {
    if (row.error) return '<p class="hint error">' + esc(row.candidate_workload || row.id) + '：' + esc(row.error) + '</p>';
    const rolledBack = row.status === 'rolled_back';
    const rollbackBusy = ['restoring', 'cleaning'].includes(row.recovery_phase);
    const rollbackButton = mode !== 'rollback' ? '' : rolledBack
      ? '<button type="button" class="btn ghost" disabled aria-disabled="true" title="本次发布已经回滚">一键回滚</button>'
      : rollbackBusy ? '<button type="button" class="btn ghost" disabled aria-disabled="true">回滚执行中…</button>'
      : row.can_rollback ? '<button type="button" class="btn ghost" data-rollout-rollback>一键回滚</button>' : '';
    const phase = mode === 'rollback' ? row.recovery_phase : row.offline_phase;
    const phaseLabels = {offlining:'正在删除旧版本', verifying:'正在确认旧版本已删除', restoring:'正在恢复目标版本', cleaning:'目标版本已就绪，正在清理其他版本', completed: mode === 'rollback' ? '回滚完成' : '下线完成', failed: mode === 'rollback' ? '回滚失败' : '下线失败'};
    const active = ['offlining','verifying','restoring','cleaning'].includes(phase);
    const operation = phase ? '<div class="rollout-operation ' + (phase === 'failed' ? 'is-failed' : active ? 'is-running' : 'is-done') + '" data-operation-active="' + (active ? '1' : '0') + '">' + (active ? '<span class="rollout-spinner" aria-hidden="true"></span>' : '') + '<strong>' + esc(phaseLabels[phase] || phase) + '</strong>' + (row.operation_updated_at ? '<small>最近更新：' + esc(row.operation_updated_at) + '</small>' : '') + (phase === 'failed' ? '<p>' + esc(mode === 'rollback' ? row.recovery_error : row.offline_error) + '</p>' : '') + '</div>' : '';
    return '<section class="env-rollout" data-rollout-id="' + esc(row.id) + '"><div class="env-rollout-head"><strong>生产发布 ' + esc(row.created_at || '') + '</strong><div class="env-rollout-head-actions"><span>' + esc(row.status === 'superseded' ? '已完成 · 已由历史回滚结束' : row.status === 'old_deleted' ? '旧版本已下线' : rolledBack ? '已回滚' : '等待老版本下线') + '</span>' + (mode === 'release' && row.old && row.old.exists && row.can_manage && !active ? '<button type="button" class="btn ghost danger" data-rollout-offline>下线</button>' : '') + rollbackButton + '</div></div>' + operation + rolloutWorkloadHtml(row, 'old', row.old || {}, mode === 'release' && row.can_manage && !active) + rolloutWorkloadHtml(row, 'new', row.new || {}, mode === 'release' && row.can_manage && !active) + '</section>';
  }).join('');
  panel.querySelectorAll('[data-rollout-id]').forEach((card) => {
    const row = rows.find((item) => item.id === card.dataset.rolloutId);
    if (row && ['restoring', 'cleaning', 'failed'].includes(row.recovery_phase)) {
      const message = row.recovery_phase === 'failed'
        ? '回滚未完成，可重新点击一键回滚重试：' + (row.recovery_error || '')
        : '回滚进行中：' + (row.recovery_phase === 'restoring' ? '等待恢复版本就绪' : '清理其他版本');
      card.insertAdjacentHTML('beforeend', '<p class="hint error">' + esc(message) + '</p>');
    }
  });
  panel.querySelectorAll('[data-rollout-scale]').forEach((btn) => btn.addEventListener('click', async () => {
    const card = btn.closest('[data-rollout-id]'); const target = btn.getAttribute('data-rollout-scale');
    const input = card && card.querySelector('[data-rollout-replicas="' + target + '"]'); const replicas = input ? Number(input.value) : NaN;
    if (!Number.isInteger(replicas) || replicas < 0 || replicas > 1000) { setEnvError('实例数必须是 0 到 1000 的整数'); return; }
    btn.disabled = true;
    try { await api('/api/parallel-rollouts/' + encodeURIComponent(card.getAttribute('data-rollout-id')) + '/scale', { method: 'POST', body: JSON.stringify({ target, replicas }) }); await loadEnvRollouts(panel.getAttribute('data-env-rollout-list'), panel, mode); }
    catch (e) { setEnvError(e.message); } finally { btn.disabled = false; }
  }));
  panel.querySelectorAll('[data-rollout-offline]').forEach((btn) => btn.addEventListener('click', async () => {
    const card = btn.closest('[data-rollout-id]'); const name = card && card.querySelector('.is-old span');
    if (!await askEnvConfirm('确认下线旧版本「' + ((name && name.textContent) || '') + '」？', {title:'老版本下线', confirmLabel:'确认下线'})) return;
    btn.disabled = true; btn.textContent = '正在提交下线…';
    try {
      await api('/api/parallel-rollouts/' + encodeURIComponent(card.getAttribute('data-rollout-id')) + '/offline-old', { method: 'POST', body: '{}' });
      await pollEnvRolloutAction(card.getAttribute('data-rollout-id'), panel, mode);
    }
    catch (e) { setEnvError(e.message); alert(e.message || '下线失败'); } finally { btn.disabled = false; }
  }));
  panel.querySelectorAll('[data-rollout-rollback]').forEach((btn) => btn.addEventListener('click', async () => {
    const card=btn.closest('[data-rollout-id]');
    btn.disabled=true; btn.textContent='正在读取版本信息…'; try {
      const base='/api/parallel-rollouts/'+encodeURIComponent(card.dataset.rolloutId);
      const {plan}=await api(base+'/rollback-plan',{method:'POST',body:'{}'});
      btn.textContent='一键回滚'; btn.disabled=false;
      const message='恢复版本「'+plan.target+'」，就绪后清理：'+(plan.delete.join('、') || '无')+'。'+(plan.affected.length ? '另有 '+plan.affected.length+' 条待下线发布将结束为已完成。' : '')+'确认继续？';
      if(!await askEnvConfirm(message, {title:'一键回滚', confirmLabel:'确认回滚', danger:false})) return;
      btn.disabled=true; btn.textContent='正在提交回滚…';
      const data=await api(base+'/rollback',{method:'POST',body:JSON.stringify({plan_token:plan.token})});
      if(data.approval_required){ btn.textContent='等待人工审批'; await openJob(currentJobId, { fromHistory: true }); return; }
      await pollEnvRolloutAction(card.dataset.rolloutId, panel, mode);
    }
    catch(e){ alert(e.message); } finally { btn.disabled=false; }
  }));
}

async function pollEnvRolloutAction(rolloutId, panel, mode) {
  for (let attempt = 0; attempt < 300 && panel && panel.isConnected !== false; attempt += 1) {
    const rows = await loadEnvRollouts(panel.getAttribute('data-env-rollout-list'), panel, mode, true);
    const row = (rows || []).find((item) => item.id === rolloutId);
    const phase = row && (mode === 'rollback' ? row.recovery_phase : row.offline_phase);
    if (!row || phase === 'failed' || phase === 'completed' || (mode === 'release' && row.status === 'old_deleted') || (mode === 'rollback' && row.status === 'rolled_back')) return;
    await new Promise((resolve) => setTimeout(resolve, attempt < 10 ? 1000 : 2000));
  }
}

async function loadEnvRollouts(id, panel, mode, quiet) {
  if (!id || !panel) return;
  panel.hidden = false;
  if (!quiet && (!panel.querySelector('.env-secret-hint') || !/加载中/.test(panel.textContent || ''))) {
    panel.innerHTML = '<p class="env-secret-hint">加载中…</p>';
  }
  try {
    const jobId = panel.dataset.jobId || '';
    const qs = jobId ? ('?job_id=' + encodeURIComponent(jobId) + '&live=1') : '?live=1';
    const data = await api('/api/environments/' + encodeURIComponent(id) + '/rollouts' + qs);
    const rows = data.rollouts || [];
    renderEnvRollouts(panel, rows, '', mode);
    return rows;
  }
  catch (e) { renderEnvRollouts(panel, [], e.message, mode); return []; }
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
  if (!canManageEnvironments()) {
    setEnvError("没有环境管理权限");
    return;
  }
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
function openEnvViewOverlay(env) {
  const overlay = $("envOverlay");
  const body = $("envOverlayBody");
  const title = $("envOverlayTitle");
  if (!overlay || !body || !title || !env) return;
  envDraftOpen = false;
  envEditingId = "";
  title.textContent = "查看环境 · " + (env.name || "未命名环境");
  setEnvError("");
  setEnvFormError("");
  body.innerHTML = envReadonlyHtml(env);
  bind("btnEnvViewClose", () => closeEnvOverlay());
  overlay.hidden = false;
  syncEnvOverlayLock();
  if ($("btnEnvViewClose")) $("btnEnvViewClose").focus();
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
  grid.querySelectorAll("[data-env-id]").forEach((card) => {
    const view = () => {
      const env = environments.find((item) => item.id === (card.getAttribute("data-env-id") || ""));
      if (env) openEnvViewOverlay(env);
    };
    card.addEventListener("click", (ev) => {
      if (ev.target.closest("button, input, select, a")) return;
      view();
    });
    card.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      if (ev.target !== card) return;
      ev.preventDefault(); view();
    });
  });
  grid.querySelectorAll("[data-env-edit]").forEach((btn) => {
    if (btn.disabled) return;
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (!canManageEnvironments()) {
        setEnvError("没有环境管理权限");
        return;
      }
      const id = btn.getAttribute("data-env-edit") || "";
      const env = environments.find((item) => item.id === id);
      if (env) openEnvOverlay(env);
    });
  });
  grid.querySelectorAll("[data-env-delete]").forEach((btn) => {
    if (btn.disabled) return;
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (!canManageEnvironments()) {
        setEnvError("没有环境管理权限");
        return;
      }
      deleteEnvironment(btn.getAttribute("data-env-delete") || "").catch((e) => setEnvError(e.message));
    });
  });
  grid.querySelectorAll("[data-env-rollouts]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-env-rollouts") || "";
      const panel = grid.querySelector('[data-env-rollout-list="' + id + '"]');
      if (panel) loadEnvRollouts(id, panel);
    });
  });
}
async function loadEnvironments() {
  setEnvError("");
  if (isAllServices() || !currentServiceId) {
    environments = [];
    renderEnvironments();
    return;
  }
  const [data, permissionData] = await Promise.all([
    api("/api/environments?service_id=" + encodeURIComponent(currentServiceId)),
    api('/api/permissions?service_id=' + encodeURIComponent(currentServiceId)),
  ]);
  servicePermissions = permissionData.permissions || [];
  environments = data.environments || [];
  const createBtn = $("btnEnvCreate");
  if (createBtn) {
    createBtn.disabled = !canManageEnvironments();
    createBtn.title = canManageEnvironments() ? "" : "没有环境管理权限";
  }
  renderEnvironments();
}
function permissionOwners(permissionId) {
  return ((servicePermissions.find((item) => item.permission_id === permissionId) || {}).owners || []);
}
function canManageEnvironments() {
  return permissionOwners('environment_manage').includes(currentUser);
}
async function loadPermissions() {
  const root = $("permissionGrid"); if (!root || !currentServiceId || isAllServices()) return;
  const data = await api('/api/permissions?service_id=' + encodeURIComponent(currentServiceId));
  servicePermissions = data.permissions || [];
  const accounts = data.accounts || [];
  const specs = [
    { permission_id: 'production_release', permission: '生产发布权限' },
    { permission_id: 'environment_manage', permission: '环境管理权限' },
  ];
  const byId = {};
  servicePermissions.forEach((row) => { byId[row.permission_id] = row; });
  root.innerHTML = '<table class="art-table permission-table"><thead><tr><th>权限</th><th>责任人</th></tr></thead><tbody></tbody></table>';
  const tbody = root.querySelector('tbody');
  specs.forEach((spec) => {
    const row = byId[spec.permission_id] || spec;
    const initialOwners = [...(row.owners || [])];
    const editable = initialOwners.includes(currentUser);
    const tr = document.createElement('tr');
    const nameTd = document.createElement('td');
    nameTd.innerHTML = '<strong>' + esc(row.permission || spec.permission) + '</strong>';
    const owners = document.createElement('td');
    owners.className = 'permission-owner-cell';
    const editor = document.createElement('div');
    editor.className = 'permission-owner-editor';
    const inputWrap = document.createElement('div');
    inputWrap.className = 'permission-owner-input' + (editable ? '' : ' is-disabled');
    const account = document.createElement('input');
    account.type = 'text';
    account.className = 'input permission-account';
    account.placeholder = editable ? '输入账号，回车添加' : '';
    account.disabled = !editable;
    inputWrap.appendChild(account);
    const save = document.createElement('button');
    save.type = 'button';
    save.className = 'btn permission-save';
    save.textContent = '保存修改';
    const currentOwners = () => [...inputWrap.querySelectorAll('.permission-chip')].map((c) => c.firstChild.nodeValue);
    const syncSave = () => {
      const next = currentOwners();
      const dirty = next.length !== initialOwners.length || next.some((name, idx) => name !== initialOwners[idx]);
      save.disabled = !editable || !dirty || next.length < 1;
      save.classList.toggle('is-dirty', editable && dirty && next.length >= 1);
      save.classList.toggle('primary', editable && dirty && next.length >= 1);
    };
    const addChip = (name) => {
      const chip = document.createElement('span');
      chip.className = 'permission-chip';
      chip.appendChild(document.createTextNode(name));
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '×';
      remove.title = '移除 ' + name;
      remove.disabled = !editable;
      remove.addEventListener('click', (ev) => {
        ev.stopPropagation();
        if (!editable) return;
        if (currentOwners().length <= 1) {
          const err = $('permissionError');
          err.hidden = false;
          err.textContent = '至少保留一名责任人';
          return;
        }
        chip.remove();
        syncSave();
      });
      chip.appendChild(remove);
      inputWrap.insertBefore(chip, account);
    };
    const tryAddOwner = () => {
      if (!editable) return;
      const name = account.value.trim();
      const err = $('permissionError');
      if (!name) return;
      if (!accounts.includes(name)) {
        err.hidden = false;
        err.textContent = '账号不存在';
        account.focus();
        return;
      }
      if (currentOwners().includes(name)) {
        err.hidden = false;
        err.textContent = '该账号已是责任人';
        return;
      }
      err.hidden = true;
      err.textContent = '';
      addChip(name);
      account.value = '';
      syncSave();
    };
    initialOwners.forEach(addChip);
    account.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        tryAddOwner();
      }
    });
    inputWrap.addEventListener('click', () => { if (editable) account.focus(); });
    save.addEventListener('click', async () => {
      const next = currentOwners();
      try {
        await api('/api/permissions', {
          method: 'POST',
          body: JSON.stringify({ service_id: currentServiceId, permission_id: row.permission_id || spec.permission_id, owners: next }),
        });
        await loadPermissions();
      } catch (e) {
        const err = $('permissionError');
        err.hidden = false;
        err.textContent = e.message;
      }
    });
    syncSave();
    editor.append(inputWrap, save);
    owners.appendChild(editor);
    tr.append(nameTd, owners);
    tbody.appendChild(tr);
  });
}
function openEnvCreate() {
  if (isAllServices() || !currentService()) {
    setEnvError("请先选择一个微服务，再创建它的环境。");
    return;
  }
  if (!canManageEnvironments()) {
    setEnvError("没有环境管理权限");
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
function askEnvConfirm(message, options = {}) {
  return new Promise((resolve) => {
    if (envConfirmResolver) envConfirmResolver(false);
    envConfirmResolver = resolve;
    const text = $("envConfirmText");
    if (text) text.textContent = message;
    const title = $("envConfirmTitle");
    if (title) title.textContent = options.title || "删除环境";
    const confirm = $("btnEnvConfirmOk");
    if (confirm) {
      confirm.textContent = options.confirmLabel || "删除";
      confirm.classList.toggle("danger", options.danger !== false);
      confirm.classList.toggle("primary", options.danger === false);
    }
    if ($("envConfirm")) $("envConfirm").hidden = false;
    syncEnvOverlayLock();
    if ($("btnEnvConfirmCancel")) $("btnEnvConfirmCancel").focus();
  });
}
async function deleteEnvironment(id) {
  if (!id) return;
  if (!canManageEnvironments()) {
    setEnvError("没有环境管理权限");
    return;
  }
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

function returnToPipeline() {
  const target = new URLSearchParams(location.search).get('return_to');
  if (target && /^\/(pipeline|submit)(\/|$)/.test(target) && !target.includes('\\')) {
    const url = new URL(target, location.origin);
    if (url.origin === location.origin) { location.replace(url.href); return true; }
  }
  return false;
}

$("loginForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("loginError").hidden = true;
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username: $("authUser").value.trim(), password: $("authPass").value }),
    });
    if (!returnToPipeline()) await bootApp(data.username);
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
    if (me.user) { if (!returnToPipeline()) await bootApp(me.user); }
    else showLogin();
  } catch (_) {
    showLogin();
  }
})();

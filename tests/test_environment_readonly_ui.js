const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const context = vm.createContext({
  services: [{ id: 'agent-governance-gw', title: '治理网关' }],
  serviceTitle: (item) => item.title || item.id,
  esc: (value) => String(value ?? '').replace(/[&<>"']/g, ''),
  canManageEnvironments: () => false,
  envIconPencil: () => '<svg></svg>',
  envIconTrash: () => '<svg></svg>',
});
for (const [startNeedle, endNeedle] of [
  ['function envReadonlyHtml(', 'function envCardHtml('],
  ['function envCardHtml(', 'function rolloutWorkloadHtml('],
]) {
  const start = source.indexOf(startNeedle);
  const end = source.indexOf(endNeedle, start);
  assert.ok(start >= 0 && end > start);
  vm.runInContext(source.slice(start, end), context);
}

const env = {
  id: 'env1', name: 'multi001', service_id: 'agent-governance-gw', environment_type: 'production',
  region: 'cn-southwest-2', region_label: '贵阳一', cluster_name: 'multi001',
  workload_name: 'governance', active_workload_name: 'governance-v-202609151756',
  namespace: 'echo-prod', kubeconfig_path: '/etc/robot-ci/kubeconfigs/dev2.yaml',
  jump_host: 'root@jump', nodes: ['node-a', 'node-b'], has_jump_password: true,
  has_node_password: true, created_by: 'owner', created_at: 'created', updated_at: 'updated',
  jump_password: 'must-not-render', node_password: 'must-not-render-either',
};
const view = context.envReadonlyHtml(env);
for (const expected of ['当前活动负载', 'governance-v-202609151756', 'echo-prod', '/etc/robot-ci/kubeconfigs/dev2.yaml', 'root@jump', 'node-a', 'node-b', '已配置（不显示明文）']) {
  assert.match(view, new RegExp(expected));
}
assert.doesNotMatch(view, /must-not-render/);
assert.doesNotMatch(view, /<input|<select|保存/);

const lockedCard = context.envCardHtml(env);
assert.match(lockedCard, /role="button" tabindex="0"/);
assert.match(lockedCard, /aria-label="查看环境 multi001"/);
assert.match(lockedCard, /data-env-edit="env1"[^>]*disabled/);
assert.match(lockedCard, /data-env-delete="env1"[^>]*disabled/);

context.canManageEnvironments = () => true;
const editableCard = context.envCardHtml(env);
assert.doesNotMatch(editableCard, /data-env-edit="env1"[^>]*disabled/);
assert.doesNotMatch(editableCard, /data-env-delete="env1"[^>]*disabled/);

assert.match(source, /grid\.querySelectorAll\("\[data-env-id\]"\)/);
assert.match(source, /if \(ev\.key !== "Enter" && ev\.key !== " "\)/);
assert.match(source, /if \(ev\.target\.closest\("button, input, select, a"\)\) return/);
console.log('Environment read-only view states: passed');

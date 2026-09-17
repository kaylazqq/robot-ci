const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const start = source.indexOf('function rolloutWorkloadHtml(');
const end = source.indexOf('function bindEnvForm()', start);
const context = vm.createContext({
  esc: (s) => String(s ?? ''),
  setTimeout: () => 1,
  clearTimeout: () => {},
});
vm.runInContext(source.slice(start, end), context);
const panel = { dataset: {}, innerHTML: '', querySelectorAll: () => [] };
const base = { id: 'release2', old: {exists:true}, new:{exists:true, desired_replicas:1}, can_rollback:true };
context.renderEnvRollouts(panel, [{...base, status:'superseded', can_manage:false}], '', 'release');
assert.match(panel.innerHTML, /已完成 · 已由历史回滚结束/);
assert.doesNotMatch(panel.innerHTML, /data-rollout-offline|data-rollout-scale/);
context.renderEnvRollouts(panel, [{...base, old:{exists:false}, status:'old_deleted', can_manage:false}], '', 'release');
assert.match(panel.innerHTML, /data-rollout-rollback/);
context.renderEnvRollouts(panel, [{...base, status:'rolled_back', can_manage:false}], '', 'rollback');
assert.doesNotMatch(panel.innerHTML, /data-rollout-rollback/);
context.renderEnvRollouts(panel, [{...base, old:{exists:false}, status:'active', recovery_phase:'restoring'}], '', 'rollback');
assert.match(panel.innerHTML, /回滚执行中/);
assert.match(panel.innerHTML, /正在恢复目标版本/);
assert.doesNotMatch(panel.innerHTML, /data-rollout-rollback/);
context.renderEnvRollouts(panel, [{...base, status:'active', recovery_phase:'cleaning'}], '', 'rollback');
assert.match(panel.innerHTML, /目标版本已就绪，正在清理其他版本/);
context.renderEnvRollouts(panel, [{...base, status:'active', can_manage:true, offline_phase:'offlining'}], '', 'release');
assert.match(panel.innerHTML, /正在删除旧版本/);
assert.doesNotMatch(panel.innerHTML, /data-rollout-offline/);
context.renderEnvRollouts(panel, [{...base, status:'old_deleted', can_manage:false, offline_phase:'completed', offline_updated_at:'2026-09-16 11:17:44'}], '', 'release');
assert.match(panel.innerHTML, /下线完成/);
assert.match(panel.innerHTML, /最近更新：2026-09-16 11:17:44/);
// Operation card must sit below both version comparison cards.
assert.match(panel.innerHTML, /旧版本负载[\s\S]*新版本负载[\s\S]*rollout-operation[\s\S]*下线完成/);
assert.doesNotMatch(panel.innerHTML, /rollout-operation[\s\S]*旧版本负载/);
context.renderEnvRollouts(panel, [{...base, status:'active', can_manage:true}], '', 'release');
assert.match(panel.innerHTML, /data-rollout-offline/);
assert.match(panel.innerHTML, /data-rollout-scale/);
assert.match(panel.innerHTML, /旧版本负载[\s\S]*data-rollout-offline[\s\S]*新版本负载/);
context.renderEnvRollouts(panel, [{...base, new:{exists:true, desired_replicas:0}, status:'active', can_manage:true}], '', 'release');
assert.match(panel.innerHTML, /disabled aria-disabled="true" title="请先将新版本负载实例数调整为大于 0">下线/);
assert.doesNotMatch(panel.innerHTML, /data-rollout-offline/);
context.renderEnvRollouts(panel, [{...base, status:'active', can_manage:true, scale_phase:'scaling', scale_target:'new', scale_replicas:3, scale_updated_at:'2026-09-16 18:00:00'}], '', 'release');
assert.match(panel.innerHTML, /正在将新版本实例数调整为 3/);
assert.match(panel.innerHTML, /最近更新：2026-09-16 18:00:00/);
assert.doesNotMatch(panel.innerHTML, /data-rollout-scale/);
context.renderEnvRollouts(panel, [{...base, status:'active', can_manage:true, scale_phase:'completed', scale_target:'new', scale_replicas:3}], '', 'release');
assert.match(panel.innerHTML, /实例数调整完成/);
context.renderEnvRollouts(panel, [{...base, old:{exists:false}, can_rollback:false}], '', 'rollback');
assert.doesNotMatch(panel.innerHTML, /历史版本缺少恢复快照/);
assert.doesNotMatch(panel.innerHTML, /data-rollout-rollback/);
assert.equal(context.rolloutActionSettled({status:'old_deleted', offline_phase:'offlining'}, 'release'), false);
assert.equal(context.rolloutActionSettled({status:'old_deleted', offline_phase:'completed'}, 'release'), true);
assert.equal(context.rolloutActionSettled({status:'active', recovery_phase:'cleaning'}, 'rollback'), false);
assert.equal(context.rolloutActionSettled({status:'rolled_back', recovery_phase:'completed'}, 'rollback'), true);
assert.equal(context.rolloutActionSettled({status:'active', scale_phase:'scaling'}, 'scale'), false);
assert.equal(context.rolloutActionSettled({status:'active', scale_phase:'completed'}, 'scale'), true);
const operationNode = { className:'', dataset:{}, innerHTML:'' };
const operationCard = {
  ownerDocument: {createElement: () => operationNode},
  querySelector: () => null,
  appendChild: (node) => { operationCard.operation = node; },
};
context.showPendingRolloutOperation(operationCard, '正在将新版本实例数调整为 3');
assert.equal(operationCard.operation, operationNode);
assert.equal(operationNode.className, 'rollout-operation is-running');
assert.equal(operationNode.dataset.operationActive, '1');
assert.match(operationNode.innerHTML, /正在将新版本实例数调整为 3/);
assert.match(source, /rolloutLoadGeneration/);
assert.match(source, /showPendingRolloutOperation\(card, '正在删除旧版本'\)/);
assert.match(source, /panel\._rolloutRefreshTimer = setTimeout/);
console.log('Rollback UI states: passed');

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const start = source.indexOf('const PIPELINE_STATE_LABELS =');
const end = source.indexOf('function isSelectablePreviewTask(', start);
const context = vm.createContext({});
vm.runInContext(source.slice(start, end) + ';this.buildPipelineStages = buildPipelineStages;', context);

const waiting = context.buildPipelineStages({
  prepare: [],
  steps: [
    {id:'release', label:'生产发布', status:'done', subtasks:[
      {id:'deploy', label:'部署新版本', status:'done'},
      {id:'compare', label:'版本对比', status:'done'},
      {id:'offline', label:'老版本下线', status:'queued'},
    ]},
    {id:'rollback', label:'一键回滚', status:'skipped', subtasks:[
      {id:'rollback', label:'一键回滚', status:'skipped'},
    ]},
  ],
}, 'ok');

assert.equal(waiting.length, 1);
assert.equal(waiting[0].id, 'release');
assert.equal(waiting[0].status, 'waiting');
assert.deepEqual(JSON.parse(JSON.stringify(waiting[0].tasks)), [{
  id: 'deploy', label: '部署新版本', status: 'waiting', logStep: 'release',
}]);

const completed = context.buildPipelineStages({
  prepare: [],
  steps: [{id:'release', label:'生产发布', status:'done', subtasks:[
    {id:'deploy', status:'done'}, {id:'compare', status:'done'}, {id:'offline', status:'done'},
  ]}],
}, 'ok');
assert.equal(completed[0].status, 'done');
assert.equal(completed[0].tasks[0].status, 'done');

const previewStart = source.indexOf('function isSelectablePreviewTask(');
const previewEnd = source.indexOf('function setPreviewHint(', previewStart);
const previewContext = vm.createContext({isProductionReleaseTemplate: () => false});
vm.runInContext('let deploymentType="code"; let runPreviewSelection={deploy:false,gammaDeploy:false,test:false};' +
  source.slice(previewStart, previewEnd) + ';this.previewPipelineData=previewPipelineData;this.isSelectablePreviewTask=isSelectablePreviewTask;', previewContext);
let personalPreview = previewContext.previewPipelineData();
assert.equal(personalPreview.steps.some((step) => step.id === 'release'), false);
assert.equal(previewContext.isSelectablePreviewTask('release', 'deploy'), false);
previewContext.isProductionReleaseTemplate = () => true;
const releasePreview = previewContext.previewPipelineData();
assert.equal(releasePreview.steps.some((step) => step.id === 'release'), true);
assert.equal(previewContext.isSelectablePreviewTask('release', 'deploy'), true);

console.log('Production release pipeline UI: passed');

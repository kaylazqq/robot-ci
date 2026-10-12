const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const start = source.indexOf('function createReleaseModePicker(');
const end = source.indexOf('function createEndpoint(', start);

assert.ok(start >= 0 && end > start, 'release mode picker should exist');
const pickerSource = source.slice(start, end);

assert.match(pickerSource, /id: 'parallel'/);
assert.match(pickerSource, /id: 'inplace'/);
assert.doesNotMatch(pickerSource, /id: 'inplace'[^\n]*disabled:\s*true/);
assert.match(pickerSource, /runPreviewSelection\.deploymentMode = choice\.id/);

assert.match(source, /deploymentMode: \(tpl && tpl\.deployment_mode\) \|\| "parallel"/);
assert.match(source, /deployment_mode: runPreviewSelection\.deploymentMode \|\| "parallel"/);
assert.match(source, /runLayerMode === "edit"[\s\S]*createReleaseModePicker/);
assert.match(source, /createConfiguredRunSummary\(doc, stage\.id\)/);

console.log('Pipeline-configured release options: passed');

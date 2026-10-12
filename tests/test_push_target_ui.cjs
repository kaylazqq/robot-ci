const fs = require('fs');
const assert = require('assert');

const app = fs.readFileSync('web/app.js', 'utf8');
const html = fs.readFileSync('web/index.html', 'utf8');

assert(html.includes('id="pushTargetSelect"'), 'run page must expose push target selector');
assert(html.includes('id="btnPushTargetCreate"'), 'run page must expose push target creation');
assert(app.includes('/api/push-targets?service_id='), 'UI must load service-scoped push targets');
assert(app.includes('push_target_id: runPreviewSelection.pushTargetId || ""'), 'run and template payloads must carry selected target');
assert(app.includes('docker login 命令'), 'target editor must accept docker login commands');
assert(!app.includes('body: JSON.stringify({ command: payload.login_command })'), 'target credentials must not be routed through legacy login API');

console.log('push target UI tests passed');

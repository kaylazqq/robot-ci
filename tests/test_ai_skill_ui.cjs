const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'web', 'index.html'), 'utf8');
const app = fs.readFileSync(path.join(root, 'web', 'app.js'), 'utf8');

assert(html.includes('data-tab="ai"'));
assert(html.includes('id="btnDownloadSkill"'));
assert(html.includes('value="codex"'));
assert(html.includes('value="claude"'));
assert(html.includes('value="cursor"'));
assert(app.includes('/api/ai/skill-packages'));
assert(app.includes('robot-ci-agent-skill.zip'));
console.log('AI Skill download UI: passed');

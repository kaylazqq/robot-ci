const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

assert.match(source, /let currentUserIsSuperAdmin = false/);
assert.match(source, /currentUserIsSuperAdmin = !!data\.is_super_admin/);
assert.match(source, /currentUserIsSuperAdmin \|\| initialOwners\.includes\(currentUser\)/);
assert.match(source, /api\('\/api\/accounts'/);
assert.match(source, /账号管理/);

console.log('Super admin account and whitelist UI: passed');

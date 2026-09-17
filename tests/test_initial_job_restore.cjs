const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const start = source.indexOf('async function openRoutedJobOrLatest(');
const end = source.indexOf('function startPolling(', start);

async function scenario(openResult) {
  const calls = [];
  const context = vm.createContext({
    loadPipelineTemplates: async () => { calls.push('templates'); },
    openJob: async () => { calls.push('open'); if (openResult instanceof Error) throw openResult; return openResult; },
    writeHash: () => calls.push('hash'),
    loadServiceJob: async () => { calls.push('latest'); },
  });
  vm.runInContext('let pinnedJobId="expired-job";' + source.slice(start, end) +
    ';this.restore=openRoutedJobOrLatest;this.pinned=()=>pinnedJobId;', context);
  const result = await context.restore('expired-job');
  return {calls, result, pinned: context.pinned()};
}

(async () => {
  const valid = await scenario(true);
  assert.deepEqual(valid.calls, ['templates', 'open']);
  assert.equal(valid.result, true);
  assert.equal(valid.pinned, 'expired-job');

  const expired = await scenario(new Error('404'));
  assert.deepEqual(expired.calls, ['templates', 'open', 'hash', 'latest']);
  assert.equal(expired.result, false);
  assert.equal(expired.pinned, '');
  console.log('Initial job restore fallback: passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });

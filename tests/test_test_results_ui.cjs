const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  try {
    const page = await browser.newPage();
    await page.setContent('<div id="root"></div>');
    const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
    const start = source.indexOf('function decodeUnicodeEscapes(');
    const end = source.indexOf('function flattenPipelineSteps(');
    assert.ok(start >= 0 && end > start, 'test-result rendering functions were not found');
    await page.addScriptTag({content: `
      const testTableState = new Map();
      function formatDuration(value) { return String(value) + 'ms'; }
      function closeModal() { window.closedFailureModal = true; }
      function openModal(title, content) {
        window.failureModal = {title, text: content.textContent};
        document.body.append(content);
      }
      ${source.slice(start, end)}
    `});
    const result = await page.evaluate(() => {
      let openedLog = false;
      renderTestRun(document.querySelector('#root'), {
        status: 'failed',
        summary: {total: 5, passed: 2, failed: 1, errors: 1},
        test_cases: [
          {name: 'pass first', status: 'passed', duration_ms: 1},
          {name: 'skip', status: 'skipped', duration_ms: 1},
          {name: 'failure', status: 'failed', duration_ms: 1, detail: 'assertion did not match'},
          {name: 'error', status: 'error', duration_ms: 1, detail: 'connection refused'},
          {name: 'pass second', status: 'passed', duration_ms: 1},
        ],
      }, 'UT 测试结果', () => { openedLog = true; });
      const rows = [...document.querySelectorAll('.test-table tbody tr')].map((row) => row.children[0].textContent);
      const failureButton = [...document.querySelectorAll('.test-failure-button')].find((button) => button.textContent === '失败');
      failureButton.click();
      const modalText = window.failureModal.text;
      [...document.querySelectorAll('.test-failure-detail .btn')][0].click();
      return {rows, modalText, openedLog, closed: window.closedFailureModal};
    });
    assert.deepEqual(result.rows, ['failure', 'error', 'pass first', 'skip', 'pass second']);
    assert.match(result.modalText, /assertion did not match/);
    assert.equal(result.openedLog, true);
    assert.equal(result.closed, true);
    console.log('PASS: failures first, clickable failure detail, and log navigation');
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exit(1); });

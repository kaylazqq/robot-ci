const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true,channel:'chrome'});
  try {
    const page = await browser.newPage();
    await page.setContent('<pre id="log"></pre>');
    const source = fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
    await page.addScriptTag({content:source.slice(source.indexOf('function pipelineUrl('),source.indexOf('function esc('))});
    const result = await page.evaluate(() => {
      const el = document.querySelector('#log');
      const text = '<img src=x onerror="alert(1)">\nPipeline: http://119.8.233.58:8080/batches/gamma-test\n(https://example.test/a(b)?x=1&y=2). https://example.test/中文。 javascript:alert(1) https://user:password@example.test/';
      paintLog(el,text,{follow:true});
      const links = [...el.querySelectorAll('a')].map(a=>({href:a.getAttribute('href'),rel:a.rel,target:a.target}));
      const unchanged=el.textContent===text, injected=el.querySelectorAll('img,script').length;
      const state={follow:false};paintLog(el,'https://example.test/next',state);
      const paused=el.textContent===text;state.follow=true;state.flush();
      return {links,unchanged,injected,paused,flushed:el.querySelector('a').href};
    });
    assert.equal(result.unchanged,true);assert.equal(result.injected,0);assert.equal(result.paused,true);
    assert.deepEqual(result.links.map(a=>a.href),['http://119.8.233.58/pipeline/batches/gamma-test','https://example.test/a(b)?x=1&y=2','https://example.test/中文']);
    assert.ok(result.links.every(a=>a.target==='_blank' && a.rel==='noopener noreferrer'));
    assert.equal(result.flushed,'https://example.test/next');
    console.log('PASS: log URLs, punctuation, XSS safety, credential URLs, paused scrolling and flush');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1)});

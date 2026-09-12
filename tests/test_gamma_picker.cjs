// Real deployed component and CSS; no build is submitted by this UI test.
const {chromium, expect} = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const root=path.resolve(__dirname,'..');
  const source=fs.readFileSync(path.join(root,'web/app.js'),'utf8');
  const component=source.slice(source.indexOf('function createStageColumn('),source.indexOf('function selectedEnvName('));
  const browser=await chromium.launch({headless:true});
  try {
    for (const width of [1440,1920,390]) {
      const page=await browser.newPage({viewport:{width,height:1000}});
      await page.setContent('<main style="max-width:100%;width:360px"></main>');
      await page.addStyleTag({content:fs.readFileSync(path.join(root,'web/styles.css'),'utf8')});
      await page.addScriptTag({content:`
        let runPreviewSelection={environmentId:'9253bcacca80',test:true,deploy:true};
        function createStatusIcon(status, cls, doc){const e=doc.createElement('span');e.className=cls;return e;}
        function createGammaEnvPicker(doc){const e=doc.createElement('span');e.textContent='dev-gamma';return e;}
        ${component}
        document.querySelector('main').append(createStageColumn({id:'gamma',label:'gamma集成测试',tasks:[]},false,false,null,true));
      `});
      await expect(page.locator('.gamma-suite-options input[type=checkbox]')).toHaveCount(6);
      for(const id of ['E04','E05','E06']) await page.locator('input[value='+id+']').check();
      const selected=await page.evaluate(()=>runPreviewSelection.suites);
      if(selected.join(',')!=='E01,E02,E03,E04,E05,E06') throw Error('Suite selection was lost');
      await expect(page.getByText('基线对照',{exact:true})).toHaveCount(0);
      if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1)) throw Error('Horizontal overflow');
      await page.screenshot({path:`/var/lib/pr-e2e/gamma-ui-qa/picker-${width}.png`,fullPage:true});
      console.log(JSON.stringify({width,selected,environment:'multica dev-gamma',passed:true}));
      await page.close();
    }
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});

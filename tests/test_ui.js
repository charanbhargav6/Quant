const puppeteer = require('puppeteer');
(async () => {
  const browser = await puppeteer.launch();
  const page = await browser.newPage();
  page.on('console', msg => console.log('PAGE LOG:', msg.text()));
  page.on('pageerror', error => console.log('PAGE ERROR:', error.message));
  page.on('response', response => {
    if(!response.ok()) console.log('NETWORK ERROR:', response.url(), response.status());
  });
  await page.goto('http://127.0.0.1:8765');
  await new Promise(r => setTimeout(r, 1000));
  await page.evaluate(() => {
    document.querySelector('[data-tab="accounts"]').click();
  });
  await new Promise(r => setTimeout(r, 2000));
  await browser.close();
})();

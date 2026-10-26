// Smoke test against a running instance with populated synthetic data.
const playwright = await import(process.env.MAPEXP_PLAYWRIGHT_MODULE || 'playwright-core');
const engine = process.env.MAPEXP_BROWSER || 'chromium';
const base = process.env.MAPEXP_URL || 'http://127.0.0.1:8010';
const browser = await playwright[engine].launch(engine === 'chromium'
  ? { executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] }
  : { headless: true });
const { default: assert } = await import('node:assert/strict');
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [], statuses = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('response', response => {
    if (new URL(response.url()).pathname === '/api/events') statuses.push(response.status());
  });
  const response = await page.request.get(base + '/api/events');
  const events = await response.json();
  assert.ok(events.length > 0, 'Populate the comparison database before running this check');
  const conditional = await page.request.get(base + '/api/events', { headers: { 'If-None-Match': response.headers().etag } });
  assert.equal(conditional.status(), 304);
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(count => document.querySelectorAll('.card').length === count, events.length);
  await page.waitForFunction(() => document.querySelector('#connection').classList.contains('online'));
  assert.equal(await page.locator('.event-marker').count(), events.filter(event => event.latitude !== null).length);
  for (let attempt = 0; attempt < 50 && !statuses.includes(304); attempt++) await page.waitForTimeout(100);
  assert.ok(statuses.includes(304), 'App should conditionally revalidate after initial SSE notification');
  await page.locator('#search').fill('no-such-synthetic-event');
  await page.waitForFunction(() => document.querySelectorAll('.card').length === 0);
  await page.locator('#state-action').click();
  await page.waitForFunction(count => document.querySelectorAll('.card').length === count, events.length);
  const mapped = events.find(event => event.latitude !== null);
  await page.locator(`[data-event="${mapped.id}"]`).click();
  await page.waitForSelector('.entry');
  assert.equal(await page.locator('.event-marker.selected').count(), 1);
  await page.locator('#back').click();
  assert.equal(await page.locator('.event-marker.selected').count(), 0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('#expand-feed').click();
  assert.equal(await page.locator('#sidebar').evaluate(node => node.classList.contains('full')), true);
  await page.locator('#sheet-toggle').click();
  assert.equal(await page.locator('#sidebar').evaluate(node => node.classList.contains('peek')), true);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ engine, incidents: events.length, apiStatuses: statuses, errors }));
} finally {
  await browser.close();
}

// Drives the settings page in a real browser against the running test setup.
//   WEB_PORT=18088 KEEP_UI=90 VELIB=... dbus-run-session -- python3 tests/integration_dbus.py &
//   NODE_PATH=<global node_modules> node tests/ui_smoke.mjs http://127.0.0.1:18088 pw-XYZ-123 <screenshot dir>
// Needs Playwright with Chromium (npm i -g playwright && npx playwright install chromium).

import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const [base, password, shots] = process.argv.slice(2);
const auth = 'Basic ' + Buffer.from('x:' + password).toString('base64');
const failures = [];
const check = (cond, msg) => { if (!cond) { failures.push(msg); console.log('FAIL:', msg); } else console.log('ok  :', msg); };

const browser = await chromium.launch();

async function newPage(opts, headers) {
  const ctx = await browser.newContext({ viewport: { width: 1024, height: 768 }, deviceScaleFactor: 1, ...opts,
    extraHTTPHeaders: headers || {} });
  const page = await ctx.newPage();
  page.errors = [];
  page.on('pageerror', e => page.errors.push(String(e)));
  // a 400 is the refused value from the range check, on purpose
  page.on('console', m => { if (m.type() === 'error' && !/status of 400/.test(m.text())) page.errors.push(m.text()); });
  page.on('dialog', d => d.accept());                       // confirm() prompts
  return page;
}
const shot = (page, name) => page.screenshot({ path: `${shots}/${name}.png`, fullPage: false });
const tab = (page, name) => page.getByRole('tab', { name }).click();

// ---- 1. locked (nobody logged in)
let page = await newPage();
await page.goto(base + '/');
await page.getByText('Unlock to edit').waitFor();
check(await page.getByText('Mopeka Standard').first().isVisible(), 'overview shows the gas bottle without login');
check(await page.getByText('Fridge', { exact: false }).first().isVisible(), 'overview shows the fridge');
await shot(page, '01-overview-locked');
await tab(page, 'Gas');
await page.getByText('Unlock the page to change settings').waitFor();
check(true, 'config tabs ask for the password');
check(page.errors.length === 0, 'no console errors (locked): ' + page.errors.join(' | '));
await page.context().close();

// ---- 2. unlock flow through the browser's own password dialog
const ctx = await browser.newContext({ viewport: { width: 1024, height: 768 }, httpCredentials: { username: 'x', password } });
page = await ctx.newPage();
await page.goto(base + '/unlock');
await page.getByText('Unlocked').waitFor();
check(true, '/unlock logs in and returns to the page');
await ctx.close();

// ---- 3. unlocked
page = await newPage({}, { Authorization: auth });
await page.goto(base + '/');
await page.getByText('Unlocked').waitFor();
await shot(page, '02-overview');
check((await page.locator('table tr').count()) >= 4, 'driver table lists the services');

// gas
await tab(page, 'Gas');
await page.getByText('Calibrate: with an empty bottle').waitFor();
await shot(page, '03-gas');
const capacity = page.locator('.field', { hasText: 'Usable gas volume' }).locator('input');
check((await capacity.inputValue()) !== '', 'gas form shows a saved volume');
check((await page.locator('.field', { hasText: 'Temperature type' }).locator('select').inputValue()) === 'outdoor', 'gas form offers the temperature as outdoor sensor by default');
check((await page.locator('.field', { hasText: 'Publish temperature' }).locator('select').inputValue()) === 'yes', 'temperature service shown as on by default');
await page.getByRole('button', { name: /Use as full/ }).click();
const full = page.locator('.field', { hasText: 'Reading when full' }).locator('input');
check((await full.inputValue()) === '233', '"Use as full" copies the live reading (233 mm)');
check(await page.getByText('1 unsaved change').isVisible(), 'save bar counts changes');
await page.locator('select').first().selectOption({ index: 1 });          // bottle preset
await page.getByText('Preset applied').waitFor();
await shot(page, '04-gas-edited');
await page.getByRole('button', { name: 'Save', exact: true }).click();
await page.locator('#toast', { hasText: 'Saved' }).waitFor();
check(true, 'gas settings saved');
await page.locator('#savebar').waitFor({ state: 'hidden' });
await page.waitForTimeout(600);                                   // the form is redrawn after a save
// a wrong value is refused and marked
await capacity.fill('-5');
await page.getByRole('button', { name: 'Save', exact: true }).click();
await page.getByText('must be at least 0').waitFor();
check(true, 'out-of-range value is refused with a message');
await page.getByRole('button', { name: 'Discard' }).click();

// fridge
await tab(page, 'Fridge');
await page.getByText('Fridge address').waitFor();
await page.getByRole('button', { name: '5 min', exact: true }).click();
check(await page.locator('.field', { hasText: 'Read every' }).locator('input').inputValue() === '300', 'poll interval chip sets 300 s');
await shot(page, '05-fridge');
await page.getByRole('button', { name: 'Save & restart driver' }).click();
await page.getByText('Saved and restarted').waitFor();
check(true, 'fridge saved and driver restart requested');

// water
await tab(page, 'Water');
await page.getByText('Sender ID').waitFor();
await shot(page, '06-water');

// battery
await tab(page, 'Battery');
await page.getByText('What I notice').waitFor();
check((await page.locator('.tile').count()) >= 8, 'battery tiles are shown');
check((await page.locator('.chart svg rect').count()) >= 16, 'one bar per cell (16)');
await page.waitForTimeout(8000);                                  // let the Pi record a few points
await page.getByRole('button', { name: '1 hour' }).click();
await page.getByText('Cell 7 is the lowest cell').waitFor();
check(true, 'findings name the weak cell');
await page.locator('.chart path[stroke]').nth(9).waitFor();           // charts draw once they have a width
check((await page.locator('.chart path[stroke]').count()) >= 10, 'graphs are drawn');
await page.locator('.chart .legend button', { hasText: 'Cell 7' }).first().click();
check((await page.locator('.chart .legend button.off').count()) >= 1, 'legend hides a cell line');
const cellsChart = page.locator('.chart', { hasText: 'Cell voltages' }).first();
await cellsChart.scrollIntoViewIfNeeded();
await cellsChart.locator('svg rect').first().hover();
check(await cellsChart.locator('.tt').isVisible(), 'hover shows a tooltip');
await shot(page, '07a-battery-live');
await page.getByRole('button', { name: 'Limits & curves' }).click();
await page.getByText('Cell voltage limits').waitFor();
check((await page.locator('.chart svg path').count()) >= 2, 'limit curves drawn');
await page.getByText('What you changed from the defaults').waitFor();
await shot(page, '07b-battery-limits');
await page.getByRole('button', { name: 'All settings' }).click();
await page.getByText('Most used settings').waitFor();
check((await page.locator('.entry').count()) >= 15, 'all documented settings are listed');
await shot(page, '07-battery');
await page.getByPlaceholder('Search settings').fill('soc calc');
check((await page.locator('.entry:visible').count()) <= 3, 'search narrows the list');
await page.locator('.entry[data-key="SOC_CALCULATION"] .switch').click();
await page.getByPlaceholder('Search settings').fill('');
const dis = page.locator('.entry[data-key="MAX_BATTERY_DISCHARGE_CURRENT"] input[type=number]');
await dis.fill('80');
check((await page.getByText('2 unsaved changes').count()) === 1, 'two changes counted');
await page.locator('.entry .link', { hasText: 'About this setting' }).first().click();
await shot(page, '08-battery-edited');
await page.getByRole('button', { name: 'Save', exact: true }).click();
await page.locator('#toast', { hasText: 'Saved' }).waitFor();
check(true, 'battery settings saved');
// bluetooth list widget -> needs a reboot
await page.getByRole('button', { name: 'Add a BMS' }).click();
await page.locator('.entry[data-key="BLUETOOTH_BMS"] input').fill('c8:47:8c:ed:1b:fb');
await page.getByRole('button', { name: 'Save', exact: true }).click();
await page.getByText('only apply after a reboot').waitFor();
check(true, 'Bluetooth change asks for a reboot');
await shot(page, '09-battery-reboot');
await page.getByText('Only changed').first().isVisible().catch(() => {});

// files & logs
await tab(page, 'Files & logs');
await page.getByText('Drivers: config.ini').waitFor();
check((await page.locator('textarea').first().inputValue()).includes('[fridge]'), 'raw editor shows config.ini');
await page.getByRole('button', { name: 'fridge', exact: true }).click();
await page.locator('dialog pre').waitFor();
await page.locator('dialog pre', { hasText: 'INFO' }).waitFor();         // the text arrives after the dialog opens
check((await page.locator('dialog pre').innerText()).includes('INFO fridge poll ok'), 'log viewer shows the log');
await shot(page, '10-files-log');
check(page.errors.length === 0, 'no console errors (unlocked): ' + page.errors.join(' | '));
await page.context().close();

// ---- 4. phone size
page = await newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 }, { Authorization: auth });
await page.goto(base + '/');
await page.getByText('Unlocked').waitFor();
await shot(page, '11-phone-overview');
await tab(page, 'Battery');
await page.getByText('What I notice').waitFor();
await page.waitForTimeout(3000);
await shot(page, '12a-phone-live');
check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'no sideways scrolling on the live view');
await page.getByRole('button', { name: 'Limits & curves' }).click();
await page.getByText('Cell voltage limits').waitFor();
await shot(page, '12b-phone-limits');
check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'no sideways scrolling on the limits view');
await page.getByRole('button', { name: 'All settings' }).click();
await page.getByText('Most used settings').waitFor();
check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'no sideways scrolling on a phone');
await shot(page, '12-phone-battery');
await page.context().close();

await browser.close();
console.log(failures.length ? `\n${failures.length} FAILED` : '\nUI SMOKE OK');
process.exit(failures.length ? 1 : 0);

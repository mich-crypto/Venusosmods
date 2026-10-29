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
await page.locator('table tr').nth(3).waitFor();
check((await page.locator('table tr').count()) >= 4, 'driver table lists the services');

// gas: a header with the numbers, a few simple settings, and a calibration popup (no typing)
await tab(page, 'Gas');
await page.getByRole('button', { name: 'Calibrate', exact: true }).waitFor();
await shot(page, '03-gas');
check((await page.locator('.metric', { hasText: 'Level' }).locator('.num').innerText()).length > 0, 'gas tab shows the level at the top');
await page.getByRole('heading', { name: /Sensor battery/ }).waitFor();
check(await page.getByRole('heading', { name: /Sensor battery/ }).isVisible(), 'gas tab shows the sensor battery');
await page.locator('.field', { hasText: 'Propane share' }).waitFor({ state: 'attached' });
check((await page.locator('.field .switch', { hasText: /temperature/i }).count()) >= 1, 'temperature is a simple on/off switch');
check((await page.locator('.field', { hasText: 'Propane share' }).count()) === 1 && !(await page.locator('.field', { hasText: 'Propane share' }).isVisible()), 'the technical settings are folded away');
await page.getByRole('button', { name: 'Calibrate', exact: true }).click();
await page.locator('#wiz .livebox').waitFor();
await shot(page, '03a-gas-calibrate');
await page.getByText('Make it match the Mopeka app').click();
await page.getByRole('button', { name: '−' }).first().click();
await page.getByRole('button', { name: 'Match it', exact: true }).click();
await page.getByText('✓ Saved').waitFor();
check(true, 'popup: the level is matched to the app with a slider');
await page.getByRole('button', { name: 'Calibrate more' }).click();
await page.getByText('The bottle is empty right now').click();
await page.getByText('✓ Saved').waitFor();
check(true, 'popup: "empty" is recorded from the live reading');
await page.getByRole('button', { name: 'Calibrate more' }).click();
await page.getByText('Change the bottle type').click();
await page.locator('#wiz .choice').first().click();
await page.getByText('✓ Saved').waitFor();
check(true, 'popup: bottle type chosen from a list');
await page.getByRole('button', { name: 'Close', exact: true }).last().click();
await page.waitForTimeout(800);
// a simple setting goes through the save bar
const gname = page.locator('.field', { hasText: 'Name' }).first().locator('input');
await gname.fill('Camper gas');
await page.getByRole('button', { name: 'Save', exact: true }).click();
await page.locator('#toast', { hasText: 'Saved' }).waitFor();
check(true, 'gas name saved');
await page.locator('#savebar').waitFor({ state: 'hidden' });
await page.waitForTimeout(600);
// a wrong value is refused and marked (inside "More settings")
await page.getByText('More settings').click();
const capacity = page.locator('.field', { hasText: 'Usable gas volume' }).locator('input');
await capacity.fill('-5');
await page.getByRole('button', { name: 'Save', exact: true }).click();
await page.getByText('must be at least 0').waitFor();
check(true, 'out-of-range value is refused with a message');
await page.getByRole('button', { name: 'Discard' }).click();

// fridge
await tab(page, 'Fridge');
await page.getByRole('button', { name: 'Change the fridge' }).waitFor();
await page.getByRole('button', { name: '5 min', exact: true }).click();
check(await page.locator('.field', { hasText: 'Read every' }).locator('input').inputValue() === '300', 'poll interval chip sets 300 s');
await shot(page, '05-fridge');
await page.getByRole('button', { name: 'Save & restart driver' }).click();
await page.getByText('Saved and restarted').waitFor();
check(true, 'fridge saved and driver restart requested');

// fridge controls on the page
await page.locator('#savebar').waitFor({ state: 'hidden' });
await page.waitForTimeout(800);                                   // the form and the controls are redrawn after a save
await page.getByRole('heading', { name: 'Controls' }).waitFor();
await page.locator('.seg').first().waitFor();
check((await page.locator('.ctl .sp').count()) === 2, 'fridge controls show both set point sliders');
await page.locator('.ctl .seg').first().getByRole('button', { name: 'Off' }).click();
await page.getByText('The fridge confirmed.').waitFor({ timeout: 40000 });
check(true, 'switching the fridge off from the page is confirmed by the fridge');
await page.locator('.ctl .seg').first().getByRole('button', { name: 'On' }).click();
await page.getByText('The fridge confirmed.').waitFor({ timeout: 40000 });
await shot(page, '05b-fridge-controls');

// water: calibration popup, buttons only
await tab(page, 'Water');
await page.getByRole('button', { name: 'Change the sender' }).waitFor();
await shot(page, '06-water');
await page.getByRole('button', { name: 'Calibrate', exact: true }).click();
await page.getByText('Quick: empty and full').click();
await page.getByRole('button', { name: '100 L' }).click();
await page.getByRole('button', { name: 'The tank is empty now' }).click();
await page.getByRole('button', { name: 'The tank is full now' }).click();
await page.getByRole('button', { name: 'Save', exact: true }).click();
await page.locator('#toast', { hasText: 'Record empty first' }).waitFor();                // the test sender reports one fixed height
check(true, 'water popup refuses a calibration whose full height is not above empty');
await shot(page, '06a-water-calibrate');
await page.getByRole('button', { name: 'Back' }).click();
await page.getByText('Accurate: fill it in steps').click();
await page.getByRole('button', { name: 'The tank is empty: start' }).click();
await page.getByRole('button', { name: '+5 L' }).click();
await page.getByRole('button', { name: 'Record this level' }).click();
await page.locator('#toast', { hasText: 'did not rise' }).waitFor();
check(true, 'step-by-step calibration waits for the water level to rise');
await page.getByRole('button', { name: 'Close', exact: true }).last().click();
await page.waitForTimeout(500);

// battery
await tab(page, 'Battery');
await page.getByText('What I notice').waitFor();
check((await page.locator('.tile').count()) >= 8, 'battery tiles are shown');
check((await page.locator('.chart svg rect').count()) >= 16, 'one bar per cell (16)');
await page.waitForTimeout(8000);                                  // let the Pi record a few points
await page.getByRole('button', { name: '1 hour' }).click();
await page.getByText('Cell 7 is the lowest cell').waitFor();
check(true, 'findings name the weak cell');
await page.waitForFunction(() => document.querySelectorAll('.chart path[stroke]').length >= 10);   // charts draw once they have a width
check(true, 'graphs are drawn');
await page.locator('.chart .legend button', { hasText: 'Cell 7' }).first().click();
check((await page.locator('.chart .legend button.off').count()) >= 1, 'legend hides a cell line');
const cellsChart = page.locator('.chart', { hasText: 'Cell voltages' }).first();
await cellsChart.scrollIntoViewIfNeeded();
await cellsChart.locator('svg rect').first().hover();
check(await cellsChart.locator('.tt').isVisible(), 'hover shows a tooltip');
await shot(page, '07a-battery-live');
await page.getByRole('button', { name: 'Limits & curves' }).click();
await page.getByText('Cell voltage limits').waitFor();
await page.locator('.chart svg path').nth(1).waitFor();
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

// starlink
await tab(page, 'Starlink');
await page.getByRole('button', { name: 'Dish', exact: true }).click();
await page.getByText('Dish says: roaming').waitFor();
check(await page.locator('.metric', { hasText: 'Downlink' }).locator('.num').innerText().then(t => /^\d+/.test(t)), 'Starlink dish view shows the downlink speed in the hero');
check((await page.locator('.hero .metric').count()) === 3, 'Starlink hero has three metrics');
check((await page.locator('.stats .stat').count()) >= 10, 'Starlink statistics grid is filled');
await page.locator('.chart path[stroke]').first().waitFor({ state: 'attached' });   // a flat line has no height: never "visible"
check((await page.locator('.chart').count()) >= 3, 'Starlink graphs are drawn');
await shot(page, '07c-starlink');
await page.getByRole('button', { name: 'Dashboard', exact: true }).click();
await page.locator('.hzgrid .hz', { hasText: 'Local' }).waitFor();
check(true, 'ping health tile for the target is shown');
await page.getByRole('button', { name: 'Run speed test' }).click();
await page.getByRole('button', { name: 'Run speed test' }).waitFor({ timeout: 60000 });      // back to idle when it is done
await page.getByText('Test edge').waitFor();
check(await page.locator('.metric', { hasText: 'Download' }).locator('.num').innerText().then(t => /^\d+/.test(t)), 'speed test result shows in the hero');
await shot(page, '07c1-starlink-dashboard');
await page.getByRole('button', { name: 'Energy', exact: true }).click();
await page.getByText('Power now').waitFor();
check((await page.locator('.metric', { hasText: 'Today' }).locator('.num').innerText()) !== '', 'energy view shows today');
await shot(page, '07c2-starlink-energy');
await page.getByRole('button', { name: 'Tests', exact: true }).click();
await page.locator('table tbody tr').first().waitFor();
check((await page.locator('table tbody tr .chip.ok').count()) >= 1, 'test table lists the finished test');
await page.getByRole('button', { name: 'Log', exact: true }).click();
await page.getByText('Availability').first().waitFor();
await page.getByText('Public IP history').waitFor();
await shot(page, '07c3-starlink-log');
await page.getByRole('button', { name: 'Sky', exact: true }).click();
await page.getByText('Where the dish points').waitFor();
await page.getByRole('button', { name: 'Debug', exact: true }).click();
await page.getByRole('button', { name: 'Dish', exact: true }).last().click();
await page.locator('pre.rawbox', { hasText: 'packet loss' }).waitFor({ timeout: 20000 });
check(true, 'network tool runs and shows its output');
await shot(page, '07c4-starlink-debug');
await page.getByRole('button', { name: 'Settings', exact: true }).click();
await page.getByText('Software update').waitFor();
await page.getByText('Bot token').waitFor();
check((await page.locator('.field', { hasText: 'Speed test every' }).locator('input').count()) === 1, 'settings offer the speed test interval');
await shot(page, '07c5-starlink-settings');

// raw data
await tab(page, 'Raw data');
await page.locator('.rawbox', { hasText: 'data=' }).waitFor();
check((await page.locator('.rawbox .k-adv').count()) >= 1, 'raw log shows the Mopeka advertisements with their bytes');
check((await page.getByRole('tab', { name: 'Raw data', selected: true }).count()) === 1, 'the open tab is marked as selected');
await shot(page, '07d-raw-gas');
await page.getByRole('button', { name: 'Fridge', exact: true }).click();
await page.getByText('Last status payload, byte by byte').waitFor();
check((await page.locator('.rawbox .k-tx').count()) >= 1 && (await page.locator('.rawbox .k-rx').count()) >= 1, 'raw log shows what is sent to and received from the fridge');
check((await page.locator('details.group tbody tr').count()) >= 20, 'fridge payload is listed byte by byte');
await shot(page, '07e-raw-fridge');

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

// setup guide: scan, pick, add (this adds the sensor to the test setup)
await page.keyboard.press('Escape');
await page.getByRole('button', { name: 'Setup guide' }).click();
await page.getByText('Pick what you want to add').waitFor();
await shot(page, '08-setup-guide');
await page.getByText('Gas bottle level').click();
await page.getByText('ready to pair', { exact: true }).waitFor({ timeout: 30000 });
check(true, 'the scan finds the gas sensor and says it is ready to pair');
await shot(page, '08a-setup-gas-scan');
await page.getByRole('button', { name: 'Use this' }).first().click();
await page.getByText('Which bottle is it?').waitFor();
await page.locator('#wiz .choice').first().click();
await page.getByRole('button', { name: 'Add the sensor' }).click();
await page.getByText('✓ Added').waitFor();
await page.getByText('Where you will see it').waitFor();
check(true, 'the guide adds the sensor and says where to find it in Venus OS');
await shot(page, '08b-setup-gas-done');
await page.getByRole('button', { name: 'Back to the list' }).click();
await page.getByText('Where do I see it in Venus OS?').click();
await page.getByText('the switches icon in the top bar').first().waitFor();
await page.getByRole('button', { name: 'Close', exact: true }).last().click();

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
await tab(page, 'Starlink');
await page.getByRole('button', { name: 'Dish', exact: true }).click();
await page.getByText('Dish says: roaming').waitFor();
await page.waitForTimeout(1200);
await shot(page, '12c-phone-starlink');
check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'no sideways scrolling on the Starlink tab');
await tab(page, 'Battery');
await page.getByRole('button', { name: 'All settings' }).click();
await page.getByText('Most used settings').waitFor();
check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'no sideways scrolling on a phone');
await shot(page, '12-phone-battery');
await page.context().close();

await browser.close();
console.log(failures.length ? `\n${failures.length} FAILED` : '\nUI SMOKE OK');
process.exit(failures.length ? 1 : 0);

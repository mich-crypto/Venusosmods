// Builds docs/Venus-OS-Mods-Guide.pdf from docs/GUIDE.md, wiring.md and calibration.md.
//
//   cd tools/pdf && npm install && npm run build
//
// Needs Playwright with Chromium (npm i -g playwright && npx playwright install chromium).

import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { marked } from 'marked';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const HERE = dirname(fileURLToPath(import.meta.url));
const DOCS = join(HERE, '..', '..', 'docs');
const OUT = join(DOCS, 'Venus-OS-Mods-Guide.pdf');

const CHAPTERS = [
  { file: 'GUIDE.md', id: 'guide', title: 'Installation guide' },
  { file: 'wiring.md', id: 'wiring', title: 'Wiring' },
  { file: 'calibration.md', id: 'calibration', title: 'Calibrating the water tank' },
  { file: 'tank-filling-chart.md', id: 'tank-filling-chart', title: 'Filling chart: wheel-arch tank' },
];

const slug = (s) => s.toLowerCase().replace(/<[^>]+>/g, '').replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');

// Cross-document links become in-PDF anchors; links to repo files point to GitHub
const REPO = 'https://github.com/mich-crypto/Venusosmods/blob/master/';
function fixLinks(md) {
  return md
    // images are embedded from disk
    .replace(/!\[([^\]]*)\]\((?!https?:)([^)]+)\)/g, (_, alt, p) => `![${alt}](file://${join(DOCS, p)})`)
    .replace(/\]\((?:docs\/)?(GUIDE|wiring|calibration|tank-filling-chart)\.md(#[^)]*)?\)/gi,
      (_, f, a) => `](#${a ? a.slice(1) : f.toLowerCase()})`)
    .replace(/\]\((?!https?:|#|file:)([^)]+)\)/g,
      (_, p) => `](${REPO}${p.startsWith('../') ? p.slice(3) : 'docs/' + p})`);
}

const toc = [];
let body = '';
for (const ch of CHAPTERS) {
  let md = readFileSync(join(DOCS, ch.file), 'utf8');
  md = md.replace(/^# .*\n/, '');          // the chapter title comes from CHAPTERS
  const renderer = new marked.Renderer();
  renderer.heading = (text, level) => {
    const id = `${ch.id}-${slug(text)}`;
    if (level === 2) toc.push({ id, text, chapter: ch.id });
    return `<h${level + 1} id="${id}">${text}</h${level + 1}>\n`;
  };
  const html = marked.parse(fixLinks(md), { renderer, gfm: true });
  toc.push({ id: ch.id, text: ch.title, chapter: ch.id, top: true });
  body += `<section class="chapter" id="${ch.id}"><h1>${ch.title}</h1>${html}</section>\n`;
}

// Keep chapter entries before their sections in the contents list
const ordered = [...CHAPTERS, { id: 'datasheets' }].flatMap((ch) => [
  toc.find((t) => t.top && t.chapter === ch.id),
  ...toc.filter((t) => !t.top && t.chapter === ch.id),
]).filter(Boolean);
const tocHtml = ordered.map((t) => t.top
  ? `<li class="toc-ch"><a href="#${t.id}">${t.text}</a></li>`
  : `<li><a href="#${t.id}">${t.text}</a></li>`).join('\n');

// Appendix: datasheet images from docs/datasheets
const sheets = readdirSync(join(DOCS, 'datasheets')).filter((f) => /\.(png|jpe?g)$/i.test(f)).sort();
if (sheets.length) {
  toc.push({ id: 'datasheets', text: 'Appendix: datasheets', chapter: 'datasheets', top: true });
  body += `<section class="chapter" id="datasheets"><h1>Appendix: datasheets</h1>` +
    sheets.map((f) => `<figure><img src="${join(DOCS, 'datasheets', f)}"><figcaption>${f}</figcaption></figure>`).join('') +
    `</section>`;
}

const date = new Date().toISOString().slice(0, 10);
const html = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Venus OS Mods – Installation Guide</title>
<style>
  @page { size: A4; margin: 18mm 16mm 20mm 16mm; }
  :root { --ink:#1d2330; --muted:#5b6474; --accent:#0a6ebd; --rule:#d8dde6; --code:#f3f5f8; }
  html { font-family: Inter, "DejaVu Sans", sans-serif; font-size: 10pt; color: var(--ink); line-height: 1.45; }
  body { margin: 0; }
  a { color: var(--accent); text-decoration: none; }
  h1 { font-size: 22pt; margin: 0 0 6mm; padding-bottom: 3mm; border-bottom: 2px solid var(--accent); }
  h3 { font-size: 14pt; margin: 7mm 0 2mm; color: var(--accent); break-after: avoid; }
  h4 { font-size: 11.5pt; margin: 5mm 0 1.5mm; break-after: avoid; }
  h5 { font-size: 10.5pt; margin: 4mm 0 1mm; break-after: avoid; }
  p, ul, ol { margin: 0 0 2.5mm; }
  li { margin: 0.6mm 0; }
  code { font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; font-size: 8.6pt;
         background: var(--code); padding: 0.2mm 1mm; border-radius: 2px; }
  pre { background: var(--code); border: 1px solid var(--rule); border-radius: 4px;
        padding: 2.5mm 3mm; margin: 0 0 3mm; break-inside: avoid; white-space: pre; overflow: hidden; }
  pre code { background: none; padding: 0; font-size: 8.2pt; line-height: 1.3; }
  table { border-collapse: collapse; width: 100%; margin: 0 0 3.5mm; font-size: 9pt; break-inside: avoid; }
  th, td { border: 1px solid var(--rule); padding: 1.3mm 2mm; text-align: left; vertical-align: top; }
  th { background: #e9eef5; }
  hr { border: 0; border-top: 1px solid var(--rule); margin: 5mm 0; }
  blockquote { margin: 0 0 3mm; padding: 1mm 4mm; border-left: 3px solid var(--accent); color: var(--muted); }
  .chapter { break-before: page; }
  .cover { height: 250mm; display: flex; flex-direction: column; justify-content: center; }
  .cover .kicker { color: var(--accent); font-weight: 600; letter-spacing: .08em; text-transform: uppercase; font-size: 10pt; }
  .cover h1 { font-size: 34pt; border: 0; margin: 3mm 0 4mm; padding: 0; line-height: 1.1; }
  .cover .sub { font-size: 13pt; color: var(--muted); max-width: 150mm; }
  .cover .devices { margin-top: 14mm; font-size: 10.5pt; }
  .cover .devices div { padding: 2.2mm 0; border-top: 1px solid var(--rule); }
  .cover .meta { margin-top: auto; color: var(--muted); font-size: 9pt; }
  .toc { break-before: page; }
  .toc ul { list-style: none; padding: 0; columns: 1; }
  .toc li { padding: 0.8mm 0 0.8mm 6mm; }
  .toc li.toc-ch { padding: 3mm 0 1mm 0; font-weight: 700; font-size: 11pt; }
  .toc li.toc-ch:first-child { padding-top: 0; }
  figure { margin: 0 0 5mm; break-inside: avoid; text-align: center; }
  figure img { max-width: 100%; max-height: 225mm; }
  .chapter p > img { display: block; max-width: 100%; max-height: 110mm; margin: 0 auto 3mm; }
  figcaption { font-size: 8pt; color: var(--muted); margin-top: 1mm; }
  .note { border: 1px solid #f0c36d; background: #fff8e6; border-radius: 4px; padding: 3mm 4mm; margin-top: 8mm; font-size: 9.5pt; }
</style></head><body>
<section class="cover">
  <div class="kicker">Victron Venus OS · Camper van</div>
  <h1>Tank sensors &amp; fridge<br>for Venus OS</h1>
  <div class="sub">Step-by-step guide: wiring, ESP32 firmware upload, tank calibration and
  installation on a Raspberry Pi running Venus OS.</div>
  <div class="devices">
    <div><b>Mopeka Check Standard / XL</b> — LPG bottle level</div>
    <div><b>ESP32-S3 hydrostatic sender</b> — fresh-water level for non-square tanks, water temperature</div>
    <div><b>Bluetooth compressor fridge</b> — fridge and freezer temperature (Alpicool controller; check Hyckes first)</div>
  </div>
  <div class="meta">github.com/mich-crypto/Venusosmods · generated ${date}</div>
</section>
<section class="toc"><h1>Contents</h1><ul>${tocHtml}</ul>
  <div class="note"><b>Before you start:</b> this is a do-it-yourself project, not a Victron product.
  Work on the 12 V system with the fuse removed, and keep drinking-water parts food-safe.
  The steps have been tested in simulation. Each part of the guide ends with a check,
  so confirm it on your own installation before moving on.</div>
</section>
${body}
</body></html>`;

const htmlPath = join(HERE, 'guide.html');
writeFileSync(htmlPath, html);

const browser = await chromium.launch();
const page = await browser.newPage();
await page.goto('file://' + htmlPath, { waitUntil: 'networkidle' });
await page.pdf({
  path: OUT,
  format: 'A4',
  printBackground: true,
  displayHeaderFooter: true,
  headerTemplate: '<div></div>',
  footerTemplate: `<div style="width:100%;font-family:Inter,sans-serif;font-size:7.5pt;color:#8a93a3;
    padding:0 16mm;display:flex;justify-content:space-between">
    <span>Venus OS Mods – Installation Guide</span>
    <span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>`,
  margin: { top: '18mm', bottom: '20mm', left: '16mm', right: '16mm' },
  outline: true,
  tagged: true,
});
await browser.close();
console.log('wrote', OUT);

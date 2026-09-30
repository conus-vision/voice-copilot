// Smoke test for the panel in a real browser: CI starts `voice-copilot serve`
// and runs `node tests/smoke/panel.mjs http://127.0.0.1:8799/`.
//
// Loads the page, opens every tab, and fails on a script error, a failed
// request, a missing icon font, or a WebSocket that never connects. The
// Python tests cover the API; this is the part only a browser runs.
import { chromium } from "playwright";

const url = process.argv[2] || "http://127.0.0.1:8765/";
const problems = [];

async function waitForServer(deadline) {
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url);
      if (res.ok) return;
    } catch {}
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`no panel at ${url}`);
}

await waitForServer(Date.now() + 30_000);
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on("pageerror", (e) => problems.push(`script error: ${e}`));
  page.on("console", (m) => {
    if (m.type() === "error") problems.push(`console: ${m.text()}`);
  });
  page.on("requestfailed", (r) => problems.push(`request failed: ${r.url()} ${r.failure()?.errorText}`));
  page.on("response", (r) => {
    if (r.status() >= 400) problems.push(`HTTP ${r.status()}: ${r.url()}`);
  });
  let socketOpen = false;
  page.on("websocket", (ws) => {
    socketOpen = true;
    ws.on("close", () => (socketOpen = false));
  });

  await page.goto(url, { waitUntil: "networkidle" });
  for (const tab of await page.$$eval(".tab", (els) => els.map((el) => el.dataset.tab))) {
    await page.click(`.tab[data-tab="${tab}"]`);
    const shown = await page.$eval(`.panel[data-panel="${tab}"]`, (el) => el.classList.contains("active"));
    if (!shown) problems.push(`tab ${tab} did not show its panel`);
  }
  await page.click('.tab[data-tab="plugins"]');
  await page.waitForSelector("#plugins-list .cli-row", { timeout: 10_000 }).catch(() =>
    problems.push("the Plugins tab listed no integration"),
  );
  const iconFont = await page.evaluate(() => document.fonts.check('24px "Material Symbols Rounded"'));
  if (!iconFont) problems.push("icon font not loaded");
  if (!socketOpen) problems.push("the live WebSocket did not connect");
} finally {
  await browser.close();
}

if (problems.length) {
  console.error(problems.join("\n"));
  process.exit(1);
}
console.log(`panel OK: ${url}`);

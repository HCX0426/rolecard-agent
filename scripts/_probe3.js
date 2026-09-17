const { existsSync } = require("fs");
const BROWSER_CANDIDATES = [
  "C://Program Files//Google//Chrome//Application//chrome.exe",
  "C://Program Files (x86)//Google//Chrome//Application//chrome.exe",
].filter(Boolean);
function loadPlaywright() {
  try { return require("playwright-core"); } catch {
    return require(path.join(process.env.USERPROFILE || "", "uicheck", "node_modules", "playwright-core"));
  }
}
const path = require("path");
(async () => {
  const pw = loadPlaywright();
  const exe = BROWSER_CANDIDATES.find((p) => existsSync(p));
  const browser = await pw.chromium.launch({ executablePath: exe, headless: true });
  const page = await browser.newPage({ viewport: { width: 1380, height: 860 } });
  await page.goto("http://127.0.0.1:8000", { waitUntil: "networkidle", timeout: 60000 });
  await page.waitForTimeout(1500);
  await page.locator("text=我对话发了我").first().click();
  await page.waitForTimeout(2500);
  const info = await page.evaluate(() => {
    const els = [...document.querySelectorAll("div")].filter(
      (d) => String(d.className).includes("bg-blue-600") && d.textContent.includes("1111111111")
    );
    const el = els[0];
    const chain = [];
    let node = el;
    for (let i = 0; i < 6 && node && node !== document.body; i++) {
      const s = getComputedStyle(node);
      chain.push({
        cls: String(node.className).slice(0, 60) || "(none)",
        display: s.display,
        width: node.getBoundingClientRect().width.toFixed(0),
        flex: s.flex,
        minW: s.minWidth,
      });
      node = node.parentElement;
    }
    return chain;
  });
  console.log(JSON.stringify(info, null, 1));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });

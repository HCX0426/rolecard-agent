const { existsSync } = require("fs");
const BROWSER_CANDIDATES = [
  "C://Program Files//Google//Chrome//Application//chrome.exe",
  "C://Program Files (x86)//Google//Chrome//Application//chrome.exe",
  "C://Program Files (x86)//Microsoft//Edge//Application//msedge.exe",
  "C://Program Files//Microsoft//Edge//Application//msedge.exe",
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
  await page.locator("text=用一句话介绍你自己").first().click();
  await page.waitForTimeout(2000);
  await page.screenshot({ path: "_shot_chat_ts.png" });
  console.log("done");
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });

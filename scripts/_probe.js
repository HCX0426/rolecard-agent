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
  await page.locator("text=我对话发了我").first().click();
  await page.waitForTimeout(2000);

  const info = await page.evaluate(() => {
    // 找到含目标文本的气泡（蓝色用户气泡）
    const els = [...document.querySelectorAll("div")].filter(
      (d) => d.className && String(d.className).includes("bg-blue-600") && d.textContent.includes("1111111111")
    );
    if (!els.length) return { found: false };
    const el = els[0];
    const cs = getComputedStyle(el);
    const chain = [];
    let node = el;
    for (let i = 0; i < 6 && node && node !== document.body; i++) {
      const s = getComputedStyle(node);
      chain.push({
        tag: node.tagName,
        cls: String(node.className).slice(0, 70),
        width: node.getBoundingClientRect().width.toFixed(0),
        whiteSpace: s.whiteSpace,
        wordBreak: s.wordBreak,
        overflowWrap: s.overflowWrap,
        writingMode: s.writingMode,
        direction: s.direction,
      });
      node = node.parentElement;
    }
    return {
      found: true,
      bubbleWidth: el.getBoundingClientRect().width.toFixed(0),
      contentChars: [...el.childNodes].map((n) => (n.nodeType === 3 ? JSON.stringify(n.textContent) : "<el>")),
      chain,
    };
  });
  console.log(JSON.stringify(info, null, 1));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });

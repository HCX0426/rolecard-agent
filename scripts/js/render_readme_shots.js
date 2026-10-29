#!/usr/bin/env node
/**
 * README 六页签截图：把控制台六个页签在真 Chrome 里各拍一张，落进 build/readme-shots/。
 *
 * 为什么要有它：`docs/assets/console-*.png` 是 README 首页那六张图，2026-09-15 拍的那版
 * 已多处与代码脱节（设置页子页签 3→7、灰字文案已删），而门禁全是文本尺子、扫不到像素
 * （B4）。重拍的取景口径要固定下来，否则下一次重拍又是一次性脚本。
 *
 * 用法（**先自己起一个隔离后端**，别打用户的 :8000；规矩同 `scripts/js/render_shots.js`）：
 *   node scripts/js/render_readme_shots.js [base_url] [--out build/readme-shots]
 *
 * 浏览器/驱动缺任何一样：打印跳过并 exit 0（环境差异不该变红，与 ui_smoke 同一口径）。
 */

const { existsSync, mkdirSync } = require("fs");
const path = require("path");

const argv = process.argv.slice(2);
const BASE = (argv[0] && !argv[0].startsWith("--") ? argv[0] : process.env.UI_SMOKE_BASE) ||
  "http://127.0.0.1:8000";
const outIdx = argv.indexOf("--out");
const OUT = outIdx >= 0 && argv[outIdx + 1] ? argv[outIdx + 1] : "build/readme-shots";

const BROWSERS = [
  process.env.UI_SMOKE_BROWSER,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);

function loadPlaywright() {
  try {
    return require("playwright-core");
  } catch {
    try {
      return require(path.join(process.env.USERPROFILE || "", "uicheck", "node_modules", "playwright-core"));
    } catch {
      return null;
    }
  }
}

// 与 README 里两张图的行列一一对应；`console-*.png` 的文件名就是这个 key。
// 2026-10-30 IA：知识库并入「资料」区块、插件更名「工具」、新增顶层「记忆」。
const PAGES = [
  ["chat", "对话页"],
  ["data", "资料页"],
  ["roles", "角色卡页"],
  ["memory", "记忆页"],
  ["tools", "工具页"],
  ["settings", "设置页"],
];

// 旧截图是 1410×805 —— 同一口径重拍，README 里两列布局才不会跳。
const VIEWPORT = { width: 1410, height: 805 };

(async () => {
  const browserPath = BROWSERS.find((p) => existsSync(p));
  const pw = loadPlaywright();
  if (!pw || !browserPath) {
    console.log("SKIP README 截图：缺 playwright-core 或本机浏览器（不算失败）");
    process.exit(0);
  }
  const health = await fetch(`${BASE}/api/health`).then((r) => (r.ok ? r.json() : null)).catch(() => null);
  if (!health) {
    console.log(`SKIP README 截图：${BASE} 没在答 —— 先起一个隔离实例，别打用户的 :8000`);
    process.exit(0);
  }
  mkdirSync(OUT, { recursive: true });

  const browser = await pw.chromium.launch({ executablePath: browserPath, headless: true });
  const page = await browser.newPage({ viewport: VIEWPORT });
  let failed = 0;
  try {
    for (const [key, label] of PAGES) {
      await page.goto(`${BASE}/#/${key}`, { waitUntil: "networkidle", timeout: 60000 });
      // networkidle 只保证请求落定，不保证 React 已把数据画出来 —— 再等一拍首屏元素。
      await page.waitForTimeout(1500);
      const name = `console-${key}.png`;
      try {
        await page.screenshot({ path: path.join(OUT, name), fullPage: false });
        console.log(`✅ ${name} — ${label}`);
      } catch (e) {
        failed += 1;
        console.log(`❌ ${name}（${label}）截图失败：${e.message}`);
      }
    }
  } catch (e) {
    failed += 1;
    console.log(`❌ 取景过程抛了：${e.message}`);
  } finally {
    await browser.close();
  }
  console.log(failed === 0 ? `图都在 ${OUT}/` : `${failed} 项没成`);
  process.exit(failed === 0 ? 0 : 1);
})().catch((e) => {
  console.error("README 截图异常:", String(e).slice(0, 300));
  process.exit(1);
});

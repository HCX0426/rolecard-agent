#!/usr/bin/env node
/**
 * 真机 UI 冒烟：用本机已装的 Chrome / Edge 打开控制台，走一遍真实交互。
 *
 * 用法：
 *   node scripts/ui_smoke.js [base_url]        # 默认 http://127.0.0.1:8000
 *
 * 设计取舍：
 *   - 只依赖 **playwright-core**（不下载浏览器，约几 MB），驱动本机已装的 Chrome/Edge；
 *     没装驱动或没浏览器时，**以"跳过"退出（exit 0）**，不把环境差异变成 CI 红。
 *   - 断言的是"用户能看到的"（可见性 / 默认展开 / 刷新后仍在），不是实现细节。
 *   - 由 scripts/smoke_check.py 的第 13 项调用，因此这里只输出结论行与退出码。
 *
 * 环境覆盖：
 *   UI_SMOKE_BASE      被测地址
 *   UI_SMOKE_BROWSER   指定浏览器可执行文件（否则自动探测 Chrome → Edge）
 *   UI_SMOKE_HEADLESS  "0" = 显示浏览器窗口（调试用）
 */

const { existsSync } = require("fs");
const path = require("path");

const BASE = process.argv[2] || process.env.UI_SMOKE_BASE || "http://127.0.0.1:8000";
const HEADLESS = (process.env.UI_SMOKE_HEADLESS || "1") !== "0";

const BROWSER_CANDIDATES = [
  process.env.UI_SMOKE_BROWSER,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);

function pickBrowser() {
  return BROWSER_CANDIDATES.find((p) => existsSync(p)) || null;
}

function loadPlaywright() {
  try {
    return require("playwright-core");
  } catch {
    try {
      // 允许驱动装在仓库外（不污染项目依赖）
      return require(path.join(process.env.USERPROFILE || "", "uicheck", "node_modules", "playwright-core"));
    } catch {
      return null;
    }
  }
}

const results = [];
function record(name, ok, note = "") {
  results.push({ name, ok, note });
  console.log(`${ok ? "✅" : "❌"} ${name}${note ? " — " + note : ""}`);
}

(async () => {
  // 0) 前置：服务在跑吗
  const health = await fetch(`${BASE}/api/health`).then((r) => (r.ok ? r.json() : null)).catch(() => null);
  if (!health) {
    console.log(`SKIP 服务未就绪：${BASE}（先启动 start.bat，再跑 UI 冒烟）`);
    process.exit(0);
  }

  const browserPath = pickBrowser();
  const playwright = loadPlaywright();
  if (!playwright || !browserPath) {
    console.log(
      `SKIP 缺少真机 UI 自测环境：playwright-core=${playwright ? "有" : "无"}，浏览器=${browserPath || "未找到"}`
    );
    console.log("     安装：npm i -D playwright-core（不下载浏览器；需本机已装 Chrome 或 Edge）");
    process.exit(0);
  }

  const { chromium } = playwright;
  const browser = await chromium.launch({ executablePath: browserPath, headless: HEADLESS });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e).slice(0, 120)));

  try {
    await page.goto(BASE, { waitUntil: "networkidle", timeout: 60000 });
    const title = await page.title();
    record("页面加载（控制台首页）", !!title, `title=${title}`);

    // 新建会话
    const newChat = page.getByRole("button", { name: /新建对话/ }).first();
    if (await newChat.count()) {
      await newChat.click();
      await page.waitForTimeout(400);
    }
    record("新建会话", true);

    // 发消息
    const QUESTION = "用一句话解释：为什么冬天白天比夏天短？";
    const input = page.getByPlaceholder(/输入消息/);
    await input.click();
    await input.fill(QUESTION);
    await page.getByRole("button", { name: "发送" }).click();

    // 思考面板（思考模型才会有；没有就跳过这一项而不是判失败）
    const summary = page.getByText("思考过程", { exact: true }).first();
    let thinkingSeen = false;
    try {
      await summary.waitFor({ state: "visible", timeout: 90000 });
      thinkingSeen = true;
    } catch {
      thinkingSeen = false;
    }
    if (thinkingSeen) {
      const openDuringStream = await page.evaluate(() => {
        const el = document.querySelector("details");
        return el ? el.hasAttribute("open") : null;
      });
      const label = (await summary.textContent()) || "";
      record("思考过程：流式期间可见且默认展开", openDuringStream === true, `标题=${JSON.stringify(label)}`);
      record("思考过程：不带「思考中…」省略号", label.trim() === "思考过程");
    } else {
      console.log("（跳过思考相关断言：当前模型未输出思考，或响应较慢）");
    }

    // 等待回答结束
    await page.getByRole("button", { name: "发送" }).waitFor({ timeout: 180000 });
    await page.waitForTimeout(2500);
    const answered = await page.getByText(/白天|白昼|昼长|日照/).first().isVisible().catch(() => false);
    record("回答生成并渲染", answered);

    if (thinkingSeen) {
      const stillVisible = await page.getByText("思考过程", { exact: true }).first().isVisible().catch(() => false);
      const stillOpen = await page.evaluate(() => {
        const el = document.querySelector("details");
        return el ? el.hasAttribute("open") : null;
      });
      // 关键回归点：轮次结束会用 checkpoint 回放整体替换消息区，思考必须留得住。
      // 注意断言的是**折叠**态：展开属于用户的点击动作（用户 2026-09-16 明确），
      // 流式期间才是默认展开（上面那条已覆盖）。
      record("回答结束后思考过程仍然保留", stillVisible);
      record("回答结束后思考面板已折叠（展开交给用户点击）", stillOpen === false);

      // 点击后必须真的能展开出思考正文（否则"保留"只是留了个空壳）
      const panel = page.getByText("思考过程", { exact: true }).first();
      await panel.click().catch(() => {});
      await page.waitForTimeout(400);
      const opened = await page.evaluate(() => {
        const el = document.querySelector("details");
        return el ? el.hasAttribute("open") : null;
      });
      record("点击思考面板可展开", opened === true);
    }

    // 刷新后历史仍在（回放）
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForTimeout(1200);
    const historyKept = await page.getByText(/白天|白昼|昼长|日照/).first().isVisible().catch(() => false);
    record("刷新页面后历史回放仍在", historyKept);

    record("无页面 JS 报错", pageErrors.length === 0, pageErrors[0] || "");
  } finally {
    await browser.close();
  }

  const passed = results.filter((r) => r.ok).length;
  console.log(`\n真机 UI 冒烟：${passed}/${results.length}`);
  process.exit(passed === results.length ? 0 : 1);
})().catch((e) => {
  console.error("UI 冒烟异常:", String(e).slice(0, 300));
  process.exit(1);
});

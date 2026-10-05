#!/usr/bin/env node
/**
 * 观收取景：把"给人看的那几格"在真 Chrome 里渲染成 PNG，落进 build/shots/。
 *
 * 为什么要有这个脚本：台账里连着三次记了同一句"截图取不到，观感没目视过"——
 * in-app browser 没有可见表面（`visibilityState=hidden`），而配色、换行、字号这类
 * 判断只有看图才作数。本机有 Chrome，`scripts/ui_smoke.js` 那条路已经证明能驱动它，
 * 所以"看不了图"其实只是"没人写取景的那二十行"。
 *
 * 用法（**先自己起一个后端**，别打用户的 :8000；规矩同 `scripts/probe_cross_window_sync.py`）：
 *   node scripts/render_shots.js --base http://127.0.0.1:<port> [--role elysia] [--out build/shots]
 *
 * 拍四张：
 *   1 quiet-settings.png   运行环境页「主动开口」那一组下面的静默行
 *   2 quiet-drawer.png     收件箱抽屉头部那一行（跟着角色筛选的那一份）
 *   3 inflight-mirror.png  **另一路在生成时**，对话页里那格镜像气泡
 *   4 settled.png          同一处，那一轮落地之后（该只剩真消息，不留重影）
 *
 * 第 3、4 张的"另一路"是本脚本自己 POST /api/chat 并读完流 —— 页面不参与发送，
 * 于是它读到 `/turn` 那份在飞登记时画的就是镜像气泡，与"用户在桌宠上发问"同一条码路。
 * 浏览器/驱动缺任何一样：打印跳过并 exit 0（环境差异不该变红，与 ui_smoke 同一口径）。
 */

const { existsSync, mkdirSync } = require("fs");
const path = require("path");

const argv = process.argv.slice(2);
function flag(name, dflt) {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt;
}

const BASE = flag("base", "http://127.0.0.1:8000").replace(/\/$/, "");
const ROLE = flag("role", "爱莉希雅");
const OUT = flag("out", "build/shots");
const HEADLESS = flag("headless", "1") !== "0";

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

const log = (...a) => console.log(...a);

(async () => {
  const browserPath = BROWSERS.find((p) => existsSync(p));
  const pw = loadPlaywright();
  if (!pw || !browserPath) {
    log("SKIP 观收取景：缺 playwright-core 或本机浏览器（不算失败）");
    process.exit(0);
  }
  const health = await fetch(`${BASE}/api/health`).then((r) => (r.ok ? r.json() : null)).catch(() => null);
  if (!health) {
    log(`SKIP 观收取景：${BASE} 没在答 —— 先起一个隔离实例，别打用户的 :8000`);
    process.exit(0);
  }
  mkdirSync(OUT, { recursive: true });

  const browser = await pw.chromium.launch({ executablePath: browserPath, headless: HEADLESS });
  const page = await browser.newPage({ viewport: { width: 1240, height: 860 } });
  let failed = 0;
  const shot = async (name, note = "") => {
    try {
      await page.screenshot({ path: path.join(OUT, name), fullPage: false });
      log(`✅ ${name}${note ? " — " + note : ""}`);
    } catch (e) {
      failed += 1;
      log(`❌ ${name} 截图失败：${e.message}`);
    }
  };

  try {
    // ---- 1) 运行环境页的静默行 ------------------------------------------------
    await page.goto(`${BASE}/#/settings`, { waitUntil: "networkidle" });
    const envTab = page.getByRole("button", { name: /运行环境/ }).first();
    if (await envTab.count()) await envTab.click();
    await page.waitForTimeout(1200);
    const inSettings = page.locator('[data-testid="quiet-status"] >> visible=true').first();
    if (await inSettings.count()) {
      await inSettings.screenshot({ path: path.join(OUT, "quiet-settings.png") });
      log(`✅ quiet-settings.png — ${(await inSettings.innerText()).replace(/\s+/g, " ").slice(0, 70)}`);
    } else {
      failed += 1;
      log("❌ 运行环境页没找到可见的静默那一格");
    }

    // ---- 2) 收件箱抽屉头部那一行 ----------------------------------------------
    await page.goto(`${BASE}/#/chat`, { waitUntil: "networkidle" });
    const bell = page.getByRole("button", { name: /主动消息/ }).first();
    if (await bell.count()) {
      await bell.click();
      await page.waitForTimeout(800);
      const drawer = page.locator('[data-testid="quiet-status"] >> visible=true').last();
      if (await drawer.count()) {
        await drawer.screenshot({ path: path.join(OUT, "quiet-drawer.png") });
        log(`✅ quiet-drawer.png — ${(await drawer.innerText()).replace(/\s+/g, " ").slice(0, 70)}`);
      } else {
        failed += 1;
        log("❌ 抽屉里没有可见的静默那一格");
      }
      // 抽屉的关闭挂在遮罩的 **mousedown** 上（`ReachoutPanel.tsx` 那句 `onMouseDown={onClose}`），
      // 键盘 Escape 它根本没接 —— 按 Escape 会让遮罩留在原地，把后面侧栏那一次点击吃掉。
      const overlay = page.locator("div.fixed.inset-0.z-50").first();
      if (await overlay.count()) {
        await overlay.dispatchEvent("mousedown");
        await page.waitForTimeout(500);
      }
    } else {
      failed += 1;
      log("❌ 头部没有「主动消息」那个按钮");
    }

    // ---- 3) 另一路在生成时，对话页里的镜像气泡 --------------------------------
    const row = page.locator(`text=${ROLE} · 主动找你`).first();
    if (!(await row.count())) {
      failed += 1;
      log(`❌ 侧栏里没有「${ROLE} · 主动找你」那一条`);
    } else {
      await row.click();
      await page.waitForTimeout(1000);
      // 线程 id 由侧栏那一行的跳转决定，这里按前缀现取一条 her 的主动会话
      const sessions = await fetch(`${BASE}/api/sessions`).then((r) => r.json());
      const lane = sessions.find((s) => s.is_proactive && s.role_name === ROLE) || sessions.find((s) => s.is_proactive);
      const threadId = lane.thread_id;
      const prompt = "给我讲讲你今天想做的事，写长一点，两三百字，分几段。";
      // 页面不参与发送：本脚本自己读完这条 SSE，于是它是"第二个读者"
      page.evaluate(async ({ base, tid, msg }) => {
        const res = await fetch(`${base}/api/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ thread_id: tid, message: msg }),
        });
        const reader = res.body.getReader();
        for (;;) {
          const { done } = await reader.read();
          if (done) break;
        }
      }, { base: BASE, tid: threadId, msg: prompt }).catch(() => {});

      const bubble = page.locator('[data-testid="inflight-mirror"] >> visible=true').first();
      let saw = false;
      for (let i = 0; i < 40 && !saw; i += 1) {
        saw = (await bubble.count()) > 0;
        if (!saw) await page.waitForTimeout(250);
      }
      if (saw) {
        const text = (await bubble.innerText()).replace(/\s+/g, " ");
        const caption = "正在生成 · 不是这一扇窗发的";
        await page.locator("main").first().screenshot({ path: path.join(OUT, "inflight-mirror.png") });
        log(`✅ inflight-mirror.png — 刚起手 ${text.length - caption.length} 字正文：${text.slice(0, 44)}`);
        // 再等它涨出一截才拍第二张：要看的是"半句怎么换行、markdown 有没有渲染"，
        // 空的那一格看不出这两件事。
        let grown = text.length - caption.length;
        for (let i = 0; i < 60 && grown < 60; i += 1) {
          await page.waitForTimeout(500);
          grown = ((await bubble.innerText().catch(() => "")) || "").replace(/\s+/g, " ").length - caption.length;
        }
        await page.locator("main").first().screenshot({ path: path.join(OUT, "inflight-text.png") });
        log(`✅ inflight-text.png — 涨到 ${grown} 字时的那一格`);
      } else {
        failed += 1;
        log("❌ 一整轮里都没等到镜像气泡（那一格没画出来）");
      }

      // ---- 4) 落地之后：气泡该收掉，真消息该在场 ------------------------------
      // 必须等**服务端说她不再生成**再拍，而且要把"读失败"和"她不再生成"分开：
      // 上一版 `.catch(() => null)` 让一次请求失败直接等于"落地了"，于是拍到半句、
      // 报出一个根本不存在的重影 bug —— 抢拍的截图会造出不存在的缺陷，和抢拍的读数一样坏。
      let sawInflight = false;
      for (let i = 0; i < 240; i += 1) {
        let inf;
        try {
          inf = await fetch(`${BASE}/api/session/${threadId}/turn`).then((r) => r.json()).then((p) => p.inflight);
        } catch {
          await page.waitForTimeout(400);
          continue; // 读失败：不算数
        }
        if (inf) sawInflight = true;
        else if (sawInflight) break; // 真的落地了
        await page.waitForTimeout(400);
      }
      // 界面那一侧允许最多 2 拍（0.8s × 2）才收掉；收不掉才是重影
      let cleared = false;
      for (let i = 0; i < 12 && !cleared; i += 1) {
        cleared = (await page.locator('[data-testid="inflight-mirror"] >> visible=true').count()) === 0;
        if (!cleared) await page.waitForTimeout(500);
      }
      await page.locator("main").first().screenshot({ path: path.join(OUT, "settled.png") });
      log(`${cleared ? "✅" : "❌"} settled.png — 落地后镜像气泡${cleared ? "已收掉" : "还挂着（重影）"}`);
      if (!cleared) failed += 1;
    }
  } catch (e) {
    failed += 1;
    log(`❌ 取景过程抛了：${e.message}`);
  } finally {
    await browser.close();
  }
  log(failed === 0 ? `图都在 ${OUT}/` : `${failed} 项没成，仍已尽量落图在 ${OUT}/`);
  process.exit(failed === 0 ? 0 : 1);
})();

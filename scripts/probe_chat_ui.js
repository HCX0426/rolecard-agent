#!/usr/bin/env node
/**
 * 对话页拆分（ChatPage → 5 个接缝）的真机交互探针。
 *
 * 只跑 `ui_smoke.js` 够不着的那几条路径：侧栏固定栏、角色抽屉、模型抽屉（含采样与
 * 上下文两个 inline 面板）、删除模式、编辑重答、批量清理。这些正是这次搬动的东西，
 * 而 jsdom 那 36 条用例全绿不代表真浏览器上对（本仓已经为此撞出过两个真 bug）。
 *
 * 用法：node scripts/probe_chat_ui.js <base_url>   （后端由 scripts/probe_chat_ui.py 自己起）
 * 只读优先：不发消息、不调模型；点采样档位那次 PATCH 落在库副本上。
 */
const { existsSync } = require("fs");
const path = require("path");

const BASE = process.argv[2] || "http://127.0.0.1:8000";

function pickBrowser() {
  return [
    process.env.UI_SMOKE_BROWSER,
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  ].filter(Boolean).find((p) => existsSync(p)) || null;
}

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

const results = [];
function record(name, ok, note = "") {
  results.push({ name, ok, note });
  console.log(`${ok ? "✅" : "❌"} ${name}${note ? " — " + note : ""}`);
}

(async () => {
  const health = await fetch(`${BASE}/api/health`).then((r) => (r.ok ? r.json() : null)).catch(() => null);
  if (!health) {
    console.log(`FAIL 后端未就绪：${BASE}`);
    process.exit(1);
  }
  const playwright = loadPlaywright();
  const browserPath = pickBrowser();
  if (!playwright || !browserPath) {
    console.log(`FAIL 缺真机环境：playwright-core=${playwright ? "有" : "无"} 浏览器=${browserPath || "无"}`);
    process.exit(1);
  }

  const { chromium } = playwright;
  const browser = await chromium.launch({ executablePath: browserPath, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e).slice(0, 160)));
  // 前端到底收到什么样的消息行？直接抓响应体，不靠推断
  let rowsShape = null;
  page.on("response", async (res) => {
    if (/\/api\/session\/[^/]+\/messages$/.test(res.url())) {
      try {
        const j = await res.json();
        if (Array.isArray(j.messages)) {
          rowsShape = j.messages.slice(-3).map((m) => ({
            role: m.role,
            id: m.id ?? null,
            keys: Object.keys(m).join("|"),
          }));
        }
      } catch {
        /* 非 JSON 就跳过 */
      }
    }
  });

  try {
    await page.goto(BASE, { waitUntil: "networkidle", timeout: 60000 });

    // 1) 侧栏固定栏：一行一个角色（这些行的可点名字在 title 上，文本只是角色名）
    const lanes = page.getByTitle(/的那条对话 —— 桌宠显示的就是这一条/);
    const laneCount = await lanes.count();
    record("侧栏「她们」一栏按角色成行", laneCount > 0, `${laneCount} 行`);

    // 要一条**有历史**的会话才好验后面几项：逐行点侧栏（固定行与临时话题行共用同一套
    // 行样式），直到画上「复制」按钮为止。
    const rows = page.locator("aside div.group.mb-1");
    const rowCount = await rows.count();
    let copyBtns = 0;
    let opened = -1;
    for (let i = 0; i < rowCount; i += 1) {
      await rows.nth(i).click();
      await page.getByPlaceholder(/输入消息/).waitFor({ state: "visible", timeout: 10000 });
      // 等历史真的画上（点行之后 selectSession 还在飞；只等输入框可见会读到半张空页）
      await page
        .waitForFunction(() => !!document.querySelector("div.group.relative.w-full"), undefined, { timeout: 8000 })
        .catch(() => {});
      copyBtns = await page.getByRole("button", { name: "复制" }).count();
      if (copyBtns > 0) {
        opened = i;
        break;
      }
    }
    record("点侧栏一行 → 走普通载入路径把历史画上（每答一条给一个「复制」）", copyBtns > 0, `侧栏 ${rowCount} 行，第 ${opened + 1} 行有历史，复制按钮=${copyBtns}`);
    console.log(`   真实消息行的形状：${JSON.stringify(rowsShape)}`);

    // 2) 角色抽屉：开 → 说明行在 → Esc 关（键盘退路是 useMenus 的 closeAllMenus）
    await page.getByTitle(/换个说话的人/).click();
    const drawerNote = await page.getByText(/选角色 = 进她那条对话/).first().isVisible().catch(() => false);
    record("角色抽屉打开并带那句语义说明", drawerNote);
    const roleItems = await page.getByText(/进她那条对话（原来那条留在左侧）/).count();
    await page.keyboard.press("Escape");
    const closedAfterEsc = !(await page.getByText(/选角色 = 进她那条对话/).first().isVisible().catch(() => false));
    record("Esc 能关掉角色抽屉（菜单靠鼠标移出关闭时键盘必须有退路）", closedAfterEsc, `抽屉=${roleItems} 个`);

    // 3) 模型抽屉：开 → 「默认后端」在 → 采样徽章点开 → 三栏里至少出现两栏
    await page.getByTitle(/切换本对话使用的模型/).click();
    const defaultRow = await page.getByText("默认后端（跟随设置）").first().isVisible().catch(() => false);
    record("模型抽屉列出「默认后端（跟随设置）」", defaultRow);
    const sampBadge = page.getByTitle(/^采样惩罚/).first();
    const hasSamp = await sampBadge.isVisible().catch(() => false);
    record("每个后端行都有「采样 ▾」徽章（两类客户端都有这一栏）", hasSamp);
    if (hasSamp) {
      await sampBadge.click();
      const freq = await page.getByText("频率惩罚", { exact: true }).first().isVisible().catch(() => false);
      const repeat = await page.getByText("重复惩罚", { exact: true }).first().isVisible().catch(() => false);
      record("采样面板里出现「频率惩罚」那一栏", freq);
      // 云端行不给重复惩罚：本地行才会出现。这一条钉的是 nativeOnly 那道筛。
      console.log(`   （本行是否出现「重复惩罚」= ${repeat}；只有 style=native 的行该有）`);
      await page.getByText("未设置", { exact: true }).first().click().catch(() => {});
      const stayedOpen = await page.getByText("频率惩罚", { exact: true }).first().isVisible().catch(() => false);
      record("点一档「未设置」之后面板不塌（PATCH 走的是就地更新，不该收起整张抽屉）", stayedOpen);
      await sampBadge.click().catch(() => {});
    }
    // 上下文徽章只对本地行出现
    const ctxBadge = page.getByTitle(/设置该模型的上下文窗口/).first();
    const hasCtx = await ctxBadge.isVisible().catch(() => false);
    if (hasCtx) {
      await ctxBadge.click();
      const eng = await page.getByText("引擎默认", { exact: true }).first().isVisible().catch(() => false);
      record("本地行的「上下文 ▾」点开有档位（含引擎默认）", eng);
      await ctxBadge.click().catch(() => {});
    } else {
      console.log("   （跳过上下文断言：这份副本里没有 style=native 的后端）");
    }

    // 4) 删除模式：进入 → 说明条与轮前复选框在 → 退出
    const diag = await page.evaluate(() => ({
      delButtons: [...document.querySelectorAll("button")]
        .map((b) => (b.textContent || "").trim())
        .filter((t) => /删除/.test(t)),
      pencilsInDom: document.querySelectorAll('[aria-label="编辑并重答"]').length,
      turnCheckboxes: document.querySelectorAll('input[aria-label^="选择这一轮"]').length,
      sidebarChecks: document.querySelectorAll('aside input[type="checkbox"]').length,
      confirmOpen: (document.querySelector("[data-confirm-root]")?.textContent || "").trim().length,
    }));
    console.log(`   诊断：${JSON.stringify(diag)}`);
    console.log(
      `   第一轮 HTML：${await page.evaluate(() => {
        const el = document.querySelector("div.group.relative.w-full");
        return el ? el.outerHTML.slice(0, 700) : "（没有匹配 div.group.relative.w-full 的轮）";
      })}`,
    );
    const delBtn = page.getByRole("button", { name: "删除对话", exact: true }).first();
    if (await delBtn.isEnabled().catch(() => false)) {
      await delBtn.click();
      const hint = await page.getByText(/删除模式：勾选任意一问或一答/).first().isVisible().catch(() => false);
      const boxes = await page.locator('input[aria-label^="选择这一轮"]').count();
      record("进入删除模式：出现说明条并按轮画复选框", hint && boxes > 0, `说明条=${hint} 复选框=${boxes}`);
      const firstBox = page.locator('input[aria-label^="选择这一轮"]').first();
      let boxClickable = true;
      try {
        await firstBox.check({ timeout: 4000 });
      } catch {
        boxClickable = false;
        await firstBox.check({ force: true }).catch(() => {});
      }
      // 复选框在 -left-7、悬浮铅笔在 -left-9，两个绝对定位本来就叠在同一片像素上；
      // 铅笔是 opacity-0 但仍吃点击 → 后画的它盖住了先画的复选框。
      record("删除模式下轮前复选框点得到（不被悬浮铅笔 intercept）", boxClickable);
      const bar = await page.getByText(/条消息（勾选一侧会带上配对的问答）/).first().isVisible().catch(() => false);
      record("勾上一条 → 确认条报出整轮计数", bar);
      await page.getByRole("button", { name: "退出选择" }).click().catch(() => {});
      await page.getByRole("button", { name: /退出删除模式/ }).click().catch(() => {});
      const gone = !(await page.getByText(/删除模式：勾选任意一问或一答/).first().isVisible().catch(() => false));
      record("退出删除模式后说明条收掉", gone);
    } else {
      record("删除模式按钮在位且非禁用（没会话时该禁用，不能是死按钮）", false, "按钮不可用：当前没有会话");
    }

    // 5) 编辑并重答：铅笔 → 就地编辑器出现 → Esc 撤掉
    const pencilCount = await page.locator('[aria-label="编辑并重答"]').count();
    const pencil = page.getByRole("button", { name: "编辑并重答", exact: true }).first();
    if (pencilCount > 0 && (await pencil.isVisible().catch(() => false))) {
      await pencil.click();
      const saveBtn = await page.getByRole("button", { name: "保存并重新生成" }).first().isVisible().catch(() => false);
      record("铅笔进就地编辑（带「保存并重新生成」）", saveBtn);
      await page.keyboard.press("Escape");
      const afterEsc = !(await page.getByRole("button", { name: "保存并重新生成" }).first().isVisible().catch(() => false));
      record("Esc 撤销就地编辑", afterEsc);
    } else {
      record("历史里有可编辑的用户消息（铅笔在位）", false, `DOM 里铅笔=${pencilCount} 条`);
    }

    // 6) 批量清理：只开合，不真删
    const batch = page.getByRole("button", { name: "批量清理" }).first();
    if (await batch.isVisible().catch(() => false)) {
      await batch.click();
      const selectAll = await page.getByRole("button", { name: "全选" }).first().isVisible().catch(() => false);
      record("临时话题「批量清理」进入后给全选与退出", selectAll);
      if (selectAll) await page.getByRole("button", { name: "全选" }).first().click();
      const delCount = page.getByRole("button", { name: /^删除 \d+$/ }).first();
      const n = await delCount.isVisible().catch(() => false);
      record("全选后「删除 N」按钮带出条数（没选中时是禁用的）", n);
      await page.getByRole("button", { name: "退出" }).first().click().catch(() => {});
    } else {
      console.log("   （跳过批量清理：这份副本里没有临时话题，本来就该没有这个入口）");
    }

    // 7) 输入框自适应：多行长高、清空缩回
    const ta = page.getByPlaceholder(/输入消息/);
    await ta.click();
    const h1 = await ta.evaluate((el) => el.offsetHeight);
    await ta.fill("一二三\n二三四\n三四五\n四五五\n五六六");
    await page.waitForTimeout(150);
    const h2 = await ta.evaluate((el) => el.offsetHeight);
    record("输入框随内容长高", h2 > h1, `${h1}px → ${h2}px`);
    await ta.fill("");

    record("全程无页面 JS 报错", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  } catch (e) {
    record("探针跑完（未中途抛出）", false, String(e).slice(0, 200));
  } finally {
    await browser.close();
  }

  const passed = results.filter((r) => r.ok).length;
  console.log(`\n对话页交互探针：${passed}/${results.length}`);
  process.exit(passed === results.length ? 0 : 1);
})();

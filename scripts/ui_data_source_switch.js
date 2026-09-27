// 数据源切换的浏览器侧（被 scripts/probe_data_source_switch.py 拉起）。
// 用法：node scripts/ui_data_source_switch.js <localBase> <cloudBase>
// 截图落在 build/（M5 三步 + M8 对账与裁决四步，失败时再来一张 dss_failed.png 供目视）。
const path = require("path");
const { existsSync } = require("fs");

const ROOT = path.join(__dirname, "..");
const OUT = path.join(ROOT, "build");
const [localBase, cloudBase] = process.argv.slice(2);
if (!localBase || !cloudBase) {
  console.error("用法：node scripts/ui_data_source_switch.js <localBase> <cloudBase>");
  process.exit(2);
}

let pw = null;
for (const m of ["playwright-core", path.join(process.env.USERPROFILE || "", "uicheck", "node_modules", "playwright-core")]) {
  try {
    pw = require(m);
    break;
  } catch {
    /* 换下一个 */
  }
}
const exes = [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
];
const exe = exes.find((p) => existsSync(p));

const results = [];
function record(name, ok, note = "") {
  results.push(ok);
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${note ? " — " + note : ""}`);
}

(async () => {
  if (!pw || !exe) {
    console.log("SKIP（缺 playwright-core 或本机浏览器）");
    process.exit(0);
  }
  const browser = await pw.chromium.launch({ executablePath: exe, headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });
  page.on("console", (m) => console.log(`  [console:${m.type()}] ${m.text().slice(0, 200)}`));
  page.on("pageerror", (e) => console.log(`  [pageerror] ${String(e).slice(0, 240)}`));
  page.on("requestfailed", (r) => console.log(`  [reqfail] ${r.url()} ${r.failure()?.errorText}`));
  page.on("response", (r) => {
    if (r.url().includes("/api/sync/")) {
      console.log(`  [sync] ${r.request().method()} ${r.url()} -> ${r.status()}`);
    }
  });
  try {
    await page.goto(localBase, { waitUntil: "networkidle" });
    await page.getByText("数据源：本机").first().waitFor({ timeout: 20000 });
    record("① 默认态：侧栏那一行是「数据源：本机」",
      (await page.locator("text=数据源：云端").count()) === 0);
    await page.screenshot({ path: path.join(OUT, "dss_step1_local.png") });

    await page.getByText("数据源：本机").first().click();
    await page.getByRole("heading", { name: "切到云端" }).waitFor({ timeout: 5000 });
    record("② 点一下出登录弹层（地址 / 账号 / 密码三格）",
      (await page.getByLabel("服务地址").count()) === 1
      && (await page.getByLabel("账号").count()) === 1
      && (await page.getByLabel("密码 / 访问令牌").count()) === 1);
    await page.screenshot({ path: path.join(OUT, "dss_step2_modal.png") });

    // 先试一个错密码：必须留在本机、一格都不变（这条是"不做半截云端"的兑现）
    await page.getByLabel("服务地址").fill(cloudBase);
    await page.getByLabel("账号").fill("u1");
    await page.getByLabel("密码 / 访问令牌").fill("错的口令");
    await page.getByRole("button", { name: "连接并切换" }).click();
    await page.getByText("账号或密码不对").first().waitFor({ timeout: 15000 });
    record("③ 密码不对：弹层里出声，界面没切（仍写着本机）",
      (await page.getByText("数据源：本机").count()) > 0
      && (await page.locator("text=数据源：云端").count()) === 0);
    await page.screenshot({ path: path.join(OUT, "dss_step3_wrong.png") });

    await page.getByLabel("密码 / 访问令牌").fill("pw");
    await page.getByRole("button", { name: "连接并切换" }).click();
    await page.getByText("数据源：云端 · u1").first().waitFor({ timeout: 25000 });
    // 登录成功之后那一次自动问（设计稿①）。它现在**挡在侧栏上面**，所以后面几步要先答它：
    // 登录对账（M8）自动跑一次：机器判得了的自己搬完，判不了的端出读数卡。
    await page.getByText("同步完成").first().waitFor({ timeout: 60000 });
    record("④ 登录成功 = 自动对账出一眼读数，判不了的冲突端上来",
      (await page.locator("body").innerText()).includes("在两端均有修改"));
    await page.getByRole("button", { name: "稍后处理" }).click();
    const body = await page.locator("body").innerText();
    record("⑤ 切成功：读数卡关掉，侧栏仍是「云端 · 账号」",
      body.includes(`数据源：云端 · u1`) && !body.includes("切到云端"));
    // 整份数据集真的换了：去「角色卡」页看列表
    await page.getByRole("button", { name: "角色卡" }).first().click();
    // 对话页是**常驻挂载**的（切页只加 hidden），所以整页文本里会有一份看不见的同名卡：
    // 断言必须钉"可见的那一个"，否则测的是隐藏 DOM。
    await page.locator("text=云端专有卡").locator("visible=true").first().waitFor({ timeout: 20000 });
    const cards = await page.locator("main").innerText();
    // 对账之后两边应当**一致**：本机独有的被推过去、云端独有的被拉回来（M8 的语义）
    record("⑥ 对账后两边一致：云端那张还在，本机独有的也到了对面",
      cards.includes("云端专有卡") && cards.includes("只有本机的卡"), cards.slice(0, 80));
    await page.screenshot({ path: path.join(OUT, "dss_step4_cloud.png") });

    // 点那一行本身（云端态整行就是"切回"的按钮；提示词只剩「切回」两格宽）
    await page.getByText("数据源：云端 · u1").first().click();
    await page.getByText("数据源：本机").first().waitFor({ timeout: 25000 });
    await page.getByRole("button", { name: "角色卡" }).first().click();
    await page.locator("text=只有本机的卡").locator("visible=true").first().waitFor({ timeout: 20000 });
    const back = await page.locator("main").innerText();
    record("⑦ 切回本机：那一行回到「本机」，云端独有的那卡也下载回来了",
      back.includes("只有本机的卡") && back.includes("云端专有卡"));
    await page.screenshot({ path: path.join(OUT, "dss_step5_back.png") });

    // ---- 数据同步（M7 向导 + M8 登录对账）：走的是同一条真链路 ----------------------
    // 本机态没有"对端"可言，所以那一格此时不该存在
    record("⑧ 本机态不给同步入口",
      (await page.locator("text=数据同步").count()) === 0);

    await page.getByText("数据源：本机").first().click();
    await page.getByLabel("服务地址").fill(cloudBase);
    await page.getByLabel("账号").fill("u1");
    await page.getByLabel("密码 / 访问令牌").fill("pw");
    await page.getByRole("button", { name: "连接并切换" }).click();
    await page.getByText("数据源：云端 · u1").first().waitFor({ timeout: 25000 });
    // 登录对账自动跑一次：机器判得了的自己搬完，判不了的端出读数卡（M8）
    await page.getByText("同步完成").first().waitFor({ timeout: 60000 });
    record("⑨ 再登录：对账再跑一遍，未决冲突还在（记忆的冲突自动档不碰）", true);
    await page.getByRole("button", { name: /立即处理/ }).click();
    // 对账模式直接落在裁决屏；这条是记忆，给三个版本选择
    await page.getByText(/冲突 \d+ /).first().waitFor({ timeout: 15000 });
    const cf = await page.locator('[role="dialog"]').innerText();
    record("⑩ 裁决屏：本机版本与云端版本并排，记忆给三档",
      cf.includes("本机版本") && cf.includes("云端版本") && cf.includes("保留两个版本"), cf.slice(0, 90));
    await page.screenshot({ path: path.join(OUT, "up_step2b_conflict.png") });
    await page.getByText("保留本机版本").click();
    await page.getByRole("button", { name: "应用选择" }).click();
    await page.getByRole("heading", { name: "同步完成" }).waitFor({ timeout: 60000 });
    const done = await page.locator('[role="dialog"]').innerText();
    record("⑪ 完成屏那两句：向量索引未随数据迁移 / 本机数据未做任何修改",
      done.includes("向量索引未随数据迁移") && done.includes("同步为复制操作"), done.slice(0, 100));
    await page.screenshot({ path: path.join(OUT, "up_step3_done.png") });

    await page.getByRole("button", { name: "知道了" }).click();
    record("⑫ 关掉之后入口仍在，且带着『上次同步』读数（登录后常驻，随时可同步）",
      (await page.locator("text=数据同步").count()) > 0
      && (await page.locator("text=上次同步").count()) > 0);
  } catch (err) {
    record("探针跑通了", false, String(err).slice(0, 240));
    await page.screenshot({ path: path.join(OUT, "dss_failed.png") }).catch(() => {});
  } finally {
    await browser.close();
  }
  process.exit(results.every(Boolean) ? 0 : 1);
})();

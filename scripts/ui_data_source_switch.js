// 数据源切换的浏览器侧（被 scripts/probe_data_source_switch.py 拉起）。
// 用法：node scripts/ui_data_source_switch.js <localBase> <cloudBase>
// 截图落在 build/（M5 五步 + M7 上行六步，失败时再来一张 dss_failed.png 供目视）。
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
    // 这一步同时也是"上行入口不许变成死路"的证明 —— 弹层里那句「这次先不带」真的能关掉。
    await page.getByRole("heading", { name: "要把本机这份带过去吗？" }).waitFor({ timeout: 25000 });
    record("④ 登录成功 = 自动弹上行那一问（默认停在「逐条合并」）",
      (await page.getByText("只有本机有的、只有云端有的直接过去").count()) > 0);
    await page.getByRole("button", { name: "这次先不带" }).click();
    await page.getByRole("heading", { name: "要把本机这份带过去吗？" })
      .waitFor({ state: "detached", timeout: 10000 });
    const body = await page.locator("body").innerText();
    // 顶栏被用户否了（09-27），所以云端态的可见指示就是侧栏那一行：它必须同时写出
    // 「云端」与「是谁」——只写"云端"的话，两个账号共用一台云端时界面上分不出是谁。
    record("⑤ 切成功：侧栏那一行改成「云端 · 账号」，问完就关",
      body.includes(`数据源：云端 · u1`) && !body.includes("切到云端"));
    // 整份数据集真的换了：去「角色卡」页看列表
    await page.getByRole("button", { name: "角色卡" }).first().click();
    // 对话页是**常驻挂载**的（切页只加 hidden），所以整页文本里会有一份看不见的同名卡：
    // 断言必须钉"可见的那一个"，否则测的是隐藏 DOM。
    await page.locator("text=云端专有卡").locator("visible=true").first().waitFor({ timeout: 20000 });
    const cards = await page.locator("main").innerText();
    record("⑥ 对面那份数据里只有对面的卡（只有本机那张不见了）",
      cards.includes("云端专有卡") && !cards.includes("只有本机的卡"), cards.slice(0, 60));
    await page.screenshot({ path: path.join(OUT, "dss_step4_cloud.png") });

    // 点那一行本身（云端态整行就是"切回"的按钮；提示词只剩「切回」两格宽）
    await page.getByText("数据源：云端 · u1").first().click();
    await page.getByText("数据源：本机").first().waitFor({ timeout: 25000 });
    await page.getByRole("button", { name: "角色卡" }).first().click();
    await page.locator("text=只有本机的卡").locator("visible=true").first().waitFor({ timeout: 20000 });
    const back = await page.locator("main").innerText();
    record("⑦ 切回本机：那一行回到「本机」，本机那份原样回来",
      back.includes("只有本机的卡") && !back.includes("云端专有卡"));
    await page.screenshot({ path: path.join(OUT, "dss_step5_back.png") });

    // ---- 上行（M7）：登录之后那四屏，走的是同一条真链路 ----------------------------
    // 本机态压根没有"对面"可推，所以那一格此时不该存在
    record("⑧ 本机态不给上行入口",
      (await page.locator("text=把本机这份带到云端").count()) === 0);

    await page.getByText("数据源：本机").first().click();
    await page.getByLabel("服务地址").fill(cloudBase);
    await page.getByLabel("账号").fill("u1");
    await page.getByLabel("密码 / 访问令牌").fill("pw");
    await page.getByRole("button", { name: "连接并切换" }).click();
    // 第二次登录仍然会问（④ 那一次已经被「这次先不带」答掉了，标记不该活到这一趟）
    await page.getByRole("heading", { name: "要把本机这份带过去吗？" })
      .waitFor({ timeout: 25000 });
    record("⑨ 再登录一次：还是先问，且这一趟真的往下走", true);
    await page.screenshot({ path: path.join(OUT, "up_step1_ask.png") });

    await page.getByRole("button", { name: "下一步：看差异" }).click();
    await page.getByRole("heading", { name: "差异看完了" }).waitFor({ timeout: 25000 });
    const dry = await page.locator('[role="dialog"]').innerText();
    record("⑩ 预检屏：四格读数 + 四类各一个勾 + 那条真冲突（这一步一个字都没写）",
      dry.includes("本机独有") && dry.includes("冲突 · 要你挑")
      && dry.includes("先处理 1 条冲突")
      && (await page.getByRole("checkbox").count()) === 4, dry.slice(0, 90));
    await page.screenshot({ path: path.join(OUT, "up_step2_dry.png") });

    // 第三屏：一条一屏。这一条是**角色卡**，所以"两份都留"讲不通 —— 只该给两个选择。
    await page.getByRole("button", { name: "先处理 1 条冲突" }).click();
    await page.getByText("冲突 1 / 1 · 一条角色卡").waitFor({ timeout: 15000 });
    const cf = await page.locator('[role="dialog"]').innerText();
    record("⑪ 冲突屏：两份并排 + 卡这一类不给「两份都留」",
      cf.includes("本机这份") && cf.includes("对面那份") && !cf.includes("两份都留")
      && (await page.getByRole("radio").count()) === 2, cf.slice(0, 90));
    await page.screenshot({ path: path.join(OUT, "up_step2b_conflict.png") });
    await page.getByText("保留本机这份").click();
    await page.getByRole("button", { name: "就这么定" }).click();
    await page.getByRole("heading", { name: "差异看完了" }).waitFor({ timeout: 15000 });

    // 不勾的那一类不许碰对面（用户 09-27：「可勾选同步项，不勾选的就用云端」）
    await page.getByLabel("同步记忆").uncheck();
    await page.getByRole("button", { name: /开始上行/ }).click();
    await page.getByRole("heading", { name: "上行完成" }).waitFor({ timeout: 60000 });
    const done = await page.locator('[role="dialog"]').innerText();
    record("⑫ 完成屏那三句：带过去了什么 / 索引是重建不是搬 / 本机那份没动",
      done.includes("已带过去") && done.includes("向量索引没有搬")
      && done.includes("一个字都没改"), done.slice(0, 100));
    await page.screenshot({ path: path.join(OUT, "up_step3_done.png") });

    await page.getByRole("button", { name: "知道了" }).click();
    record("⑬ 关掉之后入口仍在（登录后常驻，随时可同步）",
      (await page.locator("text=把本机这份带到云端").count()) > 0);
  } catch (err) {
    record("探针跑通了", false, String(err).slice(0, 240));
    await page.screenshot({ path: path.join(OUT, "dss_failed.png") }).catch(() => {});
  } finally {
    await browser.close();
  }
  process.exit(results.every(Boolean) ? 0 : 1);
})();

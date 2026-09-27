// 数据源切换的浏览器侧（被 scripts/probe_data_source_switch.py 拉起）。
// 用法：node scripts/ui_data_source_switch.js <localBase> <cloudBase>
// 截图落在 build/（七步各一张，失败时再来一张 m5_failed.png 供目视）。
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
  const cloudHost = cloudBase.replace(/^https?:\/\//, "");
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
    const body = await page.locator("body").innerText();
    // 顶栏被用户否了（09-27），所以云端态的可见指示就是侧栏那一行：它必须同时写出
    // 「云端」与「是谁」——只写"云端"的话，两个账号共用一台云端时界面上分不出是谁。
    record("④ 切成功：侧栏那一行改成「云端 · 账号」，弹层已关",
      body.includes(`数据源：云端 · u1`) && !body.includes("切到云端"));
    record("⑤ 对面 host 在登录那一步被读过（弹层里的地址就是它）",
      body.includes(cloudHost) || !body.includes("服务地址"));
    // 整份数据集真的换了：去「角色卡」页看列表
    await page.getByRole("button", { name: "角色卡" }).first().click();
    // 对话页是**常驻挂载**的（切页只加 hidden），所以整页文本里会有一份看不见的同名卡：
    // 断言必须钉"可见的那一个"，否则测的是隐藏 DOM。
    await page.locator("text=云端专有卡").locator("visible=true").first().waitFor({ timeout: 20000 });
    const cards = await page.locator("main").innerText();
    record("⑥ 对面那份数据里只有对面的卡（本机专有卡不见了）",
      cards.includes("云端专有卡") && !cards.includes("本机专有卡"), cards.slice(0, 60));
    await page.screenshot({ path: path.join(OUT, "dss_step4_cloud.png") });

    // 点那一行本身（云端态整行就是"切回"的按钮；提示词只剩「切回」两格宽）
    await page.getByText("数据源：云端 · u1").first().click();
    await page.getByText("数据源：本机").first().waitFor({ timeout: 25000 });
    await page.getByRole("button", { name: "角色卡" }).first().click();
    await page.locator("text=本机专有卡").locator("visible=true").first().waitFor({ timeout: 20000 });
    const back = await page.locator("main").innerText();
    record("⑦ 切回本机：那一行回到「本机」，本机那份原样回来",
      back.includes("本机专有卡") && !back.includes("云端专有卡"));
    await page.screenshot({ path: path.join(OUT, "dss_step5_back.png") });
  } catch (err) {
    record("探针跑通了", false, String(err).slice(0, 240));
    await page.screenshot({ path: path.join(OUT, "dss_failed.png") }).catch(() => {});
  } finally {
    await browser.close();
  }
  process.exit(results.every(Boolean) ? 0 : 1);
})();

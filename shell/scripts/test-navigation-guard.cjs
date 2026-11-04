/**
 * 导航护栏 + 控制台地址归一的判据（`R102-73` 与本轮 A-2）。
 *
 * 为什么这两支测试是**必须**的而不是补考：`R102-73` 那次"装完机控制台停在正在打开控制台…"
 * 就是这个判据把自家合法终态拦了，而**当时仓库里没有任何测试钉它** —— 错误形状一路进了
 * 安装包，靠用户肉眼发现。（`scripts/js/shell_quit_smoke.js:5-11` 记着这条判据当年想接进
 * 壳退出冒烟、试两回撤回的经过；它说的"十格判据表"在本文件之前并不存在，现在才成立。）
 *
 * 与 `test-update-cache-cleanup.cjs` 同一姿势：判据模块不 import electron，node 直跑
 * 编译产物就能测（不起壳、不建窗）。
 */

const assert = require("node:assert/strict");
const { navigationVerdict, windowOpenVerdict } = require("../out/main/navigation.js");
const { normalizeConsoleUrl } = require("../out/main/backend.js");

let passed = 0;
const t = (name, fn) => {
  try {
    fn();
    passed += 1;
  } catch (e) {
    console.error(`FAIL ${name}\n  ${e.message}`);
    process.exitCode = 1;
  }
};

const BACKEND = "http://127.0.0.1:8000";

// ---- 放行面：应用自己的两种终态 ----
t("本地着陆页放行（file://）", () =>
  assert.equal(navigationVerdict("file:///C:/app/dist/index.html", BACKEND), "allow"));

t("后端控制台放行（R102-73 那一次就是被这条拦死的）", () =>
  assert.equal(navigationVerdict("http://127.0.0.1:8000/", BACKEND), "allow"));

t("同 origin 的桌宠路由放行", () =>
  assert.equal(navigationVerdict("http://127.0.0.1:8000/#/pet", BACKEND), "allow"));

t("同 origin 带查询串放行", () =>
  assert.equal(navigationVerdict("http://127.0.0.1:8000/api/x?a=1", BACKEND), "allow"));

// ---- 拦截面：护栏存在的理由 ----
t("远程站点拦（AI 内容里塞的 <a> 想去钓鱼页）", () =>
  assert.equal(navigationVerdict("https://evil.test/", BACKEND), "deny-remote"));

t("同前缀**真攻击**拦：userinfo 伪装（字面串以本机 origin 开头，真 origin 是远程）", () => {
  // 实测：`new URL("http://127.0.0.1:8000@evil.test/").origin === "http://evil.test"`，
  // 而那个字符串 `.startsWith("http://127.0.0.1:8000")` 是 **true** —— 判若写成前缀包含
  // （批 13 之前的味道），这条就直接放出去了。origin 相等才是判据（文件头那句的依据）。
  assert.equal(navigationVerdict("http://127.0.0.1:8000@evil.test/", BACKEND), "deny-remote");
});

t("同主机不同端口拦（origin 相等才放）", () =>
  assert.equal(navigationVerdict("http://127.0.0.1:9999/", BACKEND), "deny-remote"));

t("协议不同也拦（http 的后端不认 https 的同名 origin）", () =>
  assert.equal(navigationVerdict("https://127.0.0.1:8000/", BACKEND), "deny-remote"));

t("localhost 与 127.0.0.1 是两个 origin（都拦：本后端只认配置里那一个）", () =>
  assert.equal(navigationVerdict("http://localhost:8000/", BACKEND), "deny-remote"));

t("空串拦（未知目标不该导航）", () =>
  assert.equal(navigationVerdict("", BACKEND), "deny-remote"));

t("非字符串拦（调用方给错类型不给放行）", () =>
  assert.equal(navigationVerdict(/** @type {any} */ (null), BACKEND), "deny-remote"));

t("目标解析不出来拦", () =>
  assert.equal(navigationVerdict("not a url at all", BACKEND), "deny-remote"));

// 归一 file:// 那一格之后**唯一的行为变化**（实测三个形状：`file://%%`、`file://x y`、
// `file://%00` 都以 `file://` 开头却 `new URL` 抛）。从前那句字面 `startsWith("file://")`
// 会把它们放行；现在只认解析后的 protocol ⇒ 拦。方向是收紧，不是放宽，所以钉住：
// 反方向（把该放的拦了）才是 R102-73。
t("`file://` 开头但解析不动的串拦（归一后收紧的那一格）", () => {
  for (const s of ["file://%%", "file://x y", "file://%00"]) {
    assert.equal(navigationVerdict(s, BACKEND), "deny-remote", `${s} 不该被放行`);
  }
});

t("file 的两种非规范写法放行（字面那句认不出、解析那条认得出）", () => {
  // 实测 `new URL("FILE://C:/x").protocol === "file:"` 而 `"FILE://C:/x".startsWith("file://")`
  // 是 **false** —— 也就是说从前那句字面判据在这里**漏放**（自家本地页被拦，正是 R102-73
  // 的形状），归一到解析后判据把它接住了。
  for (const s of ["FILE://C:/x", "file:/x"]) {
    assert.equal(navigationVerdict(s, BACKEND), "allow", `${s} 是本地的，该放行`);
  }
});


// ---- 后端地址坏了那一格：拦，但报成另一种因 ----
// 文件头写了为什么**不能**"宁可放行"：配置越坏、防护越小，方向反了。
t("后端地址坏了 ⇒ 拦，且读数报成'配错了'而不是'有东西想跑掉'", () =>
  assert.equal(navigationVerdict("https://evil.test/", "not a url"), "deny-broken-backend"));

t("后端坏了但目标是 file:// ⇒ 放行照旧（本地着陆页不依赖后端配置）", () =>
  assert.equal(navigationVerdict("file:///C:/app/index.html", "not a url"), "allow"));

// ---- 控制台地址归一：本轮 A-2 的**真凶**那一路 ----
// `ROLECARD_BACKEND_URL=127.0.0.1:8000`（不带 scheme，`docs/开发流程.md:263` 那种写法）
// 实测会被浏览器按相对路径解成 file:///…/127.0.0.1:8000/ —— 护栏放行、页面找不到，
// 症状与 R102-73 一模一样。归一在**源头**做，不在护栏放宽。
t("不带 scheme 的裸地址补成 http://", () =>
  assert.equal(normalizeConsoleUrl("127.0.0.1:8000", "127.0.0.1", 8000), "http://127.0.0.1:8000"));

t("带尾斜杠的裸地址补成 http:// 且**不留尾斜杠**", () =>
  assert.equal(normalizeConsoleUrl("127.0.0.1:8000/", "127.0.0.1", 8000), "http://127.0.0.1:8000"));

t("消费方拼出来的那个 URL 才是判据（base 带斜杠会跳成 host//，StaticFiles 救不了）", () => {
  // `shell/web/index.html:131` 拼的是 `${base}/` 与 `${base}/#/pet`；后端界面是
  // `app.mount("/", StaticFiles(...))`（api/main.py:490），它**不把 `//` 规范化成 `/`**，
  // 那是真 404。所以这里断言的不是"我的返回值长什么样"，而是"用户最终跳去哪"。
  // ⚠ 这一支在本文件的第一版里是**写反的**（当时断言 `…8000/`，等于把一个 bug 固化成
  // 期望值）；红过一次之后才改成问拼接结果 —— 记在这儿，别把它当口味改动。
  for (const raw of [undefined, "", "127.0.0.1:8000", "127.0.0.1:8000/", "//127.0.0.1:8000"]) {
    const base = normalizeConsoleUrl(raw, "127.0.0.1", 8000);
    assert.equal(`${base}/`, "http://127.0.0.1:8000/", `raw=${JSON.stringify(raw)} 拼出 ${base}/`);
    assert.equal(`${base}/#/pet`, "http://127.0.0.1:8000/#/pet", `raw=${JSON.stringify(raw)}`);
    // 拼接后仍能解析回本机 origin（护栏认的就是它）
    assert.equal(navigationVerdict(`${base}/`, base), "allow");
  }
});

t("带路径的部署也只在**末尾**去斜杠，不碰中段", () =>
  assert.equal(normalizeConsoleUrl("https://rc.local:8443/base/", "127.0.0.1", 8000), "https://rc.local:8443/base"));

t("协议相对写法（//host:port）补成 http://", () =>
  assert.equal(normalizeConsoleUrl("//127.0.0.1:8000", "127.0.0.1", 8000), "http://127.0.0.1:8000"));

t("已带 scheme 的原样（操作员显式写的优先，包括 https）", () => {
  assert.equal(normalizeConsoleUrl("https://rc.local:8443", "127.0.0.1", 8000), "https://rc.local:8443");
  assert.equal(normalizeConsoleUrl("http://x", "127.0.0.1", 8000), "http://x");
});

t("没配 ⇒ 用 host:port 默认", () =>
  assert.equal(normalizeConsoleUrl(undefined, "127.0.0.1", 8000), "http://127.0.0.1:8000"));

t("空串 / 纯空白都算没配（而不是 http:// 空 host）", () => {
  assert.equal(normalizeConsoleUrl("", "127.0.0.1", 8000), "http://127.0.0.1:8000");
  assert.equal(normalizeConsoleUrl("   ", "127.0.0.1", 8000), "http://127.0.0.1:8000");
});

t("端口取自 envPort 那一路时不归一仍拼得对（默认路径零变化）", () =>
  assert.equal(normalizeConsoleUrl(undefined, "127.0.0.1", 8123), "http://127.0.0.1:8123"));

// ---- 归一之后护栏与地址要**合起来**成立（分开各自绿不等于接缝绿）----
t("接缝：裸地址经归一后护栏放行（A-2 那次的完整复现链）", () => {
  const fixed = normalizeConsoleUrl("127.0.0.1:8000", "127.0.0.1", 8000);
  assert.equal(navigationVerdict(`${fixed}/`, fixed), "allow");
  // 对照：不归一的话这条正是 R102-73 的现场（浏览器解析出 file://…/127.0.0.1:8000/）
  const naive = "127.0.0.1:8000";
  assert.equal(new URL(`/${naive}/`, "file:///C:/app/index.html").protocol, "file:");
});

// ---- A-10 新开窗判据：只有 http/https 配外送系统浏览器，其余一种都不出（2026-11-04）----
t("https 外链 → open-external（真人点的文档链接该能点开）", () =>
  assert.equal(windowOpenVerdict("https://docs.example.com/api"), "open-external"));

t("http 外链 → open-external", () =>
  assert.equal(windowOpenVerdict("http://example.org/x"), "open-external"));

t("大小写不同的 scheme 也认（URL 解析后 protocol 归小写）", () =>
  assert.equal(windowOpenVerdict("HTTPS://EXAMPLE.ORG/X"), "open-external"));

t("本机后端地址外送也无妨（走系统浏览器，不进应用窗）", () =>
  assert.equal(windowOpenVerdict("http://127.0.0.1:8000/api/docs"), "open-external"));

t("file: 绝不外送（本地文件直接执行面）", () =>
  assert.equal(windowOpenVerdict("file:///C:/Windows/System32/calc.exe"), "deny"));

t("javascript: 不外送（解析得动也不放）", () =>
  assert.equal(windowOpenVerdict("javascript:alert(1)"), "deny"));

t("data: 不外送", () =>
  assert.equal(windowOpenVerdict("data:text/html,<script>x</script>"), "deny"));

t("能唤起本机程序的自定义 scheme 一种都不外送", () => {
  assert.equal(windowOpenVerdict("smb://nas/share"), "deny");
  assert.equal(windowOpenVerdict("ms-msdt:/id some"), "deny");
  assert.equal(windowOpenVerdict("mailto:a@b.c"), "deny"); // 审计口径：http/https-only
});

t("解析不动的串 → deny（与 navigationVerdict 同句）", () =>
  assert.equal(windowOpenVerdict("not a url"), "deny"));

t("空串 / 非字符串 → deny", () => {
  assert.equal(windowOpenVerdict(""), "deny");
  assert.equal(windowOpenVerdict(undefined), "deny");
});

t("协议相对写法（//host）没有 scheme → deny", () =>
  assert.equal(windowOpenVerdict("//evil.test/x"), "deny"));

console.log(`导航护栏 + 控制台地址归一：${passed} 项通过`);
if (process.exitCode) console.error("有失败项");

// 更新链判据的真跑测试（node + 编译产物，不引测试框架 —— 与 test-update-cache-cleanup.cjs 同规）。
//
// 跑法（cwd=shell）：npm run test:feedback   （先 npm run build 再跑这里）
// 门禁：gate.py 的「shell 更新反馈判据」步骤（full 档）。
//
// 为什么值得为它专门立测试：A-8 的根因是**上游两个出口都认领同一次失败**，而
// "先 emit error 再 throw"这件事住在 electron-updater 的编译产物里 —— 升级那个包
// 就可能静默变向，而 typecheck 对"弹两次"毫无感觉。
const assert = require("node:assert/strict");
const path = require("node:path");

const {
  advanceStageOnDownload,
  failureOf,
  failureTitle,
  isCheckTerminal,
  shouldHandoffQuitToInstall,
} = require(path.join(__dirname, "..", "out", "main", "updateFeedback.js"));

let pass = 0;
let fail = 0;
function check(name, fn) {
  try {
    fn();
    pass += 1;
    console.log(`✅ ${name}`);
  } catch (error) {
    fail += 1;
    console.log(`❌ ${name} — ${error.message}`);
  }
}

// ---------------------------------------------------------------- A-8 一次失败一个主人

check("手动检查失败 ⇒ 出 toast、报 check、并消费上下文", () => {
  const r = failureOf("check");
  assert.equal(r.shouldNotify, true);
  assert.equal(r.stage, "check");
  assert.equal(r.consumesAction, true);
  assert.equal(failureTitle(r.stage), "检查更新失败");
});

check("手动下载失败 ⇒ 报 download，不冒名成「检查更新失败」", () => {
  const r = failureOf("download");
  assert.equal(r.stage, "download");
  assert.equal(failureTitle(r.stage), "下载更新失败");
});

check("开机那次顺手查失败 ⇒ 一声都不出（静默档保持原语义）", () => {
  const r = failureOf(null);
  assert.equal(r.shouldNotify, false);
  assert.equal(r.consumesAction, false); // 本来就没有上下文可消费
});

// ---------------------------------------------------------------- A-8 同族：终态消费

check("「已是最新」是手点检查的终态 ⇒ 回执并消费", () => {
  assert.equal(isCheckTerminal("check"), true);
});

check("静默档与下载中都不算检查终态（不该被这一拍消费掉）", () => {
  assert.equal(isCheckTerminal(null), false);
  assert.equal(isCheckTerminal("download"), false);
});

// ---------------------------------------------------------------- A-8 同族：阶段推进

check("autoDownload：检查成功后下载失败 ⇒ 报「下载更新失败」（不冒名成检查）", () => {
  // 这条是布尔时代剩下的那个说谎形状：用户只点过一次「检查更新」，
  // autoDownload 直接开始下载，下载挂了却报"检查更新失败"。
  // 客观证据是 download-progress 事件 —— 阶段由它推进（判据见 advanceStageOnDownload）。
  const afterCheck = "check";
  const advanced = advanceStageOnDownload(afterCheck);
  assert.equal(advanced, "download");
  const r = failureOf(advanced);
  assert.equal(r.stage, "download");
  assert.equal(failureTitle(r.stage), "下载更新失败");
});

check("静默档不因开始下载而变成「用户要答案」（自动下载失败不许弹 toast）", () => {
  // 开机顺手查 + autoDownload：谁都没点过东西，自动开始下载不等于要回执。
  assert.equal(advanceStageOnDownload(null), null);
  assert.equal(failureOf(advanceStageOnDownload(null)).shouldNotify, false);
});

check("已是 download 阶段就别往回退", () => {
  assert.equal(advanceStageOnDownload("download"), "download");
});

// ---------------------------------------------------------------- A-6 退出交给谁

check("三个条件齐才把退出交给安装器", () => {
  assert.equal(
    shouldHandoffQuitToInstall({ autoInstallOnQuit: true, stateKind: "ready", installing: false }),
    true,
  );
});

check("没有下载就绪的状态 ⇒ 不许交接（退出就是退出）", () => {
  for (const kind of ["idle", "checking", "available", "downloading", "error", "uptodate"]) {
    assert.equal(
      shouldHandoffQuitToInstall({ autoInstallOnQuit: true, stateKind: kind, installing: false }),
      false,
      `kind=${kind} 不该交接`,
    );
  }
});

check("已经在装了 ⇒ 不交接（挡死循环：quitAndInstall 自己会再触发一次 before-quit）", () => {
  assert.equal(
    shouldHandoffQuitToInstall({ autoInstallOnQuit: true, stateKind: "ready", installing: true }),
    false,
  );
});

check("用户关掉「退出时自动装」⇒ 永远不交接", () => {
  assert.equal(
    shouldHandoffQuitToInstall({ autoInstallOnQuit: false, stateKind: "ready", installing: false }),
    false,
  );
});

// ---------------------------------------------------------------- 判据不许空转

check("所有 ManualAction 取值都有定义好的失败读数（新增档忘了写会在这里红）", () => {
  for (const action of ["check", "download", null]) {
    const r = failureOf(action);
    assert.ok(r && typeof r.shouldNotify === "boolean", `failureOf(${String(action)}) 形状不对`);
    assert.ok(
      r.shouldNotify === (action !== null),
      `shouldNotify 必须等价于"这是手点的那一轮"：action=${String(action)}`,
    );
    if (r.shouldNotify) {
      assert.ok(r.stage === "check" || r.stage === "download", `stage 越界：${r.stage}`);
      assert.equal(failureTitle(r.stage).length > 0, true);
    }
  }
});

console.log(`\n更新链判据：${pass}/${pass + fail}${fail ? " 有失败" : ""}`);
process.exit(fail === 0 ? 0 : 1);

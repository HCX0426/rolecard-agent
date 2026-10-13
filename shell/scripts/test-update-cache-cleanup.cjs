// 升级缓存清理判据的真跑测试（node + 真实临时目录，不引测试框架）。
//
// 为什么要有它：`out/main/updateCacheCleanup.js` 决定"删哪个文件"，删错了的代价是
// 用户重下一整个安装包、或把还没装的下载删掉。shell 没有测试框架（frontend 有 vitest，
// 壳这侧只有 tsc），而"只靠 typecheck"守不住判据 —— 类型对、逻辑反是常态。
// 这个脚本用真实文件系统跑全部六种读数，任何一条不符预期就 exit 1。
//
// 跑法（cwd=shell）：npm run test:cleanup   （它先 npm run build 再跑这里）
// 门禁：gate.py 的「shell updater 清理判据」步骤（full 档）。

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { versionFromArtifactName, cleanupConsumedPendingUpdate } = require(
  path.join(__dirname, "..", "out", "main", "updateCacheCleanup.js"),
);

const results = [];
function check(name, fn) {
  try {
    fn();
    results.push({ name, ok: true });
    console.log(`✅ ${name}`);
  } catch (error) {
    results.push({ name, ok: false });
    console.log(`❌ ${name} — ${error.message}`);
  }
}

function makeFs(root) {
  return {
    exists: (p) => fs.existsSync(p),
    readFile: (p) => fs.readFileSync(p, "utf8"),
    sizeOf: (p) => {
      if (!fs.existsSync(p)) return 0;
      const stat = fs.statSync(p);
      if (stat.isFile()) return stat.size;
      let total = 0;
      for (const item of fs.readdirSync(p)) {
        const child = path.join(p, item);
        const childStat = fs.statSync(child);
        total += childStat.isDirectory() ? 0 : childStat.size;
      }
      return total;
    },
    removeDir: (p) => fs.rmSync(p, { recursive: true, force: true }),
  };
}

/** 造一个 pending 目录：update-info.json 里写 fileName，旁边放一个占位安装包。 */
function seedPending(cacheDir, fileName, bytes = 1024) {
  const pending = path.join(cacheDir, "pending");
  fs.mkdirSync(pending, { recursive: true });
  fs.writeFileSync(
    path.join(pending, "update-info.json"),
    JSON.stringify({ fileName, sha512: "x".repeat(88), isAdminRightsRequired: false }),
  );
  fs.writeFileSync(path.join(pending, fileName), Buffer.alloc(bytes, 7));
  return pending;
}

function tempCache(label) {
  return fs.mkdtempSync(path.join(os.tmpdir(), `rc-cleanup-${label}-`));
}

// ---------------------------------------------------------------- 版本解析

check("从标准安装包名读版本", () => {
  assert.equal(versionFromArtifactName("rolecard-agent-0.4.2-x64.exe"), "0.4.2");
  assert.equal(versionFromArtifactName("rolecard-agent-10.2.0-arm64.exe"), "10.2.0");
  // 不带 arch 的形状（artifactName 改窄时仍要能读 —— 与 core/artifacts.py 同一宽容口径）
  assert.equal(versionFromArtifactName("rolecard-agent-0.4.2.exe"), "0.4.2");
});

check("形状外的一律 null（不猜）", () => {
  assert.equal(versionFromArtifactName("installer.exe"), null);
  assert.equal(versionFromArtifactName("rolecard-agent-0.4-x64.exe"), null);
  assert.equal(versionFromArtifactName("rolecard-agent-0.4.2-x64.zip"), null);
  assert.equal(versionFromArtifactName(""), null);
});

// ---------------------------------------------------------------- 六种读数

check("① 已装完（版本 == 当前）→ 删掉整个 pending", () => {
  const cache = tempCache("consumed");
  const pending = seedPending(cache, "rolecard-agent-0.4.2-x64.exe", 4096);
  const report = cleanupConsumedPendingUpdate(cache, "0.4.2", makeFs(cache));
  assert.equal(report.kind, "removed");
  assert.equal(report.version, "0.4.2");
  assert.ok(report.freedBytes > 0, "释放字节读数必须非零（删之前量的）");
  assert.ok(!fs.existsSync(pending), "pending 目录必须真的没了");
  // 关键：差量基线（pending 的兄弟）不许被牵连
  assert.ok(fs.existsSync(cache), "cacheDir 本身不该被删");
});

check("② 下载了还没装（版本 != 当前）→ 原样留着", () => {
  const cache = tempCache("pending-newer");
  const pending = seedPending(cache, "rolecard-agent-0.5.0-x64.exe");
  const report = cleanupConsumedPendingUpdate(cache, "0.4.2", makeFs(cache));
  assert.equal(report.kind, "keep");
  assert.match(report.reason, /0\.5\.0/);
  assert.ok(fs.existsSync(pending), "未安装的下载被误删了");
  assert.ok(fs.existsSync(path.join(pending, "rolecard-agent-0.5.0-x64.exe")));
});

check("③ 没有 pending → keep，不报错（首次启动是常态）", () => {
  const cache = tempCache("no-pending");
  const report = cleanupConsumedPendingUpdate(cache, "0.4.2", makeFs(cache));
  assert.equal(report.kind, "keep");
  assert.ok(fs.existsSync(cache));
});

check("④ update-info.json 坏了 → keep（读不懂就不删）", () => {
  const cache = tempCache("broken");
  fs.mkdirSync(path.join(cache, "pending"), { recursive: true });
  fs.writeFileSync(path.join(cache, "pending", "update-info.json"), "{ 不是 json");
  const report = cleanupConsumedPendingUpdate(cache, "0.4.2", makeFs(cache));
  assert.equal(report.kind, "keep");
  assert.ok(fs.existsSync(path.join(cache, "pending", "update-info.json")), "坏文件也不该被删");
});

check("⑤ fileName 里没版本 → keep（判据不成立别赌）", () => {
  const cache = tempCache("noversion");
  seedPending(cache, "installer.exe");
  const report = cleanupConsumedPendingUpdate(cache, "0.4.2", makeFs(cache));
  assert.equal(report.kind, "keep");
  assert.ok(fs.existsSync(path.join(cache, "pending", "installer.exe")));
});

check("⑥ 删除失败 → keep 且如实报（不假装清干净）", () => {
  const cache = tempCache("rmfail");
  seedPending(cache, "rolecard-agent-0.4.2-x64.exe");
  const hostile = { ...makeFs(cache), removeDir: () => { throw new Error("EBUSY"); } };
  const report = cleanupConsumedPendingUpdate(cache, "0.4.2", hostile);
  assert.equal(report.kind, "keep");
  assert.match(report.reason, /EBUSY/);
  assert.ok(fs.existsSync(path.join(cache, "pending")), "删失败时目录必须还在");
});

check("⑦ 尾部斜杠不影响路径（cacheDir 传法两种都对）", () => {
  const cache = tempCache("slash");
  seedPending(cache, "rolecard-agent-0.4.2-x64.exe");
  const report = cleanupConsumedPendingUpdate(`${cache}/`, "0.4.2", makeFs(cache));
  assert.equal(report.kind, "removed");
});

const failed = results.filter((r) => !r.ok);
console.log(`\n升级缓存清理判据：${results.length - failed.length}/${results.length}`);
process.exit(failed.length === 0 ? 0 : 1);

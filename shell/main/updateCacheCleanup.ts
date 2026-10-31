/**
 * 升级后的临时文件清理（用户 2026-10-13 提的规矩：装好/更新完要自动清临时文件）。
 *
 * 这个模块**刻意不 import electron**：判据要能被 node 直接跑测试（shell 没有测试框架，
 * 而"清理逻辑删错了东西"的代价是用户丢下载或丢回滚快照，不能只靠 typecheck）。
 * 所有文件系统读写都从外面注入 —— 于是"删对了什么、为什么留着"全部可测。
 *
 * 三类东西，三种处置（全部读源码/实测核实，不是猜）：
 *
 *   1. **pending/ 里已装完的新版安装包** —— 删。electron-updater 只在"下次下载时
 *      sha512 对不上"才 emptyDir 它（DownloadedUpdateHelper.js:74-82），装完不清；
 *      Windows 的 NsisUpdater 路径没有任何清理（6.8.9 全库 grep `deleteUpdaterCache`
 *      零命中）。不删就是每次升级永久泄漏一整个安装包（本项目 ~281 MB）。
 *      判据：`pending/update-info.json` 的 `fileName` 里那个版本 == 当前运行版本
 *      （update-info.json 只有 {fileName, sha512, isAdminRightsRequired}，没有版本字段，
 *      版本住在 fileName 里 —— 形状由 electron-builder 的 artifactName 决定）。
 *      相等 ⇒ 这份下载已经被装上（正在跑的就是它）⇒ 它作为"待装"的使命结束。
 *
 *   2. **cacheDir/installer.exe（差量下载基线）** —— **留**。它由 NSIS 每次安装时
 *      `copyFile "$EXEPATH"` 自己写（installer.nsh:93），是 `differentialDownloadInstaller`
 *      的 oldFile（AppUpdater.js:668）；删了下次更新退化成全量重下。而且它每次安装
 *      都被覆盖成"当前版本"，不累积 —— 实测本机这一份 = 198,240,362 字节，
 *      正是今天装的那版安装包，逐字节吻合。
 *
 *   3. **升级前的数据快照（rolecard-agent-update-backups/）** —— **不在这里管**。
 *      它是回滚路径不是临时文件，寿命由 updater.ts 的 BACKUP_KEEP（份数）与
 *      BACKUP_MAX_BYTES（总体积，R1031-29）双上限定，裁法是本文件导出的
 *      `snapshotsToEvict`（判据住这儿，删的动作在 updater）。
 *      把"清临时文件"理解成"顺手删备份"是要出人命的读法。
 *
 * 保守优先：读不到 json、解析不出版本、版本不等于当前 —— 一律**不动**。
 * 宁可多留一个文件，不可误删一次下载（误删的代价是用户重下一遍；而误删判据的代价
 * 是每次都重下，且"看起来清理生效了"没人会去查 —— 静默失效那一族）。
 */

/** 从 `rolecard-agent-0.4.2-x64.exe` 这类文件名里读版本；形状不对回 null。 */
export function versionFromArtifactName(name: string): string | null {
  const match = /-(\d+\.\d+\.\d+)(?:-[A-Za-z0-9_]+)?\.exe$/i.exec(name.trim());
  return match?.[1] ?? null;
}

export type CleanupFs = {
  exists: (path: string) => boolean;
  readFile: (path: string) => string;
  /** 目录（或文件）的字节总量；不存在/读不动回 0。用于"释放了多少"这个读数。 */
  sizeOf: (path: string) => number;
  removeDir: (path: string) => void;
};

export type CleanupReport =
  | { kind: "removed"; file: string; version: string; freedBytes: number }
  | { kind: "keep"; reason: string };

/**
 * 清掉"已装完"的 pending 下载。返回读数（调用方负责记进 shell.log ——
 * 清了什么、为什么留，都要能被事后查，不许静默）。
 */
export function cleanupConsumedPendingUpdate(
  cacheDir: string,
  currentVersion: string,
  fs: CleanupFs,
): CleanupReport {
  const pendingDir = `${cacheDir.replace(/[\\/]+$/, "")}/pending`;
  const infoPath = `${pendingDir}/update-info.json`;
  if (!fs.exists(infoPath)) {
    return { kind: "keep", reason: "没有 pending/update-info.json（没下载过，或已被清）" };
  }
  let fileName: unknown;
  try {
    fileName = (JSON.parse(fs.readFile(infoPath)) as { fileName?: unknown }).fileName;
  } catch (error) {
    return { kind: "keep", reason: `update-info.json 读不出来：${String(error).slice(0, 80)}` };
  }
  if (typeof fileName !== "string" || !fileName) {
    return { kind: "keep", reason: "update-info.json 里没有 fileName" };
  }
  const version = versionFromArtifactName(fileName);
  if (!version) {
    return { kind: "keep", reason: `fileName 里解析不出版本：${fileName.slice(0, 80)}` };
  }
  if (version !== currentVersion) {
    // 两种情况都落在这：下载了还没装（要留着复用），或装的是别的版本（判据不成立，别赌）
    return { kind: "keep", reason: `pending 是 ${version}、当前 ${currentVersion}，不动` };
  }
  const freedBytes = fs.sizeOf(pendingDir); // 必须在删之前量
  try {
    fs.removeDir(pendingDir);
  } catch (error) {
    return { kind: "keep", reason: `删除失败，保留：${String(error).slice(0, 80)}` };
  }
  return { kind: "removed", file: fileName, version, freedBytes };
}

/**
 * 升级快照的体积闸（R1031-29，2026-10-31）：`BACKUP_KEEP` 只管份数不管字节 ——
 * 数据根长到 8 GB 时"最近 2 份"就是 16 GB 的静默磁盘吞噬。开源参照 logback 的
 * `totalSizeCap`：份数 + 总体积双上限，超限**从最旧的归档开始删**，且
 * "never deletes the current active file" —— 这里对应"刚写的那份绝不删"。
 *
 * 判据拆成纯函数（名字进、名字出），删的动作留在调用方：与 `cleanupConsumedPending
 * Update` 同一条纪律 —— 判据必须能被 node 直接跑测试，electron 不进来。
 *
 * @param names 快照目录名，**调用方已按时间序排好**（`backup-<ISO 戳>` 字典序=时间序）。
 *              最后一个 = 刚写的这份，永远不删。
 * @param sizeOf 单个快照目录的字节量。读不动回 0（算不进预算 = 保守多留，不许少留）。
 * @returns 要删掉的目录名（可能为空数组）。
 */
export function snapshotsToEvict(
  names: string[],
  sizeOf: (name: string) => number,
  maxBytes: number,
  keepCount: number,
): string[] {
  if (names.length === 0) return [];
  // 第一刀：份数超 keepCount 的部分（最旧的先走）—— 原 BACKUP_KEEP 语义一字不动。
  const overCount = names.slice(0, Math.max(0, names.length - keepCount));
  // 第二刀：留下 keepCount 份后仍超体积预算 —— 继续从最旧删起，**永远保留最新那一份**
  // （它就是"回滚路径"本身，删了它升级就无处可退，宁超预算不许无路）。
  const survivors = names.slice(Math.max(0, names.length - keepCount));
  let total = survivors.reduce((sum, name) => sum + sizeOf(name), 0);
  const overSize: string[] = [];
  // survivors 至少剩 1 份时停手：while 条件里的 `> 1` 就是那条"最新一份不动"。
  while (total > maxBytes && survivors.length - overSize.length > 1) {
    const next = survivors[overSize.length] as string; // 上面的 length 守卫保证了这格存在
    overSize.push(next);
    total -= sizeOf(next);
  }
  return [...overCount, ...overSize];
}

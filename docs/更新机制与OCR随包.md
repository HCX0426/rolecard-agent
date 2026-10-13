# 更新机制与 OCR 随包 · 设计稿

> 状态：**已全部落地（2026-10-13，§六 清单逐项勾完）**。本稿先钉事实与决策，实现分三笔提交。
> 触发：用户 2026-10-13 三连问（覆盖安装？检查更新按钮？增量/自动更新？）+ 一个
> 顺带挖出的 P0（装机版根本没有本地 OCR）。**都要做**——都做了。

---

## 一、P0 实锤：装机版没有本地 OCR（不是"要不要随包"，是"说好随包的没接上"）

三层证据（全部实测，非推测）：

1. **作者已拍板随包**：提交 `b87e36c`（10-03，标题「装机版终于有本地 OCR 了」）把
   `ocr-worker` 写进了 `shell/electron-builder.yml` 的 `extraResources`，且它是
   v0.3.0 / v0.4.0 / v0.4.1 三个 tag 的共同祖先 —— 配置早就在了。
2. **CI 从没构建过它**：`scripts/tools/build_ocr_worker.py` 的产物 `build/ocrworker/ocr-worker/`
   是 `extraResources` 的源，但两条打包臂（release.yml 的 installer job、ci.yml 的
   windows-package job）里 **`ocr` 零命中** —— 从没跑过那支脚本。
3. **注释里承诺的防线是假的**：`shell/electron-builder.yml` 里那句「少了这一条
   electron-builder 会当场报找不到源」**不成立**。源目录在 CI 上确实不存在，包却打
   成功了（v0.4.1 = 198 MB，不含 OCR），因为产物核对的判据只有 `Length -lt 60MB`，轻松通过。

**后果**：v0.3.0 至今所有装机版都没有本地 OCR。用户机器实测（10-13，0.4.1）：
安装目录 `resources/` 里只有 `rolecard-backend`，没有 `ocr-worker`；服务页 OCR 那行原文
「本机没有可用的本地 OCR：装机版这一包没带上 ocr-worker」。而云端 OCR 按隐私红线
**绝不默认启用** —— 于是「传图片/报告 → 本地 OCR → 入检索」这条核心卖点，在所有真实
用户那里一直是死的。

**体积账**（本机实测）：`build/ocrworker/ocr-worker` 未压缩 **257.8 MB**（大头是
onnxruntime / cv2 / numpy 的 DLL，模型本身十几 MB）。随包后安装包预估 **~330–380 MB**
（NSIS LZMA 对这类 DLL 约压掉 30–40%）。当前不含 OCR 的包是 198 MB。

---

## 二、更新机制现状（回答三连问）

| 问题 | 现状 | 依据 |
|---|---|---|
| 覆盖安装会找旧安装吗 | 装到固定用户目录、就地覆盖，但每次弹向导（`oneClick: false`） | `shell/electron-builder.yml` 的 nsis 段 |
| 设置里有"检查更新"吗 | **没有**。只有一张 `ShellReleaseCard`，且它只列"本后端目录里有没有产物可下"，**从不比对版本** | `frontend/src/components/ShellReleaseCard.tsx` |
| 自动 / 增量更新 | **完全没有**。`shell/main/` 里无任何 `autoUpdater`；`shell/package.json` 无 `electron-updater` 依赖 | 全仓 grep |

---

## 三、决策

### 3.1 检查更新的事实源 = 双源，注入式

后端新增一个只读端点，比对"当前版本"与"最新版本"。最新版本的来源有两个，按可用性择一：

- **GitHub Releases**（在线，最准）：读 `HCX0426/rolecard-agent` 的 latest release tag。
  需要出网 —— 与 `scripts/tools/audit_deps.py` 同一条纪律：**要出网的判据不进 pytest，
  用注入式 fetcher 离线测**。默认 URL 走 config，可关（`UPDATE_CHECK_ENABLED`）。
- **本地 `shell_release_dir`**（离线，自托管部署方）：扫那份目录里最新产物的版本。

端点返回 `{current, latest, has_update, source, ...}`，**不下载、不安装** —— 只把
"有没有新版"从"没人知道"变成"打开就看到"。

### 3.2 增量更新：对这个项目性价比低，不做专门的增量

electron-updater 的 blockmap 差分只对**逐字节稳定**的部分有效。而本项目的体积大头是
随包后端（PyInstaller 每次重打，哪怕逻辑没变，二进制也常因时间戳/路径全变）——
差分对它基本失效，省不下多少。故**不单独优化增量**，交给 electron-updater 默认行为。

### 3.3 自动更新：做，但守三条红线

桌面壳接 `electron-updater`（GitHub provider）。三条不可让的约束：

1. **升级前备份数据根**：装新版前先把 `%LOCALAPPDATA%\rolecard-agent\` 整目录快照
   （留最近 2 份）。自动 schema 迁移（如嵌入器调序那类）失败的兜底全靠它。
   **落地时与设想有一处偏差（如实记）**：设想复用 `backup_data_root.py` 的
   `sqlite3.backup()`——那是因为它对着**活库**；壳的时序是"先停后端、后备份"，
   库安静下来时 `cpSync` 整目录（连 wal/shm、chroma、uploads）反而更完整，
   且快照是目录形态、还原 = 拷回去。备份失败 = 中止更新（红线本身不变）。
2. **后端干净退出**：更新走 `app.quit()` → `before-quit` → `backend.stop()`
   （`shell/main/backend.ts` 已有，taskkill /T /F 连进程树）。NSIS 覆盖安装时后端
   进程若还活着会文件占用失败 —— 这条必须在下载/安装前确认。
3. **用户确认制，不静默装**：发现新版 → 托盘/界面提示 → 用户点"更新并重启"才下载+安装。
   本项目有真用户数据（SQLite + chroma + 记忆），静默后台升级风险高于收益。

### 3.4 装好/更新完要自动清临时文件（用户 2026-10-13 追加的规矩）

读源码 + 实测把"哪些是临时文件、能不能删"钉成三类，处置各不相同（判据在
`shell/main/updateCacheCleanup.ts`，九条读数由 `shell/scripts/test-update-cache-cleanup.cjs`
真跑，进 full 门禁与 frontend job）：

| 东西 | 处置 | 依据 |
|---|---|---|
| `pending/` 里**已装完**的新版安装包 | **删** | electron-updater 只在"下次下载 sha512 对不上"才清它（`DownloadedUpdateHelper.js:74-82`），装完不清；Windows 的 NsisUpdater 路径**零清理**。不删 = 每次升级永久泄漏一整个包（本项目 ~281 MB） |
| `installer.exe`（差量下载基线） | **留** | NSIS 每次安装自己 `copyFile "$EXEPATH"` 写的（`installer.nsh:93`），是 `differentialDownloadInstaller` 的 oldFile（`AppUpdater.js:668`）；删了下次退化全量重下。且每次安装覆盖、不累积（实测本机这一份 = 今天装的那版，逐字节吻合） |
| 升级前的数据快照 | **不在这里管** | 它是回滚路径不是临时文件，寿命由 `BACKUP_KEEP`（留最近 2 份）定。把"清临时文件"读成"顺手删备份"是要出人命的读法 |

判据 = `pending/update-info.json` 的 `fileName` 里那个版本 **== 当前运行版本**
（update-info.json 没有版本字段，版本住在 fileName 里）。相等 ⇒ 正在跑的就是它 ⇒
"待装"使命结束。缓存目录名**不自己拼**（那是 app-builder-lib 的内部推导），读构建期
写进包内的 `app-update.yml` 的 `updaterCacheDirName` —— 抄一份就是第二个事实面。
保守优先：读不到 json / 解析不出版本 / 版本不是当前 / 删除抛错 —— 一律不动并如实记一行。
在**新版的每一次开机**跑（判据自己会沉默，不需要"是不是升级后第一次"这种记不住的状态）。

---

## 四、OCR 随包修复方案（P0）

1. **两条打包臂都加 OCR 构建步骤**：在 `npm run package` 之前，建独立 `.venv-ocr`、
   装 `requirements/requirements-ocr.txt` + `requirements/requirements-package-ocr.txt`、
   跑 `scripts/tools/build_ocr_worker.py`。位置排在后端 sidecar 之后、electron-builder 之前。
2. **强判据替掉 60MB 弱判据**：electron-builder 的 `dir` target 会产出
   `shell/release/win-unpacked/resources/ocr-worker/ocr-worker.exe`。打包后直接
   `Test-Path` 它，不在就 throw —— 这才兑现"当场报"的承诺。（解 NSIS exe 更重，
   用 unpacked 目录等价且可靠。）
3. **前置闸**：打包前显式断言 `build/ocrworker/ocr-worker` 源目录存在。不依赖
   electron-builder 对缺失 extraResources 源的行为（注释里那句假设已被证伪）。
4. **体积判据同步上调**：60MB 那条改成"含 OCR 后应 > 250MB"级别的下界，作为第二道
   哨兵（读数，不是主判据）。

---

## 五、更新机制实现分解

- **后端**：新增 update-check 路由（config 加 2~3 个键：开关、GitHub 仓库标识、
  release 目录复用现有的 `shell_release_dir`）；版本比较用不引第三方的小函数
  （`packaging` 只在 dev 依赖，运行时不可用）。
- **前端**：`ShellReleaseCard` 旁边加"检查更新"能力（显示当前/最新、有更新时给下载链接）。
  拉不到就整卡不渲染，与现有 D②-4 卡片同一口径。
- **壳**：`shell/package.json` 加 `electron-updater`；`electron-builder.yml` 加
  `publish: {provider: github, owner, repo}`；新增 `shell/main/updater.ts` 接线
  （check / download / quitAndInstall），托盘加"检查更新"条目；升级前调数据备份；
  新版每次开机清已消费的 pending 下载（§3.4，判据在独立的 `updateCacheCleanup.ts`，
  不 import electron 以便真跑测试）。

---

## 六、实施清单（分笔提交）

- [x] A. 设计稿入 `docs/` 并登记 `docs/README.md`（本文件）
- [x] B. P0：两条 workflow 接 OCR 构建 + 强判据 + 前置闸 → `65ff05a`
- [x] C. 后端 update-check 端点 + 离线测试（注入式 fetcher）→ `a37ec67`
- [x] D. 前端"检查更新"卡片 + vitest + dist 重建 → `a37ec67`（与源码同笔入库）
- [x] E. 壳 electron-updater + 数据备份 + 后端干净退出 + 门禁 `update feed parity` → `d556122`
- [x] F. 全量门禁：pytest 全绿 / ruff / mypy 149 文件 / check_consistency **72 条全过**
      （71→72，新增 update feed parity）/ 前端 vitest 402 / shell typecheck+build
- [x] G. 三笔提交已推送（`65ff05a` / `a37ec67` / `d556122`），push 触发的 CI 全绿；
      **P0 三道闸已被 dispatch 打包真跑验证**（run `38408788201`：②b 构建绿 → 前置闸绿 →
      NSIS 绿 → 产物核对「安装包 = 281.54 MB，ocr-worker 与 backend 点名在场，
      latest.yml 在场」）。第一次 dispatch（`38406037773`）红在 NSIS —— 那是
      publish 段让 electron-builder 在无 token 的 job 上尝试上传，`71ec11a` 修成
      `--publish never`（读 PublishManager.js 核实 never 不砍 latest.yml/app-update.yml）
- [x] H. 升级后自动清临时文件（§3.4，用户 10-13 追加）：`updateCacheCleanup.ts` 纯函数 +
      九条真跑读数进 full 门禁与 frontend job；对真实缓存目录实测不误删差量基线

> **一处口径订正**：`65ff05a` 的提交标题写"空包三年"是笔误 —— 受影响的出货是
> v0.3.0（10-09）到 v0.4.1（10-10），约**四天**；缺陷的"年龄"以本稿 §一 的
> 三层证据为准（b87e36c 10-03 起配置在、出货包 10-09 起缺）。已推送的信息不改写，
> 正确事实以本稿为家。

## 七、验收判据

- **P0**：CI 打包后 `win-unpacked/resources/ocr-worker/ocr-worker.exe` 在场（强判据）；
  服务页 OCR 那行不再是"没带上 ocr-worker"。**已达成**（run `38408788201` 点名在场，
  安装包 281.54 MB）。
- **检查更新**：装 0.4.0 的人打开设置→关于，能看到"当前 0.4.0 · 最新 0.4.1，可下载"，
  不必再靠人来问（这正是本轮升级的原始痛点）。**已达成**（隔离后端实打端点：
  `latest=0.4.1 / has_update=false / error=null`，真出网解析 GitHub tag）。
- **自动更新**：托盘"检查更新"→ 有新版则提示 → 用户确认 → 备份数据 → 下载 → 干净退出
  后端 → quitAndInstall。任一步失败都不丢数据（备份在）。装完后新版的每次开机清掉
  已消费的 pending 下载（§3.4），差量基线 `installer.exe` 保留。
- 全部离线测试可跑（出网判据用注入式，断网不红）。

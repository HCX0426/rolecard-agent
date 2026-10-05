# scripts/ 目录索引

> 本目录分三层（2026-10-05 脚本分层那一刀落的结构），顶层只留**门禁与 CI 每趟要跑、
> 或被代码按路径消费**的入口；其余按性质下沉：
>
> - **顶层** —— 门禁步骤、CI 步骤、README 快速开始、装机链与运行时消费的入口（改动需谨慎，路径被多处引用）；
> - [`tools/`](tools/) —— 长期工具：低频但会一直用的（评测、量尺、打包配套、索引重建）；
> - [`forensics/`](forensics/) —— 取证件：一次性探针与现场取证脚本，服务于某一轮审计/事故的验收，留存供复跑；
> - [`js/`](js/) —— Node 驱动族：headless Chrome / 桌面壳的 UI 冒烟与截图。
>
> 纪律：新脚本先问"它是门禁的一步吗？"—— 不是就进 tools/ 或 forensics/，别让顶层重新长胖。
> 取证脚本一律走库副本（scratch_db 那条纪律），一行都不写真库。

## 顶层（门禁 / CI / 装机链 / 运行时消费）

| 脚本 | 用途 | 出处轮次 | 长期 |
|---|---|---|---|
| `scripts/gate.py` | 分层门禁：--fast 日常迭代，全量档提交/发布前跑 | 2026-09-18，用户拍"别每次都跑全量" | 是 |
| `scripts/pytest_with_evidence.py` | pytest 红跑取证包装层，快档与覆盖率档共用 | 2026-10-02 轮（chroma 偶发在册修法） | 是 |
| `scripts/check_consistency.py` | 60+ 条一致性断言（账本哈希、写事务、豁免表、分层…） | 2026-09-22 起，逐轮增补 | 是 |
| `scripts/check_import_layers.py` | 依赖方向契约（import-linter）的运行包装 | 2026-10-04（base/ 收口那一刀） | 是 |
| `scripts/check_readme_numbers.py` | README 首屏数字 ↔ 刚落盘读数的收尾比对 | 2026-09-24 读数收尾流水线 | 是 |
| `scripts/check_bundle_parity.py` | 随包后端 import↔bundle parity + spec 收包清单 | 2026-09-30 打包轮 | 是 |
| `scripts/check_dist_sync.py` | frontend/dist 入库同步检查（本机与 CI 共用一份实现） | 2026-09-29 前端入库轮 | 是 |
| `scripts/check_root_purity.py` | 真库纯度检查：找出被当成用户数据留下的测试夹具行 | 2026-09-28 数据根轮 | 是 |
| `scripts/baseline.py` | 产出 build/baseline.json（全项目"当前有多少"的唯一口径）+ 库列漂移 --check | 2026-10-02 基线轮 | 是 |
| `scripts/init_db.py` | 建 SQLite 库 + 播种（README 快速开始第 1 条） | M4（2026-09 上旬） | 是 |
| `scripts/run_api.py` | 启动管理控制台 + 流式对话服务（README 快速开始第 3 条） | M4（2026-09 上旬） | 是 |
| `scripts/smoke_check.py` | 全功能离线冒烟（14 项），全量门禁末步 | 2026-09-19 门禁提速轮 | 是 |
| `scripts/probe_readme_quickstart.py` | 把 README 快速开始原样执行一次（文档可跑性门禁） | 2026-09-28（裸 uvicorn 那发） | 是 |
| `scripts/build_sidecar.py` | 打随包后端（PyInstaller onedir → build/sidecar/） | 2026-09-26 壳选型轮 | 是 |
| `scripts/ocr_worker.py` | OCR 子进程 worker，**必须**在独立 .venv-ocr 里跑 | 2026-09-22 OCR 栈引入 | 是 |
| `scripts/install_package.ps1` | 本机装机链：备份→装包→图标→ purity 检查 | 2026-09-26 壳选型轮 | 是 |
| `scripts/ps_utf8.ps1` | PowerShell 会话的 UTF-8 预设（GBK 控制台自救） | 2026-09 下旬 | 是 |

## tools/（长期工具，低频）

| 脚本 | 用途 | 出处轮次 | 长期 |
|---|---|---|---|
| `scripts/tools/backup_data_root.py` | 装机前把在用数据根整体备份成 zip | 2026-09-26 装机轮（装机链第 2 步） | 是 |
| `scripts/tools/build_audit_index.py` | 生成 docs/架构审计索引.md（编号当身份，位置只是存放地） | 2026-09-25 审计轮 | 是 |
| `scripts/tools/build_ocr_worker.py` | 打随包 OCR worker（PyInstaller onedir） | 2026-10-03 装机版本地 OCR | 是 |
| `scripts/tools/make_app_icon.py` | 重生成应用图标（多帧 RGBA .ico） | 2026-10-03 图标糊底修复 | 是 |
| `scripts/tools/make_pet_sheet.py` | 生成随包桌宠形象素材（sprite.png + pack.json） | 2026-09-28 形象层（自绘默认包） | 是 |
| `scripts/tools/measure_latency.py` | 首字/完整回答 P95 量尺（自起隔离实例） | 2026-09-26 延迟验收 | 是 |
| `scripts/tools/persona_meter.py` | 活人感客观度量（只读、不跑模型） | 2026-09-25 主动消息设计稿 | 是 |
| `scripts/tools/persona_chat_sim.py` | 日常闲聊八轮仿真（真模型、库副本） | 2026-09-25 主动消息设计稿 | 是 |
| `scripts/tools/persona_ab.py` | 活人感 A/B（同模型同历史，只差一个改动） | 2026-09-25 主动消息设计稿 | 是 |
| `scripts/tools/run_eval.py` | 评测跑批（可复现通过率基线 + 数据引用正确率） | P3 出口条件（2026-09-14 首跑） | 是 |
| `scripts/tools/seed_demo_data.py` | 给演示库注入虚构演示数据 | M4（2026-09 上旬） | 是 |
| `scripts/tools/reachout_outcomes.py` | 主动开口"结局"度量（接话率/连击/看了不接） | 2026-09-26 轮（结局可量化那格） | 是 |

## forensics/（取证件：一次性探针与现场取证）

| 脚本 | 用途 | 出处轮次 | 长期 |
|---|---|---|---|
| `scripts/forensics/scratch_db.py` | 把真库复制成可写副本 —— 一切实验脚本的共用底座 | 2026-09-25 审计轮（实验只走副本） | 是（取证基建） |
| `scripts/forensics/chroma_flake_evidence.py` | chroma 偶发的现场取证（命中签名那一刻钉证据） | 2026-10-02 轮（与红跑取证配套） | 是（取证基建） |
| `scripts/forensics/smoke_chat.py` | M4 聊天 SSE 的活模型冒烟（已被 smoke_check 取代大半） | M4（2026-09 上旬） | 留档 |
| `scripts/forensics/probe_chat_ui.py` | 对话页拆分的真机交互探针驱动 | 2026-10-01 对话页拆分轮 | 留档 |
| `scripts/forensics/probe_checkpoint_prune_safety.py` | 检查点修剪开/关的安全对减 | 2026-09 下旬修剪轮 | 留档 |
| `scripts/forensics/probe_cross_window_sync.py` | 双窗同步两种界面算法的落后量实测 | 2026-09-26 轮（桌宠慢 12 秒那条） | 留档 |
| `scripts/forensics/probe_data_source_switch.py` | 数据源切换 + 上行同步端到端（两实例） | M5/M7（2026-09 中下旬） | 留档 |
| `scripts/forensics/probe_local_ttft.py` | 本地 8B 首字延迟递增的归因探针 | 2026-09-26 轮 | 留档 |
| `scripts/forensics/probe_memory_vs_scan.py` | 记忆注入 vs 未收尾话题扫描是否重复的 A/B 判定 | 2026-09-26 轮 | 留档 |
| `scripts/forensics/probe_open_threads_live.py` | 未收尾话题的实机验收（真模型三份对话） | 2026-09-26 轮 | 留档 |
| `scripts/forensics/probe_open_threads_reach.py` | 未收尾话题的可达性验收（生产读数出处） | 2026-09-26 轮 | 留档 |
| `scripts/forensics/probe_package_artifact.py` | 打包后不装自证"这一包是这一版"（三路判据） | 2026-09-30 打包轮 | 留档 |
| `scripts/forensics/probe_stop_live.py` | 停止生成的实机端到端（库副本，不写真库） | 2026-09 下旬停旗轮 | 留档 |
| `scripts/forensics/probe_stop_marker.py` | 被叫停半句落库带标记的真链路驱动 | 2026-09-26 轮 | 留档 |
| `scripts/forensics/probe_sync_push.py` | 同步三方向两实例端到端（上行+下行+对账） | M7/M8（2026-09 下旬） | 留档 |

## js/（Node 驱动族）

| 脚本 | 用途 | 出处轮次 | 长期 |
|---|---|---|---|
| `scripts/js/ui_smoke.js` | 真机 UI 冒烟（smoke_check 的 UI 段与手动两用） | 2026-09-19 门禁提速轮 | 是 |
| `scripts/js/shell_quit_smoke.js` | 桌面壳关窗收起的真机冒烟 | 2026-09-26 壳选型轮 | 是 |
| `scripts/js/probe_chat_ui.js` | headless Chrome 交互步骤（probe_chat_ui.py 的驱动） | 2026-10-01 对话页拆分轮 | 留档 |
| `scripts/js/render_shots.js` | 对隔离实例拍五张界面截图 | 2026-09-26 审计轮 | 留档 |
| `scripts/js/ui_data_source_switch.js` | 数据源切换的界面步骤驱动 | M5（2026-09 中旬） | 留档 |

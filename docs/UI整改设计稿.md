# UI 整改设计稿（P2 余下三项 · 待用户拍板）

> 关联：`docs/架构审计.md` §4（界面分组）、§5（设计合理性 & 冗余）、§9/§10（P2 余下）。
> 状态：本稿只出设计，**未动任何代码**。规则：用户点头后才进实施（每批单独提交、文档回写同提交）。
> 方法：所有结论带 `文件:行号` 锚点，已逐一对照当前代码（不是照抄审计旧行号）。

---

## 0. 范围

三项（用户 2026-09-20 列明）：

1. **UI 原语迁移**：补 `Card` / `Modal` 两个缺的原语，并把 `Toast` 升级成全局；渐进迁移现有散落实现。
2. **界面分组重划**（审计 §4）：拆掉「通用」杂物抽屉，按语义重划设置页签；每个 `*_enabled` 只留一处可写。
3. **`supports_tools` 对话页可见**：在对话页模型菜单补「工具」能力徽章（与现有「视觉」徽章同形）。

---

## 1. 现状盘点（带锚点，作为改动落点）

### 1.1 原语覆盖缺口

| 原语 | 现状 | 缺口证据 |
|---|---|---|
| `Switch` | `ui/Switch.tsx` 已有，但**仅 PluginsPage:125 用** | SettingsPage:1007/1015 用裸 `<input type="checkbox">`（`e.target.checked`）管 `supports_vision`/`supports_tools`；ExtensionPanel 也用裸 checkbox（审计 §5「启停三副面孔」） |
| `Card` | **不存在** | 卡片容器手写成 `rounded-xl border … bg-white dark:bg-slate-800 p-4` 散落：KnowledgePage:102/147/203、PluginsPage:100、LocalServiceCard:108、DataPage:157/186/642、SettingsPage 各 section（289/318/383/405/537/563）、App 错误边界:128 |
| `Modal` | **不存在** | 二次确认 7 处副本 + 1 处 `window.confirm`（详见 §1.2） |
| `Toast` | 顶层 `Toast.tsx` 有 `useToasts`/`ToastStack`，但**每页自带、容器需 `relative`** | 仅 ChatPage:159 用；其余页用 `Notice` 行内条；无全局栈。且 Toast.tsx:43 `info` 那行 `text-slate-300 dark:text-slate-600` 与 §5 点名的深色态冲突同类 |

### 1.2 二次确认副本清单（→ 全部收口进 `Modal` + `useConfirm`）

| # | 位置 | 语义 |
|---|---|---|
| 1 | ChatPage:591/605 | 删除会话（行内 `confirmDel === s.thread_id`） |
| 2 | useMessageSelection.ts:16/45 | 多选消息删除（`confirmDelete`） |
| 3 | ExtensionPanel:431/436 | 删除扩展（`confirmDel === s.id`） |
| 4 | DataPage:195/204 | 删除报告 |
| 5 | DataPage:296/305 | 删除索引 |
| 6 | DataPage:678/681 | 删除另一类记录 |
| 7 | RolesPage:85 | 删除角色 |
| 8 | KnowledgePage「清空作用域」 | 破坏性重建二次确认（注释:202 明说"先盘点 → 二次确认 → 执行"） |
| 9 | SettingsPage:164 | `window.confirm("当前作用域有未保存的修改，切换会丢弃，继续？")` 丢草稿 |

> 注：第 8 条的"盘点 → 确认 → 执行"是**有意的分步破坏性操作**，不等同于简单删除二次确认；迁移时保留"先展示待删清单"再确认的结构，不要退化成一句空确认。

### 1.3 界面分组（审计 §4）

- 顶层 TAB（App.tsx:39）：对话 / 数据 / 知识库 / 角色卡 / 插件 / 设置 —— 6 个，与文档一致。
- 设置内 TAB（SettingsPage:21）：通用 / 模型 / 服务 / 扩展 / 运行环境 / 审计。
- **「通用」= 杂物抽屉**（SettingsPage:289–563）：外观(289) / 关于(308) / 跨会话记忆(318) / 主动开口(383) / 任务目录(405) / 系统状态(537) / 演示数据(563)。
- **主动开口总闸两入口**（§4 点名）：通用卡(383) vs 运行环境 reachout 组。
- 模型页已瘦身（P0-2 删了默认/回退链 radio），但 `supports_vision/tools` 仍裸 checkbox（1007–1020）。

### 1.4 已落地、不要再碰的前提（避免误改/重复造轮）

- ChatPage 已不再自造窄化类型，直接吃 `api.ts` 的 `BackendRow`（P1-3 后端闸门已落地，`supports_tools` 执行侧已拦）。
- `ui/` 已有 Button/Notice/PageHeader/Switch/Tag；`Toast.tsx` 已有 `useToasts`/`ToastStack` —— **从现有升级，不是从零建**。
- P0-2 已删模型页默认/回退链 radio；P1-5 已收口 OCR/RAG 选型到服务页一处。
- **num_ctx 归属冲突已决**：§4 重划建议①写"模型页管 num_ctx"，但 §8-6 回写已定"num_ctx 归对话页"（当前代码：`chatRows` 筛 chat 后端、对话页模型菜单 `setModelCtx` 设每后端 num_ctx）。**本稿遵循回写决定**：模型页只管「后端 CRUD + 能力位 + 常驻」，num_ctx 不回流。

---

## 2. 设计

### 2.1 UI 原语迁移

#### 2.1.1 `Card`（新 `ui/Card.tsx`）

```tsx
<Card title? hint? actions? className?>{children}</Card>
```
- 标题区统一 `<h3>` 观感（复用 `PageHeader` 的标题字号/字重），`actions` 落右侧（放开关/按钮）。
- 暗色态一刀切统一：`border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800` —— 顺手消掉 §5 深色态冲突里"卡片"这一类。
- **渐进迁移**：先改 SettingsPage 各 section + KnowledgePage/DataPage/PluginsPage 的列表卡片；`LocalServiceCard`、App 错误边界等同观感即可，不强改。
- 只换外层容器 className，不动内部逻辑；每个文件单独小提交。

#### 2.1.2 `Modal` + `useConfirm`（新 `ui/Modal.tsx` + `hooks/useConfirm.ts`）

- `Modal`：受控 `<Modal open onClose title>` + footer 插槽；ESC / 点遮罩关闭；基础焦点管理；暗色态一致。
- `useConfirm()`：返回 `confirm(opts) => Promise<boolean>`，内部用 portal 在 body 渲染一个全局 Modal；A11y：默认焦点在「取消」、文案必须是问句、`danger` 态可配。
- **迁移 §1.2 的 9 处**：破坏性删除统一 `useConfirm({ title, body, confirmText: "删除", danger: true })`；SettingsPage:164 丢草稿用 `useConfirm`（更好：可展示"将丢弃的内容摘要"）。
- ⚠️ **主要改造面**：`window.confirm` 是同步阻塞，改 `useConfirm` 后变异步。所有调用点从 `if (!confirm(...)) return` 改成 `if (!(await confirm(...))) return` 或 `.then`。这是本项最大的代码面，需在评审时确认接受度（见 §4-C）。
- 测试：`useConfirm` 用 jsdom 模拟点击；每处改完补「点确认才执行 / 点取消不执行」。

#### 2.1.3 全局 `Toast`（升级 `Toast.tsx` → Provider）

- 现状：ChatPage 自带栈、容器需 `relative`；其余页无。
- 方案：`ToastProvider` 挂在 App 根（App.tsx:146 内、`Suspense` 之上），context 暴露 `useToast()`；`ToastStack` 仅在根渲染一次（视口级 `fixed`，不再要求调用方 `relative`）。
- ChatPage 从本地 `useToasts` 改为 `useToast()`；`setStatus` 包装保留，**既有调用点零改**。
- 顺手修 Toast.tsx:43 的 `text-slate-300 dark:text-slate-600` 深色态冲突。
- 风险：context 须覆盖所有 lazy 页（ChatPage/DataPage/… 均 `lazy`），Provider 在 App 根即可覆盖。

### 2.2 界面分组重划（§4）

#### 2.2.1 新设置页签结构

| 原 | 新 | 说明 |
|---|---|---|
| 模型 | **模型** | 后端 CRUD + 能力位（Switch 化）+ 常驻（LocalServiceCard 顶部）；num_ctx 不回流（§1.4） |
| 服务 | **服务** | 运行时状态 / 降级；默认与回退链**唯一入口**（P0-2 已留） |
| 通用（杂物抽屉） | **记忆与任务目录**（新帖） | 跨会话记忆 + 角色专属记忆 + 任务目录 + **主动开口总闸**（agent 行为集中） |
| 通用（杂物抽屉） | **关于与系统状态**（新帖） | 外观 + 关于 + 系统状态 + 演示数据 |
| 扩展 / 运行环境 / 审计 | 不变 | — |

#### 2.2.2 主动开口总闸收口（§4 两入口）

- 保留**一处可写**：建议放「记忆与任务目录」帖（与记忆同属"agent 行为"），运行环境里的 reachout 组降级为**只读展示当前生效值**。
- 消除 §4「通用卡 vs 运行环境」双入口（数据同走 `reachout_enabled` runtime 覆盖，只是 UI 重复，不是数据分叉 —— 与 §3.1 `memory_enabled` 同性质）。

#### 2.2.3 `Switch` 统一（§5 收口）

- SettingsPage:1007–1020 的 `supports_vision/tools` 裸 checkbox 改用 `ui/Switch`。
- 这是「启停三副面孔」的最后一块（PluginsPage 已用 Switch）。

#### 2.2.4 `*_enabled` 单写点盘点（§4③）

- 主动开口：本节 2.2.2 处理。
- `web_search_enabled` / `run_tools_enabled` / `memory_enabled`：确认各自只有一处可写（memory 见 §3.1 已是单存储单写点；web_search/run_tools 需实施时 grep 复核，若有多入口则同样收口）。

### 2.3 `supports_tools` 对话页可见

- 现状：ChatPage 模型菜单渲染「视觉」徽章(1159)，`BackendRow.supports_tools` 已可取得（P1-3 执行侧闸门已落地）。
- 方案：在 1159 视觉徽章旁补「工具」徽章，`{b.supports_tools && (<span …>工具</span>)}`，与视觉同形换色（indigo/sky）。
- **语义：纯展示能力位提示，不复制后端判定**（§5/§11.1 的"判定在后端一处，界面复制一份就是第二个事实面"——这里只展示、不 disable、不改行为）。
- 测试：ChatPage.test 加「`supports_tools=true` 的后端在菜单显示「工具」徽章 / `false` 不显示」。

---

## 3. 实施顺序与门禁（沿用既有工作流）

每批：快门禁 + 受影响测试；一批做完统一跑一次全套；文档回写跟代码同一提交。

1. **`Switch` 统一**（最小、先消三副面孔）：SettingsPage 裸 checkbox → `ui/Switch`。
2. **`Card` 迁移**：SettingsPage + KnowledgePage/DataPage/PluginsPage 列表卡片。
3. **`Modal` + `useConfirm`**：收口 §1.2 的 9 处（含 SettingsPage:164 的 `window.confirm` 异步化）。
4. **全局 `Toast`**：`ToastProvider` 挂 App 根，ChatPage 切换；修深色态冲突。
5. **设置拆帖 + 主动开口收口**：新增两帖、迁移 section、运行环境 reachout 改只读。
6. **`supports_tools` 徽章**：ChatPage 模型菜单补「工具」。

> 第 3、4 项涉及调用点行为变化（异步化、context 依赖），建议各自独立成批、独立评审。

---

## 4. 待拍板的点（2026-09-20 已全部按推荐采纳，进入实施）

> 用户拍板：A 做 `ToastProvider`；B 拆帖命名用「记忆与任务目录」/「关于与系统状态」；
> C `useConfirm` 异步化（接受改造面）；D `Card` 仅迁移设置/知识/数据三页（LocalServiceCard、App 错误边界保持）；
> E 主动开口总闸归「记忆与任务目录」帖，运行环境改只读。

- **A. 全局 Toast 是否做 Provider**：我建议做（审计原文"全局 Toast"），但也可保持每页自带、只修深色态冲突。请确认。
- **B. 设置拆帖方案**：两新帖命名（「记忆与任务目录」/「关于与系统状态」）是否合适？还是低频项（关于/演示数据）合并回一个轻帖？
- **C. `useConfirm` 异步化改造面**：§1.2 的 9 处调用点要从同步 `if (!confirm)` 改异步 `if (!(await confirm))`，改造面最大。接受度？
- **D. `Card` 迁移范围**：全量（含 LocalServiceCard/App 错误边界）还是仅设置/知识/数据三页？
- **E. 主动开口总闸归属**：放「记忆与任务目录」帖（建议）还是留在「运行环境」帖？

---

## 5. 实施进度

| # | 批次 | 状态 | 提交 |
|---|---|---|---|
| 1 | `Switch` 统一（SettingsPage 裸 checkbox → `ui/Switch`） | ✅ 完成 | 2026-09-20 |
| 2 | `Card` 迁移（新增 `ui/Card.tsx`，设置/知识/数据三页） | ✅ 完成 | 2026-09-20 |
| 3 | `Modal` + `useConfirm`（收口 9 处二次确认 → 实际覆盖 10 个调用点：ExtensionPanel / ServicesPanel / DataPage（报告+索引 2 处）/ KnowledgePage 清空作用域 / RolesPage / useMessageSelection / ChatPage（删除对话+多选删除 2 处）/ SettingsPage 切作用域） | ✅ 完成 | 2026-09-20 |
| 4 | 全局 `Toast`（`ToastProvider` 挂 App 根） | ✅ 完成 | 2026-09-20 |
| 5 | 设置拆帖 + 主动开口收口 + `*_enabled` 单写点 | ✅ 完成 | 2026-09-20 |
| 6 | `supports_tools` 对话页「工具」徽章 | ✅ 完成 | 2026-09-20 |

> 六批全部落地（2026-09-20）：Switch 统一 → Card 迁移 → `Modal`+`useConfirm` → 全局 `Toast` → 设置拆帖+主动开口单写点 → `supports_tools` 徽章。

每批单独提交，文档回写同提交。

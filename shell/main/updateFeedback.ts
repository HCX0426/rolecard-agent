/**
 * 更新链的纯判据（审查 A-6 / A-8 的收口）。electron 接线在 `updater.ts`。
 *
 * 为什么抽成不 import electron 的独立模块（本仓既有姿势：`updateCacheCleanup.ts`、
 * `petDockGeometry.ts` 同规）：shell 没有测试框架、只有 tsc，而这些判据恰好是
 * typecheck 守不住的 —— 类型全对、一次失败弹两条 toast 正是这次的症状。
 *
 * ⚠ 本模块**只管这几条判据**，不管别的 toast：`update-available`（"发现新版本 X"）与
 * `update-downloaded`（"新版本已下载"）的文案在接线处各自成句（要带版本号、要带指引，
 * 而且 downloaded 不分来源都要出一声）。把它们也塞进判据函数会造出**两个 toast 的主人**
 * —— 那正是本模块要消灭的形状，我第一版就差点自己制造一个。
 */

/** 用户手点的那一轮处在哪个阶段；null = 开机那次顺手查（静默档，一声不出）。 */
export type ManualAction = "check" | "download" | null;

/**
 * 失败读数（A-8 的病根）。
 *
 * 前提从 electron-updater 编译产物坐实：`AppUpdater.js:271`（检查）与
 * `:439/:453 → :477`（下载）都是**先 `emit("error")`、再 throw**。所以同一次失败
 * 有两个认领者 —— `error` 监听器与调用方的 `.catch` 各弹一条 toast，用户点一次
 * 「检查更新」、断网时收到两条同名 toast。
 * 收口方式：**失败 toast 与状态读数只有监听器一个主人**；`.catch` 退化成
 * "记一条带 source 的日志 + 吞掉 rejection 防未捕获崩溃"，不再 notify。
 *
 * 第二个症状一起治：上下文从前是个布尔（`manualCheck`），手点检查成功后不清
 * （那是事实：接下来用户要点下载），于是**下载**失败时监听器拿着旧上下文弹
 * "检查更新失败" —— 检查明明成功了，文案在说谎。现在上下文带阶段
 * （见 `advanceStageOnDownload`），`failureOf` 按阶段给读数与消费。
 *
 * `action === null`（开机那次顺手查）保持静默：一声不出、只进状态行 —— 这条规则
 * 从前就有，这里原样搬进来，不改语义。
 *
 * 单主人换来的一个边界，写清楚免得下轮误判成 bug：`AppUpdater.js:451-457` 把
 * `dispatchError` 包在 try 里 —— 监听器链上有人抛时上游只 `_logger.warn`，然后
 * reject 那路被我们的 `.catch` 收成一条日志。这不丢**状态**读数：接线处的监听器
 * 先 `setState` 后 notify，执行到 notify 时状态已经落对了；可能丢的只是那一声
 * toast，而 notify 当前不会抛（Electron `Notification.show()`）。将来若给 toast
 * 加降级路径（比如托盘不可用时改走别处），要知道那个降级抛异常时上游不兜，而
 * `autoUpdater.logger = null` 映射到 NoOpLogger（AppUpdater.js:66 实测）——
 * 那一声 warn 也到不了 shell.log。真要把边界收掉，正确做法是让 toast 自身可观测，
 * 而不是在 reject 那一路重演一次读数（那又回到两个主人）。
 */
export type FailureReport = {
  /** 该不该出 toast（静默档不出）。 */
  shouldNotify: boolean;
  /** 报哪个阶段 —— 只有 shouldNotify 为真时有意义。 */
  stage: "check" | "download";
  /** 这一拍要不要消费掉手点上下文（不消费的话，一个陈旧的上下文会让之后的静默事件也弹窗）。 */
  consumesAction: boolean;
};

export function failureOf(action: ManualAction): FailureReport {
  if (action === null) return { shouldNotify: false, stage: "check", consumesAction: false };
  return { shouldNotify: true, stage: action, consumesAction: true };
}

/** 失败标题按阶段分：从前两种都写"检查更新失败"，下载失败被冒名成检查。 */
export function failureTitle(stage: "check" | "download"): string {
  return stage === "check" ? "检查更新失败" : "下载更新失败";
}

/**
 * 下载已开始 ⇒ 把手点上下文从 check **推进**到 download。
 *
 * 为什么需要这一步：`update-available` 之后上下文**故意不清**（用户接下来要点「下载更新」，
 * 那一轮的成败是他要的答案，而报哪个阶段靠它）。但下载未必是用户点的 ——
 * `autoDownload=true` 时检查成功就直接开始下载，谁都没点过第二下。此时若不推进阶段，
 * 下载失败会拿着旧的 `"check"` 报"检查更新失败"：**检查明明成功了，说谎没治全**。
 *
 * 客观证据是 `download-progress` 事件本身：一旦开始下载，后续失败就属于下载阶段，
 * 无论这一轮是谁发起的。
 *
 * 静默档必须保持静默：`null`（开机顺手查 + autoDownload）不该因为开始下载就变成
 * "用户要答案" —— 那会让自动下载失败弹出打扰人的 toast，而用户从没点过任何东西。
 */
export function advanceStageOnDownload(action: ManualAction): ManualAction {
  return action === "check" ? "download" : action;
}

/**
 * "已是最新"是不是这一轮的终态（A-8 同族的另一半）。
 *
 * 手点检查得到"没有更新"要回执（10-14 那条反馈：点了没反应）；开机那次不该弹。
 * 这是检查的**终态**，所以送达即消费上下文。下载阶段拿到 update-not-available
 * 不是这一拍的终态（那件事不是用户等的答案），null 更不是。
 */
export function isCheckTerminal(action: ManualAction): boolean {
  return action === "check";
}

/**
 * 退出时自动装的交接判据（审查 A-6）。
 *
 * `before-quit` 为了把退出交给安装器调了 `preventDefault()`。可 `doInstall` 的两条
 * 中止路（备份抛错、端口没空出来）从前只 `return` —— 没有任何人再触发一次退出。
 * 用户点「退出」看到的是：一个错误框，然后应用照旧活着，**既没退也没装**；而
 * `installing` 已复位，下一次退出会再走同一条死路（可重复）。
 *
 * 交接成立 ⇔ 开关开着 **且** 有下载好的等着装 **且** 这一轮没在装。
 * 不成立就照常退出 —— 这条同时挡住死循环：`quitAndInstall` 自己会再触发一次
 * `before-quit`，那时 `installing` 已置位 ⇒ 判 false ⇒ 走普通收尾。
 * 中止路上调 `app.quit()` 也不会循环：两条中止路都先 `setState({kind:"error"})`，
 * 而这里的第二条判据（kind 必须是 ready）当场不成立。
 */
export type QuitHandoffInput = {
  autoInstallOnQuit: boolean;
  /** 更新状态机的 kind（只有 "ready" 代表"有下载好的等着装"）。 */
  stateKind: string;
  installing: boolean;
};

export function shouldHandoffQuitToInstall(input: QuitHandoffInput): boolean {
  return input.autoInstallOnQuit && input.stateKind === "ready" && !input.installing;
}

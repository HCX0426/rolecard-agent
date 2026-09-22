/**
 * 桌面壳能力的前端入口（Electron 预加载脚本注入的 `window.rolecardShell`）。
 *
 * 为什么要有这一层：同一份 `dist` 既跑在浏览器里（B/S）也跑在壳里（C/S），而壳能力**在
 * 浏览器里不存在**。所以每次用到都显式判空（不缓存、不在模块加载时定格），壳没注入就是一
 * 条都不做事 —— 界面行为不该分叉，只有"要不要拍系统通知 / 点气泡能不能拉起主窗"这两件
 * 事由它决定。
 */

/** 壳对"这个 Ollama 是不是我起的"的回答。**"在不在跑"不在这里** —— 那是
 *  `/api/local-service` 的事实，两处各说一件事，才不会互相打脸。 */
export interface OllamaOwner {
  managed: boolean;
  pid: number | null;
  binary: string | null;
}

export interface ShellAttempt {
  ok: boolean;
  reason?: string;
}

export interface ShellBridge {
  backendUrl(): Promise<string>;
  backendReachable(): Promise<boolean>;
  /** 让壳把控制台带到前台并打开这个会话。 */
  openSession(threadId: string): void;
  /** 拍一条系统通知；点通知 = 打开 `threadId`。 */
  notify(title: string, body: string, threadId?: string | null): void;
  /** 桌宠悬停展开 / 收起：页面只说要不要摊开，尺寸与落点由壳按工作区算（§7.2）。 */
  setPetExpanded(expanded: boolean): Promise<boolean>;
  /** 拖动桌宠：只报**增量**。绝对坐标等于让页面决定"窗口摆在哪"，那条权力留在壳里。 */
  movePetBy(dx: number, dy: number): void;
  /** 松手那一刻让壳判要不要吸到边上（§7.6）。**零参数、只报"放手了"**：哪条边、藏多深
   *  由壳按当前工作区量。可选 —— 旧壳没这个方法就是不吸，不该因此报错。 */
  petDragEnd?(): void;
  /** 托盘「显示消息内容」那面旗子。**页面只能读**：写它的是用户手里的托盘，不是界面自己。
   *
   * 标成可选是因为一条真实的错身：`dist` 由后端随时更新，而安装包是偶尔才重打一次的 ——
   * **新界面完全可能跑在旧壳里**。当成必选就是拿一个 tsc 里的假设，换桌面上一次
   * `undefined is not a function`（整个桌宠页白屏）。缺这能力时页面按"显示"渲染。 */
  petContentVisible?(): Promise<boolean>;
  /** 旗子被改动时收一次通知；传 null 注销。首值仍需 pull 一次。同样可选（旧壳没有）。 */
  onPetContentVisible?(handler: ((visible: boolean) => void) | null): void;
  /** 登记"谁来接收壳发来的打开会话"；传 null 注销。 */
  onRequestOpenThread(handler: ((threadId: string) => void) | null): void;
  /** 本地推理服务进程的归属与起停（D③-b）。**都不带参数**：路径由壳自己决定。 */
  ollamaOwner(): Promise<OllamaOwner>;
  startOllama(): Promise<ShellAttempt>;
  stopOllama(): Promise<ShellAttempt>;
  /** 系统目录选择器（D②-6）。取消 = null。**页面不给路径**，所以这不是一条"任意路径"口子。 */
  pickDirectory(): Promise<string | null>;
}

declare global {
  interface Window {
    rolecardShell?: ShellBridge;
  }
}

/** 壳能力；B/S（纯浏览器打开）时是 null。 */
export function shellBridge(): ShellBridge | null {
  return window.rolecardShell ?? null;
}

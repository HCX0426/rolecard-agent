/**
 * 桌面壳能力的前端入口（Electron 预加载脚本注入的 `window.rolecardShell`）。
 *
 * 为什么要有这一层：同一份 `dist` 既跑在浏览器里（B/S）也跑在壳里（C/S），而壳能力**在
 * 浏览器里不存在**。所以每次用到都显式判空（不缓存、不在模块加载时定格），壳没注入就是一
 * 条都不做事 —— 界面行为不该分叉，只有"要不要拍系统通知 / 点气泡能不能拉起主窗"这两件
 * 事由它决定。
 */

export interface ShellBridge {
  backendUrl(): Promise<string>;
  backendReachable(): Promise<boolean>;
  /** 让壳把控制台带到前台并打开这个会话。 */
  openSession(threadId: string): void;
  /** 拍一条系统通知；点通知 = 打开 `threadId`。 */
  notify(title: string, body: string, threadId?: string | null): void;
  /** 登记"谁来接收壳发来的打开会话"；传 null 注销。 */
  onRequestOpenThread(handler: ((threadId: string) => void) | null): void;
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

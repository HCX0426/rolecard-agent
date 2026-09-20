/**
 * 开机自启（只在打包态提供）。
 *
 * 为什么 dev 下不给这个开关：`setLoginItemSettings` 写进登录项的是**当前这个可执行文件**，
 * 开发态它就是 `node_modules/electron/dist/electron.exe` —— 用户会在登录项里留下一条
 * 指向依赖目录的假启动项，而且它不在任何安装记录里，卸载也带不走它。
 *
 * 顺带带上 `--autostart`：开机自启的语义是"桌宠在桌面上待着"，不是"再弹一扇控制台盖住
 * 你刚打开的工作"。
 */
import { app } from "electron";

export const AUTOSTART_FLAG = "--autostart";

export function canManageLoginItem(): boolean {
  return app.isPackaged;
}

export function loginItemEnabled(): boolean {
  return canManageLoginItem() && app.getLoginItemSettings().openAtLogin;
}

export function setLoginItemEnabled(on: boolean): void {
  if (!canManageLoginItem()) return;
  app.setLoginItemSettings({ openAtLogin: on, args: on ? [AUTOSTART_FLAG] : [] });
}

/** 这次启动是不是"开机自启"带起来的（决定控制台要不要显示出来）。 */
export function startedByLoginItem(argv: string[] = process.argv): boolean {
  return argv.includes(AUTOSTART_FLAG);
}

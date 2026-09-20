/**
 * 托盘：控制台收起来之后，用户唯一的入口就在这里，所以它必须一直存在。
 *
 * 「关闭控制台」= 隐藏而不是退出（桌宠与后端继续活着）—— 这是一只宠物，
 * 关掉窗口不该让它消失；真要退出走托盘的「退出」，那条路会回收后端进程。
 */
import { Menu, MenuItem, Tray, app, nativeImage } from "electron";
import path from "node:path";

const ICON = path.join(__dirname, "..", "..", "build", "icon.ico");

export type TrayControls = {
  showConsole: () => void;
  petVisible: () => boolean;
  setPetVisible: (visible: boolean) => void;
  /** 开机自启只在打包态存在（dev 下写登录项 = 留一条指向 node_modules 的假启动项）。 */
  canAutostart: () => boolean;
  autostartEnabled: () => boolean;
  setAutostart: (on: boolean) => void;
};

export type TrayHandle = { tray: Tray; refresh: () => void };

export function createTray(controls: TrayControls): TrayHandle {
  const icon = nativeImage.createFromPath(ICON);
  if (icon.isEmpty()) {
    // 打包态最容易踩的就是这条：图标文件没进 asar ⇒ 托盘变成一个"看不见的入口"，
    // 而收起主窗之后它是用户唯一的出路。宁可在这里说一句，也别让它安静地空白。
    console.error(`[shell] 托盘图标读不出来（${ICON}）→ 托盘会是空白，收进 asar 后重试`);
  }
  const tray = new Tray(icon);
  tray.setToolTip("rolecard-agent");

  const render = () => {
    tray.setContextMenu(
      Menu.buildFromTemplate([
        { label: "显示控制台", click: () => controls.showConsole() },
        {
          label: "桌宠在桌面",
          type: "checkbox",
          checked: controls.petVisible(),
          click: (item) => {
            controls.setPetVisible(item.checked);
            render(); // 勾选态由这次操作决定：立刻重画，托盘就不会短暂说谎
          },
        },
        // 不给一个"看得见但永远无效"的条目：开发态整条不出现。
        ...(controls.canAutostart()
          ? [
              {
                label: "开机自启",
                type: "checkbox" as const,
                checked: controls.autostartEnabled(),
                click: (item: MenuItem) => {
                  controls.setAutostart(item.checked);
                  render();
                },
              },
            ]
          : []),
        { type: "separator" as const },
        // 唯一的退出入口：它触发 before-quit，那里负责回收本壳 spawn 的后端。
        { label: "退出 rolecard-agent", click: () => app.quit() },
      ]),
    );
  };
  render();
  tray.on("double-click", () => controls.showConsole());
  return { tray, refresh: render };
}

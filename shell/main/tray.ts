/**
 * 托盘：控制台收起来之后，用户唯一的入口就在这里，所以它必须一直存在。
 *
 * 「关闭控制台」= 隐藏而不是退出（桌宠与后端继续活着）—— 这是一只宠物，
 * 关掉窗口不该让它消失；真要退出走托盘的「退出」，那条路会回收后端进程。
 */
import { Menu, Tray, app, nativeImage } from "electron";
import path from "node:path";

const ICON = path.join(__dirname, "..", "..", "build", "icon.ico");

export type TrayControls = {
  showConsole: () => void;
  petVisible: () => boolean;
  setPetVisible: (visible: boolean) => void;
};

export type TrayHandle = { tray: Tray; refresh: () => void };

export function createTray(controls: TrayControls): TrayHandle {
  const tray = new Tray(nativeImage.createFromPath(ICON));
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
        { type: "separator" },
        // 唯一的退出入口：它触发 before-quit，那里负责回收本壳 spawn 的后端。
        { label: "退出 rolecard-agent", click: () => app.quit() },
      ]),
    );
  };
  render();
  tray.on("double-click", () => controls.showConsole());
  return { tray, refresh: render };
}

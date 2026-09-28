/**
 * 托盘：控制台收起来之后，用户唯一的入口就在这里，所以它必须一直存在。
 *
 * 「关闭控制台」= 隐藏而不是退出（桌宠与后端继续活着）—— 这是一只宠物，
 * 关掉窗口不该让它消失；真要退出走托盘的「退出」，那条路会回收后端进程。
 */
import { Menu, MenuItem, Tray, app, nativeImage } from "electron";
import path from "node:path";

import type { PetPrefs } from "./state";

const ICON = path.join(__dirname, "..", "..", "build", "icon.ico");

/** 透明度档位：照 Cherry Studio 那样给几档挑，而不是拖滑块 —— 托盘菜单里放滑块要再造一扇窗。 */
export const OPACITY_STEPS: { label: string; value: number }[] = [
  { label: "100%", value: 1 },
  { label: "85%", value: 0.85 },
  { label: "70%", value: 0.7 },
  { label: "55%", value: 0.55 },
];

export type TrayControls = {
  showConsole: () => void;
  petVisible: () => boolean;
  setPetVisible: (visible: boolean) => void;
  /** 桌宠三项偏好：勾选态从这里读，改的时候整包交回去（应用与持久化都在主进程那一处）。 */
  petPrefs: () => PetPrefs;
  setPetPrefs: (patch: Partial<PetPrefs>) => void;
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
    const prefs = controls.petPrefs();
    const onPet = controls.petVisible();
    // 桌宠收起时这三项照样可改：写的是偏好，下次放出桌宠时生效（`createPetWindow` 读同一份）。
    // 原生菜单没有 tooltip 可挂这句话（`MenuItemConstructorOptions` 里压根没那一项），所以摆在
    // 它上面一行 —— 一个"改了 apparently 没反应"的勾选框，比一句说明更容易被当成坏了。
    const check = (
      label: string,
      checked: boolean,
      onChange: (next: boolean) => void,
    ): MenuItem =>
      new MenuItem({
        label,
        type: "checkbox",
        checked,
        click: (item) => onChange(item.checked),
      });
    tray.setContextMenu(
      Menu.buildFromTemplate([
        { label: "显示控制台", click: () => controls.showConsole() },
        check("桌宠在桌面", onPet, (next) => {
          controls.setPetVisible(next);
          render(); // 勾选态由这次操作决定：立刻重画，托盘就不会短暂说谎
        }),
        ...(onPet
          ? []
          : [
              new MenuItem({
                label: "（桌宠收起中：下面五项下次放出时生效）",
                enabled: false,
              }),
            ]),
        check("自动置顶", prefs.alwaysOnTop, (next) => {
          controls.setPetPrefs({ alwaysOnTop: next });
          render();
        }),
        new MenuItem({
          label: "透明度",
          submenu: Menu.buildFromTemplate(
            OPACITY_STEPS.map((step) => ({
              label: step.label,
              type: "radio" as const,
              // 手改过偏好文件时可能落在档位之外：没有一条匹配就全不选中，比硬选一条诚实。
              checked: Math.abs(prefs.opacity - step.value) < 0.001,
              click: () => {
                controls.setPetPrefs({ opacity: step.value });
                render();
              },
            })),
          ),
        }),
        check("显示消息内容", prefs.showContent, (next) => {
          controls.setPetPrefs({ showContent: next });
          render();
        }),
        // 语音（系统 TTS）。默认关：出声是"打扰屋里人"的那个方向，必须是用户主动开的那一下。
        // 与「显示消息内容」是两道独立的闸 —— 页面侧两个都开着才出声（见 PetPage.speak）。
        check("朗读消息", prefs.voice, (next) => {
          controls.setPetPrefs({ voice: next });
          render();
        }),
        check("靠边隐藏", prefs.dockEnabled, (next) => {
          // 关掉时 `updatePetPrefs` 会当场把它从边上滑回来（关了却还藏着 = 开关只管下一次）。
          controls.setPetPrefs({ dockEnabled: next });
          render();
        }),
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

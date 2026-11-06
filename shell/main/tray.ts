/**
 * 托盘：控制台收起来之后，用户唯一的入口就在这里，所以它必须一直存在。
 *
 * 「关闭控制台」= 隐藏而不是退出（桌宠与后端继续活着）—— 这是一只宠物，
 * 关掉窗口不该让它消失；真要退出走托盘的「退出」，那条路会回收后端进程。
 */
import { Menu, MenuItem, Tray, app, nativeImage, type MenuItemConstructorOptions } from "electron";
import path from "node:path";

import type { PetPrefs } from "./state";
import type { Updater } from "./updater";

const ICON = path.join(__dirname, "..", "..", "build", "icon.ico");

/** 面板自动收起的档位（毫秒；0 = 不自动收起）。与透明度同一条分工：档位表住在托盘这一侧，
 *  `state.ts` 只管取值与夹取 —— 托盘是这张表唯一的写入口。 */
export const PANEL_AUTO_HIDE_STEPS: { label: string; value: number }[] = [
  { label: "不自动收起", value: 0 },
  { label: "5 秒", value: 5000 },
  { label: "15 秒", value: 15000 },
  { label: "30 秒", value: 30000 },
  { label: "60 秒", value: 60000 },
];

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
  /** 自动更新（设计稿 §3.3）。dev 态传 null ⇒ 整条更新菜单不出现（与开机自启同一理由）。 */
  updater: Updater | null;
  /** 设置类条目点完把新状态播报出来（菜单会关，用户就看不到结果了）。
   *  由 index.ts 用**更新回执同一条** `new Notification().show()` 实现 —— 那是本仓
   *  唯一有真机显示证据的通道；刻意绕开 notify() 的"前台就抑制"（这条是用户自己
   *  刚做的动作，前台更要给）。 */
  notifyResult: (title: string, body: string) => void;
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

  // 设置类条目（勾选/单选/自启）点完把结果**播报**出来。菜单会关是 Win32 的 OS 行为
  // （选中即销毁），Electron 没有"保持展开"的口子（官方 Tray API 全文核对过）。
  // 「原地重开」试过两版（popUpContextMenu + 延时/焦点归还通知区），真机两轮都没兑现；
  // 而这个时机在本仓不可验证 —— DSH 会话里起的壳会被沙箱拦在 userData 之外（实测
  // `Lock file can not be created: 拒绝访问`），每次都是用户替我当测试机，猜第三组
  // 参数就是拿别人的时间赌。用户 2026-11-06 拍板「实在不行就不做了」⇒ 撤重开，
  // 只留有真机证据的那半边：controls.notifyResult = 更新回执同一条
  // `new Notification().show()`（本仓唯一有真机显示证据的通道）。
  // 观感上菜单照旧关，但点完看得见"改成了什么"。
  const announceSetting = (label: string, value: string) => {
    controls.notifyResult(label, `已设为：${value}`);
  };

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
        click: (item) => {
          onChange(item.checked);
          announceSetting(label, item.checked ? "开" : "关");
        },
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
                announceSetting("透明度", step.label);
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
        new MenuItem({
          // 面板（消息框）摊着多久之后自己收回去。用户 10-03 报的：点开之后不收，得再点一下。
          // 「不自动收起」保留原行为（当便签一直开着的人要它）。到点**不一定**收：指针还在面板上、
          // 你正在输入、她正在说话这三种时候收走等于抢你正在读的东西，那条判据在页面侧。
          label: "面板自动收起",
          submenu: Menu.buildFromTemplate(
            PANEL_AUTO_HIDE_STEPS.map((step) => ({
              label: step.label,
              type: "radio" as const,
              // 手改过偏好文件、落在档位之外时全不选中，比硬选一条诚实（与透明度同一条理由）。
              checked: prefs.panelAutoHideMs === step.value,
              click: () => {
                controls.setPetPrefs({ panelAutoHideMs: step.value });
                render();
                announceSetting("面板自动收起", step.label);
              },
            })),
          ),
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
                  announceSetting("开机自启", item.checked ? "开" : "关");
                },
              },
            ]
          : []),
        // 自动更新（设计稿 §3.3）：一条状态行 + 一条动作行，动作随状态变（检查→下载→装）。
        // 每次点完都 render()：托盘菜单是即时快照，不重画就会出现"下完了还写着下载"的谎。
        ...(updateMenuItems(controls.updater, render, announceSetting)),
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

/** 把 updater 的状态机翻成托盘的两行（状态 + 动作）。null（dev 态）= 两行都不出。
 *  `announceSetting` 由 createTray 注入：设置类条目点完播报结果（菜单会关是 OS 行为，
 *  见 createTray 里那条撤掉重开的账）；动作类条目不播：它们的去向不在菜单里。 */
function updateMenuItems(
  updater: Updater | null,
  refresh: () => void,
  announceSetting: (label: string, value: string) => void,
): MenuItemConstructorOptions[] {
  if (!updater) return [];
  const state = updater.state();
  const status = ((): { label: string; enabled: boolean } => {
    switch (state.kind) {
      case "checking":
        return { label: "正在检查更新…", enabled: false };
      case "available":
        return { label: `发现新版本 ${state.version}`, enabled: false };
      case "downloading":
        return { label: `下载中 ${state.percent}%`, enabled: false };
      case "ready":
        return { label: `新版 ${state.version} 已下载`, enabled: false };
      case "uptodate":
        return { label: "已是最新版本", enabled: false };
      case "error":
        return { label: `更新：${state.message.slice(0, 40)}`, enabled: false };
      default:
        return { label: "更新", enabled: false };
    }
  })();
  const action = ((): { label: string; run: () => void } | null => {
    switch (state.kind) {
      case "idle":
      case "uptodate":
      case "error":
        return { label: "检查更新", run: () => updater.check() };
      case "available":
        return { label: "下载更新", run: () => updater.download() };
      case "ready":
        return { label: "重启并更新", run: () => void updater.install() };
      default:
        return null; // checking / downloading 进行中：不给可点的动作
    }
  })();
  const items: MenuItemConstructorOptions[] = [
    { label: status.label, enabled: status.enabled },
  ];
  if (action) {
    items.push({
      label: action.label,
      click: () => {
        action.run();
        refresh();
      },
    });
  }
  // 自动更新的三档开关（用户 2026-10-14 要的）。做成**逐级放开**而不是一个总开关，
  // 理由写在 updater.ts 的 UpdaterPrefs 注释里：检查/下载/装 三件事的打扰程度差着量级，
  // 一个开关管三件等于"允许开机查一下"顺手买下"它会自己重启我的程序"。
  const prefs = updater.prefs();
  const toggle = (label: string, key: "checkOnStartup" | "autoDownload" | "autoInstallOnQuit"): MenuItemConstructorOptions => ({
    label,
    type: "checkbox",
    checked: prefs[key],
    click: (item: MenuItem) => {
      updater.setPrefs({ [key]: item.checked });
      refresh();
      announceSetting(label, item.checked ? "开" : "关");
    },
  });
  // 更新通道（P10 ③，2026-10-27 接上界面入口）：桌面版没有金丝雀，分通道的 feed 就是灰度。
  // 默认 stable；选 beta 即收预发布（electron-updater 的 allowPrerelease + beta channel
  // feed 两件事都在 updater.ts 的 applyPrefs 里一起翻，这里只写偏好）。写侧走 setPrefs
  // 的白名单枚举：认不出的值回 stable，坏文件不许把人拐进灰度。
  const CHANNELS: { label: string; value: "stable" | "beta" }[] = [
    { label: "正式版（只收正式发布）", value: "stable" },
    { label: "灰度 beta（预发布也收）", value: "beta" },
  ];
  items.push({
    label: "自动更新设置",
    submenu: Menu.buildFromTemplate([
      toggle("启动时检查更新", "checkOnStartup"),
      toggle("自动下载更新", "autoDownload"),
      toggle("退出时自动安装", "autoInstallOnQuit"),
      new MenuItem({ type: "separator" }),
      new MenuItem({ label: `更新通道：${prefs.channel === "beta" ? "灰度（beta）" : "正式版"}`, enabled: false }),
      ...CHANNELS.map(
        (c): MenuItemConstructorOptions => ({
          label: c.label,
          type: "radio",
          checked: prefs.channel === c.value,
          click: () => {
            updater.setPrefs({ channel: c.value });
            refresh();
            announceSetting("更新通道", c.value === "beta" ? "灰度 beta" : "正式版");
          },
        }),
      ),
    ]),
  });
  return items;
}

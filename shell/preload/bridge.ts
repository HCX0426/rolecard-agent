/**
 * 壳暴露给页面的全部能力 —— 目前只有"后端在哪 / 后端起来了吗"。
 *
 * 为什么探测要问主进程，而不是页面自己 fetch 后端：着陆页是 `file://` 加载的本地页面，
 * 从那儿跨源访问 http 端点会被浏览器拦，探活结果变成一个恒假的信号（表现为"永远在启动中"）。
 * 主进程有真 socket，用它那份 `serving()` 判定，端口与语义都只有一处定义。
 *
 * 每多暴露一个方法，就是给"后端托管的那个远程源"多开一条能碰到桌面的口子 ——
 * 后面加通知/托盘/目录对话框时，一个一个显式加（架构审计 §11.2 的实测教训：
 * 远程源能不能拿到桥，取决于这里暴露了什么，而不是页面写了什么属性）。
 */
import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("rolecardShell", {
  backendUrl: (): Promise<string> => ipcRenderer.invoke("shell:backend-url"),
  backendReachable: (): Promise<boolean> => ipcRenderer.invoke("shell:backend-reachable"),
});

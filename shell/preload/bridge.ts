/**
 * 壳暴露给页面的全部能力 —— 目前三类：后端在哪 / 起来了吗、打开某个会话（+系统通知）、
 * 本地推理服务的进程归属与起停。
 *
 * 为什么探测要问主进程，而不是页面自己 fetch 后端：着陆页是 `file://` 加载的本地页面，
 * 从那儿跨源访问 http 端点会被浏览器拦，探活结果变成一个恒假的信号（表现为"永远在启动中"）。
 * 主进程有真 socket，用它那份 `serving()` 判定，端口与语义只有一处定义。
 *
 * 每多暴露一个方法，就是给"后端托管的那个源"多开一条能碰到桌面的口子 —— 所以这里只放
 * 桌面壳自己该有的四样，且**不做任何通用能力**（没有"打开任意 URL""执行任意 IPC 通道"
 * 那种转发器：有了它，下面这些限制就一句空话）。
 *
 * "要打开哪个会话"的接收侧是**单个槽位**而不是监听器列表：一个页面只有一个"打开会话"的
 * 归属者（控制台），后注册者覆盖前者；注册完成后向主进程报一次就绪，主进程才把攒下的那条
 * 投过来 —— 早于监听 send 等于丢消息。
 */
import { contextBridge, ipcRenderer } from "electron";

type OpenThreadHandler = (threadId: string) => void;

let onOpenThread: OpenThreadHandler | null = null;

ipcRenderer.on("shell:open-thread", (_event, value: unknown) => {
  if (typeof value === "string" && value) onOpenThread?.(value);
});

contextBridge.exposeInMainWorld("rolecardShell", {
  backendUrl: (): Promise<string> => ipcRenderer.invoke("shell:backend-url"),
  backendReachable: (): Promise<boolean> => ipcRenderer.invoke("shell:backend-reachable"),
  /** 请壳把控制台带到前台并打开这个会话（桌宠气泡被点时走这里）。 */
  openSession: (threadId: string): void => {
    void ipcRenderer.invoke("shell:open-session", threadId);
  },
  /** 请壳拍一条系统通知；点通知 = 打开 `threadId`（没有会话就只是提示）。 */
  notify: (title: string, body: string, threadId?: string | null): void => {
    void ipcRenderer.invoke("shell:notify", title, body, threadId ?? null);
  },
  /** 登记"谁来接收打开会话"，并向主进程报一次就绪。传 null 注销。 */
  onRequestOpenThread: (handler: OpenThreadHandler | null): void => {
    onOpenThread = handler;
    if (handler) void ipcRenderer.invoke("shell:renderer-ready");
  },
  // ---- 本地推理服务的进程（D③-b）：三个方法**一律零参数** ------------------------------
  // 起停一个本机进程，能碰到的东西比"打开一个会话"多得多。参数一旦允许路径/命令，
  // 这个桥就退化成任意执行入口 —— 所以归属、启动、停止都不带任何输入，路径由壳自己决定。
  /** 只回答"这个 Ollama 是不是本壳起的（+pid）"；"在不在跑"由 `/api/local-service` 说。 */
  ollamaOwner: (): Promise<{ managed: boolean; pid: number | null; binary: string | null }> =>
    ipcRenderer.invoke("shell:ollama-owner"),
  startOllama: (): Promise<{ ok: boolean; reason?: string }> =>
    ipcRenderer.invoke("shell:ollama-start"),
  stopOllama: (): Promise<{ ok: boolean; reason?: string }> =>
    ipcRenderer.invoke("shell:ollama-stop"),
  /** 用系统目录选择器挑一个目录；取消 = null。**不回传任何参数给主进程**：路径是用户
   *  在原生对话框里选的，不是页面给的，所以这条不构成"页面能指定任意路径"的口子。 */
  pickDirectory: (): Promise<string | null> => ipcRenderer.invoke("shell:pick-directory"),
});

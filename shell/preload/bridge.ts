/**
 * 壳暴露给页面的全部能力 —— 目前四类：后端在哪 / 起来了吗、打开某个会话（+系统通知）、
 * 桌宠窗（展开 / 拖动 / 「显示消息内容」旗子）、本地推理服务的进程归属与起停。
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
type ContentVisibleHandler = (visible: boolean) => void;
type SpriteShiftHandler = (px: number) => void;

let onOpenThread: OpenThreadHandler | null = null;
// 与 open-thread 同一个形状：**单个槽位**而不是监听器列表 —— 桌宠页是这面旗的唯一归属者，
// 后注册者覆盖前者（React 严格模式下挂载两次也不留旧监听）。
let onContentVisible: ContentVisibleHandler | null = null;
let onSpriteShift: SpriteShiftHandler | null = null;

ipcRenderer.on("shell:open-thread", (_event, value: unknown) => {
  if (typeof value === "string" && value) onOpenThread?.(value);
});

ipcRenderer.on("shell:pet-content-visible", (_event, value: unknown) => {
  if (typeof value === "boolean") onContentVisible?.(value);
});
// 色片在展开窗里要自己挪回原位多少像素（贴边时夹取会把窗推走，见 windows.ts 的
// `petExpandTarget`）。非数字一律不动 —— 这条通道来自壳自己，但渲染端不该信任任何输入。
ipcRenderer.on("shell:pet-sprite-shift", (_event, value: unknown) => {
  if (typeof value === "number" && Number.isFinite(value)) onSpriteShift?.(value);
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
  /** 桌宠悬停展开 / 收起（设计稿 §7.2）。**只报意图不报坐标**：面板该长多大、贴着屏幕
   *  哪一侧翻，全是主进程按工作区算的 —— 页面能指定的只有"现在要不要摊开"。 */
  setPetExpanded: (expanded: boolean): Promise<boolean> =>
    ipcRenderer.invoke("shell:pet-expanded", expanded),
  /** 拖动桌宠：只报增量，绝对坐标与出屏夹取都在壳里。 */
  movePetBy: (dx: number, dy: number): void => {
    ipcRenderer.send("shell:pet-drag", [dx, dy]);
  },
  /** 松手了：壳在这一刻才判要不要吸到边上（§7.6）。**零参数** —— 哪条边、藏多深都由壳量。 */
  petDragEnd: (): void => {
    ipcRenderer.send("shell:pet-drag-end");
  },
  /** 悬停：只把趴在边上的那半只拉出来（面板改成点击才弹，见 §7.6）。零参数。 */
  petReveal: (): void => {
    ipcRenderer.send("shell:pet-reveal");
  },
  /** 指针离开桌面件：本来吸着边的，让它自己趴回去。零参数。 */
  petRetuck: (): void => {
    ipcRenderer.send("shell:pet-retuck");
  },
  /** 桌宠的「显示消息内容」旗子（托盘是它唯一的写入口）。**只有读**：给页面一条写回的路，
   *  等于让后端托管的那个源自己决定要不要藏起内容，那就不叫隐私开关了。 */
  petContentVisible: (): Promise<boolean> => ipcRenderer.invoke("shell:pet-content-visible"),
  /** 旗子被托盘改动时收一次通知；传 null 注销。首次值仍要 pull（push 早于监听就是丢消息）。 */
  onPetContentVisible: (handler: ContentVisibleHandler | null): void => {
    onContentVisible = handler;
  },
  /** 色片自挪的像素数（壳每次改展开落点时推一次）；传 null 注销。同上是单槽位。 */
  onPetSpriteShift: (handler: SpriteShiftHandler | null): void => {
    onSpriteShift = handler;
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

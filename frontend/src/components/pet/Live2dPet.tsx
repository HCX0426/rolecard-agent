// 桌宠的 Live2D 渲染层：pixi.js + pixi-live2d-display，**整条分支懒加载**。
//
// 为什么懒加载：pixi 是第二套渲染栈（几百 KB），而绝大多数时候用的是序列帧；只有当某个角色
// 真的指向一个 `kind: "live2d"` 的包时才值得付这笔钱（Vite 对动态 import 单独切 chunk，
// 序列帧那条路一行 pixi 都不会加载）。
//
// 为什么 Cubism Core 要我们手动挂到 window：渲染库自己写着
// `if (!window.Live2DCubismCore) throw new Error("Could not find Cubism 4 runtime")`，
// 而那份运行时**不由我们随包分发**（Live2D SDK 条款要使用者自己接受）。它从后端那条
// `/api/pets/_runtime/...` 读本地文件 —— 也就是"离线可用"是设计前提，不是碰运气：
// 本项目里"不装 Ollama、纯本机、没网"是一等形态，运行时抓 CDN 的那种做法不算做完。
//
// 为什么这里没有菜单/状态条/气泡：换过 `oh-my-live2d` 之后发现它是博客看板娘（自带那套
// 外壳，而且**没有"按组触发 motion"的 API**），而桌宠要的恰恰是"她在回话时做那个动作"。
// 所以这里直接用 pixi 那条路，外壳继续是我们自己的（透明窗、拖拽、点开合都在 PetPage）。
//
// ⚠ 这一条分支**没有真模型可验过**（仓库里不带任何来源不明的角色模型）。已经验的是：
// 清单/守卫/回退那几层（后端 35 条用例）、以及"加载失败就退回默认包并说原因"这条路径；
// 没验的是"模型真的动起来"。第一次放真模型时要按 `motionsFromPack` 那张映射对着看。

import { useEffect, useRef, useState } from "react";
// 只取类型：类型导入不会把 pixi 拉进首屏 chunk（运行时那份在函数里动态 import）。
import type { Application, Container } from "pixi.js";

import type { PetStatus } from "./PetSprite";

const CUBISM_CORE_URL = "/api/pets/_runtime/live2dcubismcore.min.js";

/** 把 Cubism Core 挂上 window（已经在了就直接过）。失败抛出去，由调用方决定怎么回退。 */
async function ensureCubismCore(): Promise<void> {
  if ((window as unknown as { Live2DCubismCore?: unknown }).Live2DCubismCore) return;
  await new Promise<void>((resolve, reject) => {
    const tag = document.createElement("script");
    tag.src = CUBISM_CORE_URL;
    tag.onload = () => resolve();
    tag.onerror = () => reject(new Error("缺 Cubism Core 运行时"));
    document.head.appendChild(tag);
  });
}

/** 状态 → motion 组名：包自己声明（`pack.json` 的 `motions`），没声明就不主动触发。 */
export function motionFor(
  motions: Record<string, string> | undefined,
  status: PetStatus,
): string | undefined {
  return motions?.[status];
}

export interface Live2dPetProps {
  /** 这个包的 `model3.json` 地址（后端那条按包读文件的路由给的）。 */
  url: string;
  status: PetStatus;
  motions?: Record<string, string>;
  width?: number;
  height?: number;
  /** 加载/渲染失败时的回退（原因会被界面说出来）。抛出去的一律算失败，不静默。 */
  onError?: (reason: string) => void;
  className?: string;
  "data-testid"?: string;
}

/**
 * 渲染库返回的那个模型：我们只用到"摆位/缩放"三个几何属性与"放一个动作"这一个方法。
 * 故意写窄 —— 库的类型是 pixi 的 Container 加上它自己一大堆，全接过来等于把换库的成本
 * 写进这一格。`as Container` 那一步在 addChild 处做。
 */
interface LiveModel {
  x: number;
  y: number;
  width: number;
  height: number;
  motion?: (group?: string) => unknown;
}

export function Live2dPet({
  url,
  status,
  motions,
  width = 160,
  height = 184,
  onError,
  className,
  ...rest
}: Live2dPetProps) {
  const host = useRef<HTMLDivElement | null>(null);
  // 模型与 app 都活在 ref 里：它们的重建只该发生在 url 变了的时候，而不是每次渲染。
  const model = useRef<LiveModel | null>(null);
  const [failed, setFailed] = useState("");

  useEffect(() => {
    let disposed = false;
    let app: Application | null = null;
    setFailed("");
    (async () => {
      try {
        await ensureCubismCore();
        const [{ Application: PixiApp, Ticker }, { Live2DModel }] = await Promise.all([
          import("pixi.js"),
          import("pixi-live2d-display/cubism4"),
        ]);
        if (disposed || !host.current) return;
        // 渲染库靠 ticker 推进动作；不注册就没有任何一帧在动（它把这件事留给宿主做）。
        Live2DModel.registerTicker(Ticker);
        app = new PixiApp({ width, height, backgroundAlpha: 0, antialias: true });
        const canvas = app.view as unknown as HTMLCanvasElement;
        canvas.style.width = `${width}px`;
        canvas.style.height = `${height}px`;
        host.current.appendChild(canvas);
        const loaded = (await Live2DModel.from(url, {
          // 关掉库自带的点击互动：桌宠的点/拖全在 PetPage 的根节点上（一次冒泡只触发一遍），
          // 让模型自己再监听一遍会出现"点一下既动又开面板"。
          autoInteract: false,
        })) as unknown as LiveModel;
        if (disposed) {
          app.destroy(true, { children: true });
          app = null;
          return;
        }
        // 模型原始尺寸通常远大于桌宠这一格：按短边等比缩进来，脚踩窗口下缘（与序列帧一致）。
        const scale = Math.min(width / loaded.width, height / loaded.height);
        loaded.width *= scale;
        loaded.height *= scale;
        loaded.x = width / 2 - loaded.width / 2;
        loaded.y = height - loaded.height;
        // 库给的对象本来就是 pixi 的 Container，只是我们故意不把那个类型接过来（见 LiveModel）。
        app.stage.addChild(loaded as unknown as Container);
        model.current = loaded;
      } catch (error) {
        if (disposed) return;
        const reason = error instanceof Error ? error.message : "模型加载失败";
        setFailed(reason);
        onError?.(reason);
      }
    })();
    return () => {
      disposed = true;
      model.current = null;
      app?.destroy(true, { children: true });
    };
  }, [url, width, height]);

  // 换状态就换动作：包没声明这个状态的 motion 时什么都不做（不打断当前动作）。
  useEffect(() => {
    const group = motionFor(motions, status);
    if (group) model.current?.motion?.(group);
  }, [motions, status]);

  if (failed) {
    // 失败不在这里画东西 —— 交给调用方按"这个包不可用"去回退（PetPage 收到 onError 之后
    // 落默认包并把原因印出来）。这里只留一个空壳，避免半张坏图挂在桌面上。
    return <div className={className} data-testid={rest["data-testid"] ?? "pet-live2d-failed"} />;
  }

  return (
    <div
      ref={host}
      className={className}
      data-testid={rest["data-testid"] ?? "pet-live2d"}
      role="img"
      aria-label="桌宠形象"
      style={{ width, height }}
    />
  );
}

export default Live2dPet;

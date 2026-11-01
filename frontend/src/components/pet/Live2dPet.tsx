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
//
// ⚠ 上面那句"没真模型可验"自 2026-11-01 起**部分作废**：本机装了用户自己的 hiyori 包
// （`<数据根>/pets/hiyori/`，不入库），已在真渲染进程上量过它的内容边界并验过趴边可见性。
// 量法见 `scripts/js/verify_pet_dock.cjs`（CDP 直取真值）与 `verify_dock_by_drag.cjs`
// （CDP 真输入拖着走三条边）—— **别用屏摄**：那扇窗是透明置顶窗，`CopyFromScreen` 会被
// 别的窗整个盖住（2026-11-01 就因此把一片纯白读成"什么都没画"），`PrintWindow` 对透明 GPU
// 窗只回残缺一块（160×184 只抓到 53×18）。只有渲染进程自己知道"画了什么、画在哪"。

import { useEffect, useRef, useState } from "react";
// 只取类型：类型导入不会把 pixi 拉进首屏 chunk（运行时那份在函数里动态 import）。
import type { Application, Container } from "pixi.js";

// 摆位与内容测量的算术住在 `live2dGeometry.ts`：那是一层没有 React / pixi 的纯函数，
// 既能脱离 jsdom 直接测，也不该寄在组件文件里（`react-refresh/only-export-components`）。
import {
  contentBounds,
  live2dPlacement,
  type ContentBox,
} from "./live2dGeometry";

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

/**
 * 渲染一帧并量出画布里**内容**的边界。
 *
 * 走的是 pixi 自己的 `renderer.extract.pixels`（不碰 WebGL 细节）：渲染一帧 → 把像素
 * 读回来 → 扫 alpha 求 bbox。读不到（环境不支持 extract / 上下文丢了）就返回 null，
 * 调用方据此**退回按画布摆位** —— 旧行为，不比原先更坏。
 *
 * **缓冲尺寸一律从 renderer 自己报，不吃调用方传进来的 CSS 宽高**：`extract.pixels()`
 * 读的是 `renderer.width × renderer.height`（= CSS 尺寸 × `resolution`）。按 CSS 尺寸当
 * stride 去扫，一旦有人为了高分屏清晰加上 `resolution: devicePixelRatio`，框会**静默量错**
 * —— 不崩，只是"内容位置怪怪的"，属于最难查的那一类（这台机器就是 150% 缩放）。
 */
async function measureContent(
  app: Application,
): Promise<{ box: ContentBox; buffer: { width: number; height: number } } | null> {
  try {
    app.renderer.render(app.stage);
    const renderer = app.renderer as unknown as {
      extract?: { pixels(target?: unknown): Uint8Array | Uint8ClampedArray };
      width: number;
      height: number;
    };
    const pixels = renderer.extract?.pixels();
    if (!pixels) return null;
    const width = Math.max(1, Math.round(renderer.width));
    const height = Math.max(1, Math.round(renderer.height));
    const box = contentBounds(pixels, width, height);
    return box ? { box, buffer: { width, height } } : null;
  } catch {
    // 读像素失败不是故障：量边界只是"让趴边露得出东西"的优化，读不到就退回旧摆法。
    return null;
  }
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
  /**
   * 量到内容边界时报一次（**画布 CSS 像素**坐标，原点左上）。
   *
   * 报的是"我这个 160×184 的盒子里，哪一块真的有像素"。壳据此算趴边该推出去多少、
   * 以及点击命中区在哪 —— 见 `PetPage.reportHotRects` 与 `shell/main/windows.ts`。
   *
   * 三条路径，口径各不相同，说清免得后来人以为只有一条：
   *  - 两次都量到（正常）⇒ 报**摆完再量**的那一格，并按它做过一次居中纠偏；
   *  - 只有第二次没量到 ⇒ 报**按第一次测量摆出来的**那一格（摆位就是照它落的，所以
   *    与画面一致，只是没吃到纠偏）；
   *  - 两次都没量到 ⇒ **一行都不报**，调用方保持"整个盒子都算内容"的旧口径 —— 那比按
   *    一个瞎猜的框收窄更保守，不会把人点不着。
   */
  onContentBounds?: (box: ContentBox) => void;
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
  onContentBounds,
  className,
  ...rest
}: Live2dPetProps) {
  const host = useRef<HTMLDivElement | null>(null);
  // 模型与 app 都活在 ref 里：它们的重建只该发生在 url 变了的时候，而不是每次渲染。
  const model = useRef<LiveModel | null>(null);
  const [failed, setFailed] = useState("");
  // onError 走"最新引用"ref（而不是直接进下面 effect 的依赖）：调用方传的是内联箭头
  // （PetPage 每次渲染都换引用），把它放进依赖会让整个 pixi 应用跟着每次渲染重建 —— 那
  // 正是这个 effect 注释反复强调要避开的。ref 让"加载失败那一刻读到的总是最新的 onError"
  // 成立，而依赖数组保持只有 url/width/height。
  // 赋值放 effect 不放渲染体（react-hooks/refs 的判据：渲染中途被并发打断会留下半路的
  // 旧值）；effect 在提交后同步跑，而加载失败最早也在 await 之后 —— 读到时必已就位。
  const onErrorRef = useRef(onError);
  const onContentBoundsRef = useRef(onContentBounds);
  useEffect(() => {
    onErrorRef.current = onError;
    onContentBoundsRef.current = onContentBounds;
  }, [onError, onContentBounds]);

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
        // `preserveDrawingBuffer`：让画布内容**可读**。要量"人物在画布里占多大、偏在哪"
        // （见 `contentBounds`），没有它就读不到像素 —— 而那是让趴边的桌宠露得出东西的
        // 前提。代价是多保留一帧缓冲，对这块 160×184 的小窗可以接受。
        app = new PixiApp({
          width,
          height,
          backgroundAlpha: 0,
          antialias: true,
          preserveDrawingBuffer: true,
        });
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
        // 库给的对象本来就是 pixi 的 Container，只是我们故意不把那个类型接过来（见 LiveModel）。
        app.stage.addChild(loaded as unknown as Container);

        // --- 量内容边界 → 按内容摆位 → 再量一次报出去 --------------------------------
        //
        // Live2D 的 `loaded.width/height` 是 **Cubism 画布**尺寸（正方形，人物居中带透明边），
        // 不是人物占多大。按画布摆位 ⇒ 趴边时露出的那 48px 全落在透明边里（屏内零像素）。
        //
        // 所以：先按画布粗摆一帧 → 读一次像素量出内容边界 → 按内容重摆（居中、脚踩下沿）
        // → **再读一次**取实测报出去（下一段讲为什么必须是第二次那个）。
        // 全程只在加载完量这两次，不逐帧量：姿态会变、边界会随动作抖，而几何只该定一次；
        // 每帧回读还会把 WebGL 的回读开销压到桌宠这条常驻链路上。
        const canvasBox = { width: loaded.width, height: loaded.height };
        // 粗摆：先按**画布**摆一帧（内容这时还量不出来，得先有东西在画面上）。
        const rough = live2dPlacement({ x: 0, y: 0, ...canvasBox }, { width, height });
        loaded.width = rough.width;
        loaded.height = rough.height;
        loaded.x = rough.x;
        loaded.y = rough.y;

        const measured = await measureContent(app);
        if (disposed) return;
        // 推算出来的摆位（量不到就是 null ⇒ 保持粗摆那一帧，等于旧行为）
        let place: ReturnType<typeof live2dPlacement> | null = null;
        if (measured) {
          // 量到的框在**像素缓冲**坐标，要倒回画布原始单位才能再摆一次，中间两段：
          //   ÷(buffer.width/width) 回到画布 CSS，÷rough.scale 回到粗摆那一帧之前的原始单位
          //   （那一帧把整个画布缩到盒里了）。第二段是承重的：`loaded.width` 乘的是
          // **没缩放过**的原始画布尺寸（hiyori 是 1024），少除一段会把人整个推出盒外。
          const perUnit = width / measured.buffer.width / rough.scale;
          const raw: ContentBox = {
            x: measured.box.x * perUnit,
            y: measured.box.y * perUnit,
            width: measured.box.width * perUnit,
            height: measured.box.height * perUnit,
          };
          place = live2dPlacement(raw, { width, height });
          loaded.width = canvasBox.width * place.scale;
          loaded.height = canvasBox.height * place.scale;
          loaded.x = place.x - raw.x * place.scale;
          loaded.y = height - raw.y * place.scale - place.height;
        }

        // --- 摆完**再量一次**，报出去的那一格必须是实测的 ------------------------------
        //
        // 这条纪律是 2026-11-01 用真机换来的。上一版报的是**上面那套算术推算出来的**落点，
        // 而 pixi 的 Live2D 模型按**画布中心**缩放、不是左上角 —— "内容左沿正好落在 place.x"
        // 这个假设不成立，报出去的**位置与宽度都偏了**。两组真机读数：
        //   壳收到的上报  `桌宠内容框上报：[251,332,58,184]` ⇒ 锚点局部 x=71 宽=58
        //   几分钟后 CDP 量 alpha bbox（画布 x[55..102]，盒在画布 200 处）⇒ 锚点局部 x=75 宽=48
        // 这两个数之间叠着**两种误差**：推算偏（左沿报小了、宽度报虚了）与姿态漂移
        //   （人物在呼吸，内容框只在加载完量过一次）。合起来的表现：
        //   趴左：T = x + w − vis ⇒ 71+58−48 = **81**（照 75/48 只该 75）⇒ 屏内只剩 **42px**；
        //   趴右：T = 200 − x − vis ⇒ 81 ⇒ 屏内 **44px**（切掉的落点不同，所以轻一些）。
        // ⇒ 同一个错在两条边上表现不同，**只验一条一定会漏**（当天我就只验了右边就宣布修好了）。
        // 这两个数连同"窄内容整条露"的新规则钉在 `shell/scripts/test-pet-dock-geometry.cjs`，
        // 三条边的真机跑法在 `scripts/js/verify_dock_by_drag.cjs`。
        //
        // 现在：量 → 摆 → **再量** → 按实测做一次平移纠偏（把内容挪到"水平居中 + 脚踩下沿"）
        // → 报纠偏后的实测格。只纠一帧、不迭代（反复量会跟动作打架，几何只会抖）。
        const settled = await measureContent(app);
        if (disposed) return;
        if (settled) {
          const k = width / settled.buffer.width;
          const a: ContentBox = {
            x: settled.box.x * k,
            y: settled.box.y * k,
            width: settled.box.width * k,
            height: settled.box.height * k,
          };
          const dx = (width - a.width) / 2 - a.x; // 实测内容挪到水平居中
          const dy = height - a.height - a.y; // 挪到脚踩下沿
          loaded.x += dx;
          loaded.y += dy;
          onContentBoundsRef.current?.({ x: a.x + dx, y: a.y + dy, width: a.width, height: a.height });
        } else if (place) {
          // 第二次读不到（extract 失败 / 上下文丢了）：退回报推算值 —— 与上一版同口径，
          // 不比原先更坏，而壳那边日志会显示报来的是哪种（`桌宠内容框上报`）。
          onContentBoundsRef.current?.({
            x: place.x,
            y: height - place.height,
            width: place.width,
            height: place.height,
          });
        }
        model.current = loaded;
      } catch (error) {
        if (disposed) return;
        const reason = error instanceof Error ? error.message : "模型加载失败";
        setFailed(reason);
        onErrorRef.current?.(reason);
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

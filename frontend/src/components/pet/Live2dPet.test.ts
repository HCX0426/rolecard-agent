// Live2dPet 的布局几何测试（纯函数那半边，不碰 pixi）。
//
// 钉的是 2026-11-01 那条真事：**桌宠趴边时看不见**。
//
// 壳的靠边隐藏原先假设「色片内容横向充满那 160 宽的盒」—— 趴边时窗推出去 132，留在屏内的
// 那 48px（`PET_PEEK_PX`）就该是宠物本体。Live2D 的模型**画布**是 Cubism 的正方形画布，
// 人物只占中间一小块（雏雾 hiyori 真机实测人物只有 44×166，左右各约 58px 全透明）——
// 按画布摆位时那 48px 恰好全落在透明边里 ⇒ 屏幕上一个像素都没有，用户看到的就是一只不见了。
//
// 修法**不是**把人物挪到左沿。第一版那么干过，它只治好了趴右边缘：趴左边缘时屏内留的是
// 锚点的**右侧**那 48px，左对齐的人物就整个跑到屏外去了（同一个 bug 的对称版本），
// 而且不趴边时她还平白偏左 58px。开源那边的口径也一致 —— VPet 把「贴边该往里推多少」做成
// **形象自己声明**的数据，不是宿主里的常数。
//
// 所以分工是：**渲染层保持居中、只负责把内容边界量准并报出去**，「露多少、往哪贴」由壳按
// 当前吸的那条边算（`shell/main/petDockGeometry.tuckForEdge`，那边另有一套读数测试）。
// 这里钉两件事：① 摆位水平居中、脚踩下沿；② 报出去的那一格 = 屏幕上真正有像素的那一格。

import { describe, expect, it } from "vitest";

import { contentBounds, live2dPlacement } from "./live2dGeometry";

// 壳里那几格（`shell/main/petDockGeometry.ts`）：色片盒 160×184、趴边露 48。
const BOX = { width: 160, height: 184 };
const PEEK = 48;

describe("live2dPlacement", () => {
  it("按内容短边等比缩进盒里（不放大、不裁切）", () => {
    // 内容 100×200（1:2 的窄立绘）：高受限 ⇒ scale = 184/200，宽 92。
    const p = live2dPlacement({ x: 0, y: 0, width: 100, height: 200 }, BOX);
    expect(p.scale).toBeCloseTo(BOX.height / 200, 10);
    expect(p.height).toBeCloseTo(BOX.height, 10);
    expect(p.width).toBeCloseTo(92, 10);
  });

  it("脚踩下沿（它站在地上，不是浮着）", () => {
    const p = live2dPlacement({ x: 0, y: 0, width: 100, height: 200 }, BOX);
    expect(p.y).toBeCloseTo(BOX.height - p.height, 10);
  });

  it("水平居中：不在渲染层定死左还是右（那由壳按吸的边决定）", () => {
    const p = live2dPlacement({ x: 58, y: 4, width: 44, height: 166 }, BOX);
    // 内容 44×166 ⇒ 高受限 scale=184/166 ⇒ 宽 ≈48.8，居中 x=(160−48.8)/2≈55.6。
    expect(p.x).toBeCloseTo((BOX.width - p.width) / 2, 10);
    expect(p.x).toBeGreaterThan(0);
  });

  it("内容缩出来比露头宽 ⇒ 壳只要肯算，屏内那 48px 必落在人物上", () => {
    const p = live2dPlacement({ x: 58, y: 4, width: 44, height: 166 }, BOX);
    expect(p.width).toBeGreaterThanOrEqual(PEEK - 1);
  });

  it("比盒还宽的内容缩到盒宽，左右都不越界", () => {
    const p = live2dPlacement({ x: 0, y: 0, width: 3000, height: 100 }, BOX);
    expect(p.width).toBeLessThanOrEqual(BOX.width);
    expect(p.x).toBeCloseTo((BOX.width - p.width) / 2, 10);
  });
});

describe("报给壳的那一格内容框", () => {
  /**
   * 复刻组件里「量完内容 → 重摆 → 报出去」那几行的算术。
   *
   * 为什么不直接测组件：那条路要 pixi + WebGL + 一个真模型，jsdom 里能 mock 的全 mock 了，
   * 测到的只是 mock 听话。真会算错的只有这几行坐标换算（画布坐标系 → 盒内坐标系），
   * 于是把它按同样的顺序独立跑一遍 —— 钉的就是「报出去的那一格 = 屏上真有像素的那一格」。
   * 组件那段（`Live2dPet.tsx` 的 `onContentBoundsRef.current?.(...)`）必须与此同形。
   */
  function reportedBox(raw: { x: number; y: number; width: number; height: number }) {
    const place = live2dPlacement(raw, BOX);
    return {
      x: place.x,
      y: BOX.height - place.height,
      width: place.width,
      height: place.height,
    };
  }

  it("雏雾那种窄立绘：框是人物本身，且底边正好踩在盒底", () => {
    const r = reportedBox({ x: 58, y: 4, width: 44, height: 166 });
    expect(r.height).toBeCloseTo(BOX.height, 10);
    expect(r.y).toBeCloseTo(0, 10);
    expect(r.x + r.width).toBeLessThanOrEqual(BOX.width);
  });

  it("内容充满画布时报整盒 ⇒ 壳那边等价于旧常数（序列帧零行为变化）", () => {
    const r = reportedBox({ x: 0, y: 0, width: BOX.width, height: BOX.height });
    expect(r.x).toBeCloseTo(0, 10);
    expect(r.y).toBeCloseTo(0, 10);
    expect(r.width).toBeCloseTo(BOX.width, 10);
    expect(r.height).toBeCloseTo(BOX.height, 10);
  });

  it("矮内容：框底仍在盒底（脚不悬空），报的高就是人物高", () => {
    const r = reportedBox({ x: 0, y: 0, width: 160, height: 40 });
    // scale 受宽 ⇒ 160/160=1，高 40 ⇒ 框高 40，底边踩盒底。
    expect(r.height).toBeCloseTo(40, 10);
    expect(r.y + r.height).toBeCloseTo(BOX.height, 10);
  });
});

describe("contentBounds", () => {
  /** 造一张 w×h 的 RGBA 图：给定矩形填不透明，其余透明。 */
  function image(w: number, h: number, rect: { x: number; y: number; width: number; height: number }) {
    const px = new Uint8Array(w * h * 4);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = (y * w + x) * 4;
        const inside =
          x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height;
        if (inside) {
          px[i] = 200;
          px[i + 1] = 180;
          px[i + 2] = 160;
          px[i + 3] = 255;
        }
      }
    }
    return px;
  }

  it("量出内容边界（雏雾那种：左右各约 58px 透明）", () => {
    const b = contentBounds(image(160, 184, { x: 58, y: 4, width: 44, height: 166 }), 160, 184);
    expect(b).not.toBeNull();
    expect(b).toEqual({ x: 58, y: 4, width: 44, height: 166 });
  });

  it("整张图都不透明时，边界就是整张图", () => {
    const b = contentBounds(image(160, 184, { x: 0, y: 0, width: 160, height: 184 }), 160, 184);
    expect(b).toEqual({ x: 0, y: 0, width: 160, height: 184 });
  });

  it("全透明 ⇒ 没有内容（null）：调用方据此退回旧摆法", () => {
    expect(contentBounds(new Uint8Array(160 * 184 * 4), 160, 184)).toBeNull();
  });

  it("半透明边缘（alpha≤10）不算内容", () => {
    const px = image(160, 184, { x: 50, y: 50, width: 60, height: 80 });
    // 把内容左边一列压成近乎透明 ⇒ 边界右移一列
    for (let y = 0; y < 184; y++) px[(y * 160 + 50) * 4 + 3] = 5;
    const b = contentBounds(px, 160, 184);
    expect(b?.x).toBe(51);
  });
});

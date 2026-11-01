// Live2D 摆位与内容测量的**纯几何层** —— 没有 React、没有 pixi，只有算术。
//
// 为什么从 `Live2dPet.tsx` 拆出来（2026-11-01）：那是一条组件文件，`react-refresh` 要求
// "只导出组件"，把纯函数寄在里面会让热更新整体失效（lint 的 `only-export-components` 判据）。
// 拆出来还顺带买第二件事：这一层可以脱离 pixi/WebGL/jsdom 直接测 —— 而"桌宠趴边看不见"
// 那种 bug 恰恰只在这层算术里，组件那半边怎么 mock 都测不到它。
//
// 口径统一：**画布 CSS 像素**、原点左上、Y 向下。像素读回来之后第一步就是换算到这个空间
// （见 `measureContent` 那头），后面所有算术都不该再碰"缓冲尺寸"那个口径。

/** 一块内容边界（模型**画布**坐标系里的像素，原点左上、Y 向下）。 */
export interface ContentBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * 判"这一格算不算画了东西"的 alpha 门槛。
 *
 * 是个**约定**而不是物理量：发梢、投影、抗锯齿边缘都是半透明，门槛越高框越紧。
 * 从内联字面量提成具名常数，是因为它现在同时决定两件事 —— 摆位用的几何，以及
 * 上报给壳的那块"画了像素的地方"（`PetPage` 拿它当点击命中区）。
 */
export const CONTENT_ALPHA_MIN = 10;

/**
 * 模型在盒里的摆位：**按内容**等比缩进盒里 + 脚踩下沿。
 *
 * **为什么按"内容"而不是按画布**（2026-11-01 修"桌宠看不见"）：壳的靠边隐藏原先假设
 * 「色片内容横向充满那 160 宽的盒」—— 趴右边缘时把窗推出去，让色片盒的**左侧 48px**
 * 留在屏内（`PET_PEEK_PX`），"露出来的那一条"就该是宠物本体。
 *
 * Live2D 的模型**画布**是 Cubism 的正方形画布，人物只占中间一小块，其余是透明边
 * （雏雾 hiyori 在真渲染进程上实测：160×184 的画布里人物只有 46×184，左右各约 57px
 * 透明）。按画布摆位时，屏内那 48px 恰好全落在**左边的透明边**里 —— 屏幕上**一个像素
 * 都没有**，用户看到的就是"桌宠不见了"，而窗活着、页面活着、模型也活着、hover 也能唤出来。
 *
 * **水平对齐居中，不左不右**（同一天收口）。第一版把内容无条件**左对齐**，那是拿"屏内
 * 永远只剩左侧那 48px"当唯一形态：趴**左**边缘时屏内是锚点的**右侧** 48px，左对齐的人物
 * 整个在屏外 —— 同一个 bug 的对称版本；而且不趴边时她也平白偏左。开源调研给的口径一致：
 * VPet 把"贴边该往里推多少"做成**形象自己声明**的数据（`GraphConfig.Data["side"]["left"]`），
 * 不是宿主里的常数。所以这里的分工是：**渲染层只管把内容量准并报出去，"露多少、往哪贴"
 * 由壳按当前吸的那条边算**（`shell/main/petDockGeometry.ts` 的 `tuckForEdge`）。
 *
 * 脚踩下沿（`y = box.height - height`）是"它站在地上"的感觉，与左右无关。
 */
export function live2dPlacement(
  content: ContentBox,
  box: { width: number; height: number },
): { scale: number; x: number; y: number; width: number; height: number } {
  // 按短边等比缩：内容原始尺寸通常远大于桌宠这一格。
  const scale = Math.min(box.width / content.width, box.height / content.height);
  const width = content.width * scale;
  const height = content.height * scale;
  return {
    scale,
    width,
    height,
    x: (box.width - width) / 2,
    y: box.height - height,
  };
}

/**
 * 画布里**内容**（非透明像素）的边界。
 *
 * 读像素只为量一次边界 —— 模型加载完、摆位前算一次就够，之后不再每帧读
 * （每帧读会把 WebGL 的回读开销压到桌宠这条链路上）。
 *
 * **为什么必须真的读像素**：Cubism 只给画布尺寸（`CanvasInfo`），人物在画布里占多大、
 * 偏在哪，全在**贴图与参数**里。库自己也没有"内容 bbox"这个概念 —— `pixi-live2d-display`
 * 在 `InternalModel` 的注释里写得很直白：`originalWidth` "doesn't represent the model's
 * real size, as the model can overflow from its canvas"。它的 `anchor` 同样只能对齐**画布**。
 * 顶点其实能量（`getDrawableBounds` 遍历 drawable 取并集），但那是**当前姿态**的，会随动作
 * 抖；而且顶点是**上界**（含被 mask 裁掉的部分）。对"贴边至少要露出一点"这个用途，
 * 一次性的像素实测更贴近眼睛看到的东西。
 *
 * **alpha=0 的格子一定不参与命中，所以框不会"漏掉看得见的像素"**；顶点并集偏大的方向
 * 也只会多算内容、不会露出透明 —— 两个方向的误差都不会重现"屏内零像素"。
 */
export function contentBounds(
  pixels: Uint8Array | Uint8ClampedArray,
  width: number,
  height: number,
): ContentBox | null {
  let minX = width;
  let minY = height;
  let maxX = -1;
  let maxY = -1;
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      // RGBA 的第 4 字节是 alpha：透明区 alpha≈0。
      if (pixels[(y * width + x) * 4 + 3] > CONTENT_ALPHA_MIN) {
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
  }
  if (maxX < 0) return null; // 一张空画布：没有"内容"可摆
  return { x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1 };
}

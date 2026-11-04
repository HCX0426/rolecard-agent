/**
 * 桌宠趴边（靠边隐藏）的几何 —— **不含 electron 依赖的纯函数层**。
 *
 * 为什么单拆一个文件（照 `updateCacheCleanup.ts` 的先例）：这一层判的是"该把窗推出去
 * 多少像素"，写错的代价是"桌宠趴边后整个看不见"（2026-11-01 真发生过）。而
 * `windows.ts` 顶部 `import { screen } from "electron"`，node 侧直接 require 它会当场炸，
 * 判据就永远只能靠真机量 —— 真机上又差点把"hover 得到"当成"看得见"（那次误判的根源）。
 * 拆出来就能像 `npm run test:cleanup` 那样真跑一遍全部读数。
 *
 * 口径：**锚点框**（色片那块 200×240）与**色片盒**（160×184）一律用 DIP/CSS 像素 ——
 * 与 `setBounds` / `getBounds` / 页面 `getBoundingClientRect` 同一个空间。150% 缩放的屏上
 * Electron 报的就是 DIP，物理像素只存在于截图里，这一层碰不到。
 */

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export type DockEdgeName = "left" | "right";

/** 锚点框尺寸（色片那块 200×240）。与 `windows.ts` 的 PET_WIDTH / PET_HEIGHT 同值。 */
export const ANCHOR_W = 200;
export const ANCHOR_H = 240;

/** 渲染层那一格的尺寸（`PetPage` 传给 Live2dPet / PetSprite 的那对数）。 */
export const SPRITE_W = 160;
export const SPRITE_H = 184;
/** 根节点的 `pb-1`：色片盒底下到锚点下沿的间距。 */
export const SPRITE_BOTTOM_GAP = 4;

/** 趴边之后仍留在屏内的**内容**宽度（= `windows.ts` 的 PET_PEEK_PX）。 */
export const PEEK_PX = 48;

/**
 * "窄就整条露"的放宽量：内容宽 ≤ PEEK + 这个数时，不裁到 PEEK，而是**全露**。
 *
 * 为什么需要它（2026-11-01 左边缘真机实测）：内容框是模型**加载完那一刻**量的一次，
 * 而人物在呼吸 —— 报上去的是宽 58，几秒后实测缩成宽 48、左沿还右移了 4px。壳照 58 算
 * "只露 48"，多推的那 6px 就把人的一截推出了屏幕（屏内只剩 42px）。
 * 与其猜一个边距（猜小了照样切、猜大了谁都藏不住），不如承认：**比露头宽不了多少的
 * 内容，藏它没有意义** —— 雏雾那种窄立绘总共才 48px，本来就该整条看得见。
 *
 * 为什么取 20：实测的姿态张合幅度是 10px（58 → 48），取两倍留量；再大就开始把该藏的
 * 藏不住了。这条同时**保住安全阀**：序列帧包内容宽 160，远超 48+20，走的还是"露 48"
 * 那条老路，一格行为都没变。
 */
export const FULL_REVEAL_SLACK = 20;

/**
 * 色片盒（160×184）在锚点框（200×240）里的位置：水平居中、底对齐。
 *
 * 这是**唯一的出处** —— 兜底 tuck 从它算，两边不会再各写一份而对不上
 * （2026-10-29 那次订正就是因为这两维抄成了旧值 88）。
 */
export const SPRITE_BOX: Rect = {
  x: Math.round((ANCHOR_W - SPRITE_W) / 2), // 20
  y: ANCHOR_H - SPRITE_BOTTOM_GAP - SPRITE_H, // 52
  width: SPRITE_W,
  height: SPRITE_H,
};

/** 内容充满色片盒时的旧常数：`SIDE_TUCK = 20 + 160 − 48 = 132`。 */
export const LEGACY_SIDE_TUCK = ANCHOR_W - SPRITE_BOX.x - PEEK_PX;

/**
 * 该把锚点框沿 `edge` 推出屏幕多少像素，才让**内容**（不是透明 padding）露出 PEEK。
 *
 * `content` 是页面上报的"画了像素的那一块"，**锚点框坐标**（原点 = 锚点左上角）。
 * null = 没报（序列帧包、模型还没加载完、旧界面、着陆页）⇒ 落旧常数，行为与今天逐格一致。
 *
 * **只管左右两条边**：纵向不吸（见 `dockEdgeFor`）—— 往下推必然把脚切掉，用户 2026-11-01
 * 拍了「底下和上都不需要隐藏」。原先这里有一条 bottom 式子，连同它的读数一起删了：留着一条
 * 永远不会被调用的分支，绿灯看着像覆盖、其实是空的，比没有更坏。
 *
 * **先钉死"屏内保留锚点的哪一段"——这是这段几何唯一容易算反的地方，别凭直觉改。**
 * 依据是 `windows.ts` 的 `flushRect` / `tuckedRect` 真实推法（贴边时锚点 x 左 `0`、
 * 右 `workR+1−200=1507`）：
 *  - right：锚点往右推 T ⇒ 锚点 local 的屏幕坐标 = `1507 + T + l`，屏内要求 ≤ workR
 *    ⇒ `l ≤ 199 − T` ⇒ 保留的是**左段 x∈[0 .. 199−T]**；
 *  - left ：锚点往左推 T ⇒ 屏内要求 `−T + l ≥ 0` ⇒ 保留的是**右段 x∈[T .. 199]**。
 *
 * 于是两条式子（`vis = min(PEEK, 内容宽)`：内容比 PEEK 还窄时整块都该露着）：
 *  - right ：屏内内容 = `min(199−T, x+w−1) − x + 1 = vis` ⇒ **T = ANCHOR_W − x − vis**
 *  - left  ：屏内内容 = `x+w−1 − max(T, x) + 1 = vis`     ⇒ **T = x + w − vis**
 *
 * **安全阀（这套改动敢上安装目录的唯一凭据）**：内容充满色片盒时（序列帧包 = 锚点内
 * x[20..179]）两条都必须算出旧常数 `200 − 20 − 48 = 20 + 160 − 48 = 132` = 旧 `SIDE_TUCK`。
 * 逐格相等 ⇒ 上报通道对老形态**零行为变化**，只治"人物只占画布中间一小块"的 Live2D
 * （雏雾 hiyori 真机实测内容只有 46×184，左右各约 57px 全透明 —— 按旧常数推 132 时屏内
 * 那 48px 全是透明边，屏幕上一个人像素都没有）。
 * ⚠ 两条式子互换的话，趴左那条会多推 ⇒ 人的一半出屏，而趴右那条只是少藏一点、看着没事
 * —— **同一个错在两条边上表现完全不同**（2026-11-01 用户报的"左边藏一半、右边不会"就是
 * 这个形状，只是当时错在上报值上）。钉在 `npm run test:dock`。
 *
 * 夹取：T ≥ 0（内容窄到比 PEEK 还小就整块露着，别把框推过头）；T ≤ 锚点宽 − 1
 * （永远留一条缝：窗彻底出屏就再也 hover 不回来了）。
 */
export function tuckForEdge(edge: DockEdgeName, content: Rect | null): number {
  if (!content) return LEGACY_SIDE_TUCK;
  // 内容**窄到与露头量一个量级**就整条露出来（`vis = 内容宽`），别去裁它：见
  // `FULL_REVEAL_SLACK` 那段——内容框只量一次而人物在呼吸，裁掉的正是会漂移的那几像素。
  // 宽内容（序列帧包 160）仍然裁到 48 ⇒ 旧常数逐格不变。
  const vis = content.width <= PEEK_PX + FULL_REVEAL_SLACK ? content.width : PEEK_PX;
  const raw =
    edge === "right" ? ANCHOR_W - content.x - vis : content.x + content.width - vis;
  return Math.round(Math.min(Math.max(raw, 0), ANCHOR_W - 1));
}

/**
 * 松手那一刻判：该吸哪条边。`rect` 是锚点框，`work` 是工作区，`snap` 是"算贴边"的距离闸。
 *
 * **只判左右**（2026-11-01 用户拍板「底下和上都不需要隐藏」）。原先有 bottom：往下推 T 之
 * 后屏内保留的是锚点的**上段**（头顶那条），"站在地上"的形象于是被从脚踝往上整条藏掉 ——
 * 对桌宠这是最坏的一种隐藏，一眼就是"缺了一截"而不是"收进了边里"。横向没这问题（左右推
 * 出去露的是身体的纵条，头脚都在）。top 从来没有过。
 *
 * 落空就是"普通摆放"：窗停在用户松手的地方，不吸、不推、不藏。旧偏好里存着 bottom 的那些
 * 也由这一条自然兜住（不必写迁移代码，也不会卡在"永远藏着打不开"）。
 */
export function dockEdgeFor(
  rect: Rect,
  work: Rect,
  snap: number,
): "left" | "right" | null {
  if (rect.x - work.x <= snap) return "left";
  if (work.x + work.width - (rect.x + rect.width) <= snap) return "right";
  return null;
}

/**
 * 校验页面上报的内容框并折算到**锚点坐标**。
 *
 * 与 `setPetHotRects` 同一套"输入不可信"的写法：非数组、非数字、尺寸 ≤0、越界的一律当没报
 * （返回 null ⇒ 调用回落旧常数，**不会因为页面乱报而把窗推出屏幕**）。
 *
 * `canvasOffset` 是锚点左上角在画布里的偏移（x=CANVAS_SIDE_SLACK=180、
 * y=PET_CANVAS_HEIGHT−PET_HEIGHT=280）：页面报的是画布坐标，而壳的几何全按锚点算，
 * 换算只发生在这一处。
 */
export function normalizeContentRect(
  rect: unknown,
  canvasOffset: { x: number; y: number },
): Rect | null {
  if (!Array.isArray(rect) || rect.length !== 4) return null;
  const nums = rect.map((n) =>
    typeof n === "number" && Number.isFinite(n) ? Math.round(n) : Number.NaN,
  );
  if (nums.some((n) => Number.isNaN(n))) return null;
  const [x, y, width, height] = nums as [number, number, number, number];
  if (width <= 0 || height <= 0 || width > 4000 || height > 4000) return null;
  const out: Rect = { x: x - canvasOffset.x, y: y - canvasOffset.y, width, height };
  // 报出锚点框的内容不该换来"多推一点"：夹回锚点内。宁可少露，不可把窗整个推出屏。
  out.x = Math.min(Math.max(out.x, 0), ANCHOR_W - 1);
  out.y = Math.min(Math.max(out.y, 0), ANCHOR_H - 1);
  out.width = Math.min(out.width, ANCHOR_W - out.x);
  out.height = Math.min(out.height, ANCHOR_H - out.y);
  return out;
}

/**
 * 把矩形夹回**工作区**：保尺寸只挪位置；矩形比工作区还大时贴工作区原点。
 *
 * （A-11 归一，2026-11-04）这条数学从前有**三份近亲写法**：`state.ts` 的
 * `clampToWorkArea`、`windows.ts` `petAnchorFor` 的行内八行、`flushRect` 只夹 y 的
 * 标量化半份 —— 三处各自手推 `maxX = work.x + Math.max(0, work.width − width)`。
 * 收成这一个纯函数：唯一读法、`npm run test:dock` 真跑得着（纯层不碰 electron，
 * 正是本文件存在的理由），三个调用点不再各信各的。
 *
 * `work` 是**参数**而不是函数里去 `screen.getPrimaryDisplay()`：调用点自己取屏。
 * 归一当天四条调用路都仍给主屏工作区 —— 零行为变化；多屏按 `getDisplayMatching`
 * 取屏是另一件事，没真机读数之前不动（见 A-11 收尾备注）。
 */
export function clampRectToWorkArea(rect: Rect, work: Rect): Rect {
  const maxX = work.x + Math.max(0, work.width - rect.width);
  const maxY = work.y + Math.max(0, work.height - rect.height);
  return {
    ...rect,
    x: Math.min(Math.max(rect.x, work.x), maxX),
    y: Math.min(Math.max(rect.y, work.y), maxY),
  };
}

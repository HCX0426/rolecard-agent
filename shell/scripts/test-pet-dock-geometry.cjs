// 桌宠趴边几何的真跑测试（纯函数，不引测试框架）。
//
// 为什么必须有它：`tuckForEdge` 判的是"该把窗推出屏幕多少像素"。写错的代价不是报错，
// 而是**桌宠趴边后屏内少几像素、直到整个人不见**——2026-11-01 一天里踩实了三种：
//   ① 按盒子的常数推（132）而人物只占画布中间 46px ⇒ 屏内零像素 = "桌宠不见了"；
//   ② right/left 两条式子互换 ⇒ 一条边 0px、另一条边只是"少藏一点"，**只验一条会漏**；
//   ③ 内容窄时裁到 peek(48)，而内容框只量一次、人物在呼吸（实测 58 → 48、左沿右移 4px）
//      ⇒ 屏内只剩 42px，就是用户报的"左边会隐藏一半，右边不会"。
// 这三种在类型上全都合法，只在真机量得出来；而真机验证本身又容易把"hover 得到"当成
// "看得见"（这一天就误判过一轮）。所以这里把两条边的读数、"内容充满时必须等于旧常数
// 132"这条安全阀、以及②③那两个具体形状全部钉死。
//
// 纵向（bottom/top）不在这里：它已经从 `DockEdge` 类型里删掉了 —— 往下推必然把脚切掉，
// 用户拍的「底下和上都不需要隐藏」。判它不产出边的两条 `dockEdgeFor` 读数在下面。
//
// 跑法（cwd=shell）：npm run test:dock   （先 npm run build 再跑这里）
// 门禁：gate.py 的「shell 桌宠趴边几何」步骤（full 档）+ ci.yml 的那一步。

const assert = require("node:assert/strict");
const path = require("node:path");

const G = require(path.join(__dirname, "..", "out", "main", "petDockGeometry.js"));

const results = [];
function check(name, fn) {
  try {
    fn();
    results.push({ name, ok: true });
    console.log(`✅ ${name}`);
  } catch (e) {
    results.push({ name, ok: false, err: e.message });
    console.log(`❌ ${name}\n   ${e.message}`);
  }
}

const { ANCHOR_W, ANCHOR_H, PEEK_PX, SPRITE_BOX, LEGACY_SIDE_TUCK } = G;

console.log(
  `格子：锚点 ${ANCHOR_W}×${ANCHOR_H}｜色片盒 ${SPRITE_BOX.width}×${SPRITE_BOX.height} @(${SPRITE_BOX.x},${SPRITE_BOX.y})｜peek ${PEEK_PX}`,
);

check("锚点与色片盒的读数就是设计稿那几格", () => {
  assert.equal(ANCHOR_W, 200);
  assert.equal(ANCHOR_H, 240);
  assert.equal(SPRITE_BOX.width, 160);
  assert.equal(SPRITE_BOX.height, 184);
  assert.equal(SPRITE_BOX.x, 20);
  assert.equal(SPRITE_BOX.y, 52); // 240 − 4(pb-1) − 184
  assert.equal(PEEK_PX, 48);
});

// ---- 安全阀：内容充满色片盒时，必须逐格等于改掉之前的常数 --------------------------
// 这一条是整个改动敢上安装目录的唯一凭据：它证明"上报通道"对序列帧包是**零行为变化**。
check("内容充满盒子 ⇒ 左右两条都退回旧常数 132（序列帧零行为变化）", () => {
  const full = { x: SPRITE_BOX.x, y: SPRITE_BOX.y, width: SPRITE_BOX.width, height: SPRITE_BOX.height };
  assert.equal(G.tuckForEdge("right", full), 132);
  assert.equal(G.tuckForEdge("left", full), 132);
  // 兜底数就是导出的那个常数本身（不能一个对、一个"碰巧"）
  assert.equal(LEGACY_SIDE_TUCK, 132);
  assert.equal(G.tuckForEdge("right", null), LEGACY_SIDE_TUCK);
  assert.equal(G.tuckForEdge("left", null), LEGACY_SIDE_TUCK);
});

// ---- 两条边的方向：屏内保留的分别是锚点的左段 / 右段 --------------------------------
// 纵向不吸（见下面 dockEdgeFor 那两条），所以这里没有 bottom 的事 —— 原先那条式子连同
// 它的读数一起删了：留一条永不被调用的分支，绿灯看着像覆盖、其实是空的，比没有更坏。
/** 屏内保留的锚点 local 区间（照 windows.ts 的 flushRect/tuckedRect 真实推法反推）。 */
function visibleSpan(edge, tuck) {
  if (edge === "right") return [0, ANCHOR_W - 1 - tuck]; // 往右推 ⇒ 留左段
  return [tuck, ANCHOR_W - 1]; // 往左推 ⇒ 留右段
}
/** 屏内那一段与内容的交（= 用户真能看到的像素宽）。 */
function visibleContent(edge, content) {
  const t = G.tuckForEdge(edge, content);
  const [lo, hi] = visibleSpan(edge, t);
  return Math.max(0, Math.min(hi, content.x + content.width - 1) - Math.max(lo, content.x) + 1);
}

check("Live2D 那种窄立绘：左右两条边屏内都有像素（不许再出现零）", () => {
  // 雏雾 hiyori 的真机读数：量完摆位后**实测**落在锚点内 x[62..107]（宽 46）。
  // 这一格是壳实际收到的上报值（第二次实测之后），不是推算值。
  const hiyori = { x: 62, y: 0, width: 46, height: 184 };
  for (const edge of ["right", "left"]) {
    const v = visibleContent(edge, hiyori);
    assert.ok(v > 0, `${edge} 屏内零像素 —— 这就是"桌宠不见了"`);
    assert.equal(v, Math.min(PEEK_PX, hiyori.width), `${edge} 屏内 ${v}px，应 ${Math.min(PEEK_PX, hiyori.width)}px`);
  }
});

check("病根本身钉住：内容居中那份读数用旧常数推 ⇒ 屏内 0 像素", () => {
  // 这一条不是回归测试，是"2026-11-01 为什么会看不见"的证据，所以用的是**当年**的读数：
  // 那版还没做内容摆位，人物按画布居中落在锚点内 x[78..121]（CDP 实测 bbox x[58..102]
  // 加 SIDE_PAD 20）。旧常数 132 之后屏内只剩锚点 [0..67] —— 与内容一格都不搭 ⇒ 零像素。
  // 删掉旧常数以后的人不知道自己删掉了什么，这条就是留给他看的。
  const asDocked = { x: 78, y: 0, width: 44, height: 184 };
  const spanAtLegacy = visibleSpan("right", LEGACY_SIDE_TUCK); // [0..67]
  const seen = Math.max(
    0,
    Math.min(spanAtLegacy[1], asDocked.x + asDocked.width - 1) - Math.max(spanAtLegacy[0], asDocked.x) + 1,
  );
  assert.equal(seen, 0, "旧常数本该算出零像素（否则这份读数或这段推导记错了）");
  // 同一份内容交给新公式：两条边各露 44px（人物整条都在屏内，因为它比 peek 还窄）
  assert.equal(G.tuckForEdge("right", asDocked), 78);
  assert.equal(G.tuckForEdge("left", asDocked), 78);
  assert.equal(visibleContent("right", asDocked), 44);
  assert.equal(visibleContent("left", asDocked), 44);
});

check("内容充满盒子时，新公式与旧常数对两条边都给出同一个数", () => {
  // 与上面那条安全阀同一件事，但走的是"屏内像素数"的判据（不只是 tuck 的数）
  const full = { x: SPRITE_BOX.x, y: SPRITE_BOX.y, width: SPRITE_BOX.width, height: SPRITE_BOX.height };
  for (const edge of ["right", "left"]) {
    assert.equal(visibleContent(edge, full), PEEK_PX, `${edge} 充满时应正好露 48`);
  }
});

check("宽内容（比 peek 宽）：两条边都正好露出 peek", () => {
  const wide = { x: 60, y: 52, width: 100, height: 184 };
  assert.equal(visibleContent("right", wide), PEEK_PX);
  assert.equal(visibleContent("left", wide), PEEK_PX);
});

check("right 与 left 的式子不许互换（互换会把人物推到屏外）", () => {
  // 内容偏左（锚点内 x[20..79]，宽 60）。宽 60 落在"窄就整条露"的放宽带里（60 ≤ 48+20），
  // 所以 vis = 60 而不是 48：
  //   right 保留锚点【左段】⇒ T = 200 − 20 − 60 = **120** ⇒ 屏内 [0..79]，内容整条 60px
  //   left  保留锚点【右段】⇒ T = 20 + 60 − 60   = **20**  ⇒ 屏内 [20..199]，也整条
  // 互换的话：left 会用成 T=120 ⇒ 屏内 [120..199] 与内容 [20..79] 的交 = **0px**，人整个
  // 看不见；而 right 用成 T=20 只是少藏一点（屏内照样 60px），**看着没事**。
  // 这就是 2026-11-01 那个错只在一条边上露出来的机理 —— 同一个错两条边症状不同，
  // 当年我只验了右边就宣布修好了。
  const leftish = { x: 20, y: 52, width: 60, height: 184 };
  assert.equal(G.tuckForEdge("right", leftish), 120);
  assert.equal(G.tuckForEdge("left", leftish), 20);
  assert.equal(visibleContent("right", leftish), 60);
  assert.equal(visibleContent("left", leftish), 60);
  const swapped = visibleSpan("left", G.tuckForEdge("right", leftish)); // [120..199]
  assert.equal(
    Math.max(
      0,
      Math.min(swapped[1], leftish.x + leftish.width - 1) - Math.max(swapped[0], leftish.x) + 1,
    ),
    0,
    "互换后趴左该整条看不见（0px）—— 这就是要防的那个错",
  );
  // 内容偏右的那一半同样要成立（"两条边各自成立"而不是"蒙对一条"）
  const rightish = { x: 120, y: 52, width: 60, height: 184 };
  assert.ok(visibleContent("right", rightish) > 0);
  assert.ok(visibleContent("left", rightish) > 0);
});

check("宽内容走的是裁到 peek 那条老路（放宽带只管窄的）", () => {
  const wide = { x: 60, y: 52, width: 100, height: 184 }; // 100 > 48+20 ⇒ vis=48
  assert.equal(G.tuckForEdge("right", wide), 200 - 60 - 48);
  assert.equal(G.tuckForEdge("left", wide), 60 + 100 - 48);
  assert.equal(visibleContent("right", wide), PEEK_PX);
  assert.equal(visibleContent("left", wide), PEEK_PX);
});

check("内容比 peek 还窄 ⇒ 整块露着（T 夹到 0，不把框推过头）", () => {
  const sliver = { x: 150, y: 52, width: 6, height: 184 };
  const t = G.tuckForEdge("right", sliver);
  assert.ok(t >= 0);
  assert.equal(visibleContent("right", sliver), Math.min(PEEK_PX, sliver.width));
  // 内容贴锚点右沿的细条：往右推到底也不能超过锚点宽 − 1（留一条缝才 hover 得回来）
  const maxT = G.tuckForEdge("left", { x: 0, y: 0, width: ANCHOR_W, height: ANCHOR_H });
  assert.ok(maxT <= ANCHOR_W - 1, `T=${maxT} 越界：窗会彻底出屏`);
});

// ---- 窄内容整条露，不裁到 peek（2026-11-01 左边缘真机实测换来的） --------------------
check("内容宽在 peek 上下一个量级 ⇒ 整条露出来（姿态会呼吸，裁它必切）", () => {
  const SLACK = G.FULL_REVEAL_SLACK;
  assert.equal(SLACK, 20);
  // 真机报上去的那一份（`桌宠内容框上报：[251,332,58,184]` 减去锚点偏移 180 ⇒ x=71 宽=58，
  // 比 peek 宽 10，正落在放宽带里）。
  const reported = { x: 71, y: 0, width: 58, height: 184 };
  assert.equal(G.tuckForEdge("left", reported), 71); // x + w − vis，vis=w ⇒ 整条露
  assert.equal(G.tuckForEdge("right", reported), ANCHOR_W - 71 - 58);
  // 同一个模型几分钟后**实测**：缩成宽 48、左沿右移 4px（呼吸/动作）。这就是当时"只剩 42px"
  // 的那一份读数 —— 探针量到人物落在屏幕 x[-6..41]，锚点 L=-81 ⇒ 锚点局部 [75..122]。
  const drifted = { x: 75, y: 0, width: 48, height: 184 };
  const spanNew = visibleSpan("left", G.tuckForEdge("left", reported)); // 壳照上报值摆的位
  const seenNew = Math.max(
    0,
    Math.min(spanNew[1], drifted.x + drifted.width - 1) - Math.max(spanNew[0], drifted.x) + 1,
  );
  assert.equal(seenNew, drifted.width, `放宽后仍被切：屏内只剩 ${seenNew}/${drifted.width}`);
  // 老写法（vis = peek = 48）对同一份上报算出 T=81 ⇒ 屏内 [81..199] 与内容 [75..122] 的交
  // = 42px —— **正是用户报的"左边会隐藏一半"**。把病根钉住，别哪天又被"改回去"。
  const tuckOld = 71 + 58 - PEEK_PX;
  assert.equal(tuckOld, 81);
  const spanOld = visibleSpan("left", tuckOld);
  const seenOld = Math.max(
    0,
    Math.min(spanOld[1], drifted.x + drifted.width - 1) - Math.max(spanOld[0], drifted.x) + 1,
  );
  assert.equal(seenOld, 42, `老写法本该切到 42px（真机量到的就是这个数）`);
  // 同一份数据在**右边**：老写法算出的屏内 [0..118] 与内容 [75..122] 的交 = 44px ——
  // 也切了，只是比左边的 42px 轻，而轻到肉眼几乎看不出。**同一个错在两条边上症状不同**，
  // 这就是"只验一条边会漏"的确凿数字（当年我就是只验了右边就说它修好了）。
  const spanRightOld = visibleSpan("right", ANCHOR_W - reported.x - PEEK_PX);
  const seenRightOld = Math.max(
    0,
    Math.min(spanRightOld[1], drifted.x + drifted.width - 1) - Math.max(spanRightOld[0], drifted.x) + 1,
  );
  assert.equal(seenRightOld, 44, "右边老写法该切 4px（比左边轻，但不是没事）");
  // 新写法两条边都该整条露着
  assert.equal(
    Math.min(
      Math.min(visibleSpan("right", G.tuckForEdge("right", reported))[1], drifted.x + drifted.width - 1) -
        Math.max(0, drifted.x) + 1,
      Math.min(
        visibleSpan("left", G.tuckForEdge("left", reported))[1],
        drifted.x + drifted.width - 1,
      ) -
        Math.max(visibleSpan("left", G.tuckForEdge("left", reported))[0], drifted.x) + 1,
    ),
    drifted.width,
    "新写法该让内容整条都在屏内",
  );
});

check("宽内容不受放宽影响：序列帧包照旧裁到 peek、tuck 仍是 132", () => {
  const full = { x: SPRITE_BOX.x, y: SPRITE_BOX.y, width: SPRITE_BOX.width, height: SPRITE_BOX.height };
  assert.ok(full.width > PEEK_PX + G.FULL_REVEAL_SLACK); // 160 远离放宽带
  assert.equal(G.tuckForEdge("right", full), 132);
  assert.equal(G.tuckForEdge("left", full), 132);
  assert.equal(visibleContent("right", full), PEEK_PX);
  assert.equal(visibleContent("left", full), PEEK_PX);
});

// ---- 判边：纵向不吸（用户 2026-11-01「底下和上都不需要隐藏」） ------------------------
const WORK = { x: 0, y: 0, width: 1707, height: 1019 };
const anchorAt = (x, y) => ({ x, y, width: ANCHOR_W, height: ANCHOR_H });
check("dockEdgeFor：贴左/贴右照样吸，贴下/贴上不吸", () => {
  assert.equal(G.dockEdgeFor(anchorAt(0, 500), WORK, 24), "left");
  assert.equal(G.dockEdgeFor(anchorAt(4, 500), WORK, 24), "left"); // ≤ snap 就算贴
  assert.equal(G.dockEdgeFor(anchorAt(30, 500), WORK, 24), null); // 差一点就不吸
  assert.equal(G.dockEdgeFor(anchorAt(WORK.width - ANCHOR_W, 500), WORK, 24), "right");
  assert.equal(G.dockEdgeFor(anchorAt(WORK.width - ANCHOR_W - 4, 500), WORK, 24), "right");
  // 纵向：贴到工作区下沿 / 屏顶都**不该**产出边（旧版在这里返回 "bottom"）
  assert.equal(G.dockEdgeFor(anchorAt(800, WORK.height - ANCHOR_H), WORK, 24), null);
  assert.equal(G.dockEdgeFor(anchorAt(800, 0), WORK, 24), null);
  // 贴右下角：判到左右那两条之一，绝不判成 bottom
  assert.equal(G.dockEdgeFor(anchorAt(WORK.width - ANCHOR_W, WORK.height - ANCHOR_H), WORK, 24), "right");
});

check("dockEdgeFor：任何输入都不会产出 bottom/top（切脚的源头）", () => {
  for (let x = -30; x <= WORK.width; x += 7) {
    for (let y = -30; y <= WORK.height; y += 11) {
      const e = G.dockEdgeFor(anchorAt(x, y), WORK, 24);
      assert.ok(e === null || e === "left" || e === "right", `判出了不该存在的边：${e} @(${x},${y})`);
    }
  }
});

// ---- 上报输入不可信 ------------------------------------------------------------------
const CANVAS_OFFSET = { x: 180, y: 280 }; // 锚点左上角在画布里的偏移
check("normalizeContentRect：画布坐标折算成锚点坐标", () => {
  // 色片整盒在画布里是 (200,332) 起 160×184 ⇒ 锚点内 (20,52)
  assert.deepEqual(G.normalizeContentRect([200, 332, 160, 184], CANVAS_OFFSET), {
    x: 20,
    y: 52,
    width: 160,
    height: 184,
  });
});

check("normalizeContentRect：乱报一律当没报（不能让页面把桌宠推出屏幕）", () => {
  for (const bad of [
    null,
    undefined,
    [],
    [1, 2, 3],
    [1, 2, 3, 4, 5],
    ["200", 332, 160, 184],
    [200, 332, 160, Number.NaN],
    [200, 332, 0, 184], // 宽 0
    [200, 332, 160, -5], // 负高
    [200, 332, 99999, 184], // 越界
    [Infinity, 0, 10, 10],
  ]) {
    assert.equal(G.normalizeContentRect(bad, CANVAS_OFFSET), null, `该当没报：${JSON.stringify(bad)}`);
  }
  // 空数组（页面没量到内容时报的那一条）也必须当没报
  assert.equal(G.normalizeContentRect([], CANVAS_OFFSET), null);
});

check("normalizeContentRect：报出锚点框的内容被夹回锚点内", () => {
  const out = G.normalizeContentRect([0, 0, 560, 520], CANVAS_OFFSET);
  assert.ok(out.x >= 0 && out.y >= 0);
  assert.ok(out.x + out.width <= ANCHOR_W, `宽越界 ${out.x + out.width}`);
  assert.ok(out.y + out.height <= ANCHOR_H, `高越界 ${out.y + out.height}`);
  // 夹完之后仍然算得出一个合法的 tuck（不许 NaN）。只有左右两条：纵向不吸边，
  // `tuckForEdge` 的入参类型也已经收成 "left" | "right" —— 别再写回 "bottom"，
  // 那会走进 left 分支照样算出个数，绿灯是假的。
  for (const e of ["right", "left"]) {
    const t = G.tuckForEdge(e, out);
    assert.ok(Number.isFinite(t) && t >= 0 && t <= ANCHOR_W, `${e}=${t}`);
  }
});

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} 通过`);
if (failed.length) {
  console.log("失败：" + failed.map((f) => f.name).join("；"));
  process.exit(1);
}

// 这一层测不到的东西，说清楚，免得后来人以为"绿灯 = 三条边都验过"：
//   · **纵向不吸边**已经钉在上面两条 `dockEdgeFor` 读数里了（判不出 bottom/top 就没机会切脚）。
//   · 剩下的只有**报上来的内容框是不是实测值**：同一个误差在趴左/趴右上表现完全不同
//     （多推 22px 把人藏掉一半，少推 13px 看着没事），这条只能在真机上量 ——
//     跑 `node scripts/js/verify_pet_dock.cjs`，它会把 CDP 实测的 bbox 与壳实际推出的距离
//     并排打出来。**别用屏摄**：那扇窗是透明置顶窗，截图会被别的窗盖住（2026-11-01 骗过一轮）。

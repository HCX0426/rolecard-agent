// @vitest-environment jsdom
/**
 * 首帧脚本（`frontend/index.html` 里那段内联）与 `appearance.ts` 的 **逐值对拍**。
 *
 * 为什么要有这一支（2026-11-04）：透明度→实心度这份算术住在两处 —— `bgSolidity()`
 * 是权威，`index.html` 那份是**手抄的镜像**（它跑在打包前的裸 HTML 里，import 不到那边）。
 * 一致性尺子（`scripts/consistency` 的「外观默认值单一真值」）钉的是**兜底数字**与
 * **各自只写一条赋值**这两件文本形状，**钉不住算术**：把镜像里的
 * `Math.max(0, Math.min(100, 100 - a))` 改成别的写法、或删掉 `Math.round`，尺子照样绿，
 * 而首帧会画出一个与 React 起来之后不同的值 —— 正是这份文件要拦的。
 *
 * 关键做法：测试**真去执行 `index.html` 里那段脚本本体**（读文件 → 抠内联 → `new Function`
 * 跑），不是在测试里再抄一份镜像逻辑。抄一份就成了第三处实现，那份抄错了这条测试也测不出。
 * 可行性是实测过的：jsdom 下 `matchMedia` 挂在 globalThis（`test-setup.ts` 补的桩），
 * 抠出的脚本跑 `alpha=30` 得到 `--bg-solid:70`，不抛。
 *
 * ⚠ 首帧脚本整体包在 try/catch 里（catch 里那句注释是"留给 CSS 默认值"）—— 它会把中途的
 * 抛错吞成一个"什么都没写"的静默结果。所以下面每个用例都先断言 `--bg-solid` **非空**
 * （非脏值那一批），否则脏输入那一路会拿空串当读数假绿。
 */
// `?raw` 走 Vite 的导入器而不是 node:fs：前端 tsconfig 只挂 `vite/client`（浏览器工程，
// 没有 @types/node），用 fs/process 会让 `tsc --noEmit` 红。附带一个好处 —— 路径写死成
// 构建期就能解析的 `../../index.html`，文件被挪走时 vitest 直接报解析失败，
// 不会像运行时读盘那样"读不到 ⇒ 空字符串 ⇒ 断言悄悄通过"。
import rawIndexHtml from "../../index.html?raw";

import { beforeEach, describe, expect, it } from "vitest";

import { ALPHA_DEFAULT, ALPHA_MAX, SOLID_DEFAULT, bgSolidity, initAppearance } from "./appearance";

/** 抠出 `index.html` 里那段首帧内联脚本的正文（只有一个内联 script，其余是 type="module"）。 */
function firstFrameSource(): string {
  const html = String(rawIndexHtml);
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  if (scripts.length !== 1) {
    // 尺子的输入变了（多/少一段内联）就大声红，不猜哪一段是要执行的那段。
    throw new Error(`index.html 里内联 script 段数 = ${scripts.length}，预期 1 段`);
  }
  return scripts[0];
}


/** 清干净 <html> 的内联与 localStorage，回到"全新机器"的起点。 */
function resetDom(): void {
  document.documentElement.removeAttribute("style");
  document.documentElement.removeAttribute("data-has-bg");
  document.documentElement.removeAttribute("data-material");
  document.documentElement.removeAttribute("data-palette");
  localStorage.clear();
}

/** 真跑首帧脚本，返回它写到 --bg-solid 上的值（没写就是空串）。 */
function runFirstFrame(): string {
  document.documentElement.removeAttribute("style");
  // 真执行 index.html 里那份原文 —— 不接受在测试里再抄一份镜像逻辑（抄了就成第三处实现，
  // 那份抄错时这条测试根本测不出）。new Function 是有意的：本仓 eslint 没开 no-new-func，
  // 所以这里不放 eslint-disable（放了一条"关掉一个没启用的规则"的指令 = 无用指令警告）。
  new Function(firstFrameSource())();
  return document.documentElement.style.getPropertyValue("--bg-solid");
}

/** React 侧同一条链路的落值（`initAppearance` 是唯一写路径，非测试造的假逻辑）。 */
function runReact(): string {
  document.documentElement.removeAttribute("style");
  initAppearance();
  return document.documentElement.style.getPropertyValue("--bg-solid");
}

describe("首帧脚本与 bgSolidity 逐值对拍（镜像不许漂）", () => {
  beforeEach(() => {
    resetDom();
  });

  it("用户能存出来的每一个整数档，两路落值一致", () => {
    // 滑块/输入框存的永远是 0..100 整数（`setAlpha` 过 `clampAlpha`，含 Math.round），
    // 所以这是**真实可达域**：这一档从前两路本就一致，对拍守住的是"以后别再漂开"。
    for (let a = 0; a <= ALPHA_MAX; a++) {
      localStorage.setItem("rolecard.alpha", String(a));
      const frame = runFirstFrame();
      const react = runReact();
      expect(frame, `alpha=${a} 首帧没写值`).not.toBe("");
      expect(frame, `alpha=${a}`).toBe(react);
      expect(Number(frame), `alpha=${a}`).toBe(bgSolidity(a));
    }
  });

  it("小数 / 越界 / 缺键也都对得上（镜像已含 round，与 clampAlpha 同序）", () => {
    // 这些值今天的写入路径存不出来（setAlpha 必 round），但**盘上历史值 / 手改**可 reaching，
    // 而且这条才是 2026-11-04 那次改动真正加固的一档：
    // 镜像从前少一个 round，`33.7` 首帧算 66.3、React 算 66 —— 同一份偏好两个值。
    for (const raw of ["33.7", "33.2", "0.5", "99.5", "-5", "150", "0", "100", ""]) {
      if (raw === "") localStorage.removeItem("rolecard.alpha");
      else localStorage.setItem("rolecard.alpha", raw);
      const frame = runFirstFrame();
      const react = runReact();
      expect(frame, `raw=${JSON.stringify(raw)}`).toBe(react);
    }
    // 小数那两档单独钉死数值，防止"两边一起算错同一个值"仍相等（对拍查不出共谋）
    localStorage.setItem("rolecard.alpha", "33.7");
    expect(runFirstFrame()).toBe("66");
    expect(runReact()).toBe("66");
  });

  it("脏值（读不成数）：首帧不写、React 写默认档，而两者渲染相同", () => {
    // 这一档是两路**唯一**的真实不对称，必须如实钉住而不是假装相等：
    //   · 首帧 `isFinite(a)` 为假 ⇒ 什么都不写，值留给 index.css 的 `--bg-solid` 默认；
    //   · React 的 `readAlpha` 把"读不成数"与"没设过"一并归到 `ALPHA_DEFAULT` ⇒ 显式写 100。
    // 之所以屏幕上看不出差别：CSS 那份默认值 == `SOLID_DEFAULT` == `bgSolidity(ALPHA_DEFAULT)`，
    // 下面最后一句就是把这条等价**当场钉住** —— 它一旦分开（改了默认档没同步 CSS），
    // 这个"看似相同"就崩了，而那正是最该红的时刻。
    for (const raw of ["abc", "Infinity", "NaN"]) {
      localStorage.setItem("rolecard.alpha", raw);
      expect(runFirstFrame(), `脏值 ${raw} 首帧不该写内联`).toBe("");
      expect(runReact(), `脏值 ${raw} React 该落默认档`).toBe(String(SOLID_DEFAULT));
    }
    expect(SOLID_DEFAULT).toBe(bgSolidity(ALPHA_DEFAULT));
    expect(SOLID_DEFAULT).toBe(ALPHA_MAX); // 出厂 alpha=0 ⇒ 全实
  });

  it("镜像里确实写了 round（防有人把它删回去而对拍仍绿）", () => {
    // 对拍只在"两边同错"时失效，所以这一句直接查**源文本**：round 是 2026-11-04 归一时
    // 补进镜像的那一步，删掉它 `33.7` 那档会漂 —— 上面那条会红，但这一条把"为什么要有它"
    // 留在测试里，而不是只留在提交信息里。
    expect(firstFrameSource()).toMatch(/Math\.round\(/);
  });
});

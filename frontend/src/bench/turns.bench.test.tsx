// @vitest-environment jsdom
/** 长会话首渲染基准 —— 用数据回答"要不要虚拟滚动"。
 *
 *  2026-10-24 实测（本文件）：2000 条 = 首渲染 1361ms / 18k DOM 节点（jsdom 保守值，
 *  真实浏览器更快）；会话是渐进增长的，没有"全量重渲"的热路径 ⇒ **虚拟滚动暂不做**。
 *  触发线：这里跑到 >3s，或真机上打开长会话出现可感知卡顿 —— 到那天再重开这个题。
 *
 *  默认跳过（不占日常测试时长），跑法：`BENCH=1 npx vitest run src/bench`
 */
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Markdown } from "../components/Markdown";

// 开关用显式常量而非 process.env：前端 tsconfig 的 types 只有 vite/client，
// 为一个基准引入 @types/node 不值当 —— 跑基准时把这行翻成 true，跑完翻回来。
const RUN = false;

const codeSample = "```python\ndef f(x):\n    return x * 2\n```";

function msg(i: number) {
  return i % 3 === 0
    ? `第 ${i} 条：这是一段普通中文回复，长度中等，用来模拟真实对话的密度。`
    : `第 ${i} 条：带一点结构。\n\n- 要点一\n- 要点二\n\n${codeSample}`;
}

(RUN ? describe : describe.skip).each([100, 500, 1000, 2000])(
  "长会话渲染 %d 条",
  (n) => {
    it(`Markdown × ${n} 首渲染`, () => {
      const text = Array.from({ length: n }, (_, i) => msg(i)).join("\n\n");
      const t0 = performance.now();
      render(<Markdown text={text} />);
      const ms = performance.now() - t0;
      const nodes = document.querySelectorAll("*").length;
       
      console.info(`[bench] n=${n} 首渲染 ${ms.toFixed(0)}ms, DOM 节点 ${nodes}`);
      expect(ms).toBeGreaterThan(0);
    });
  },
);

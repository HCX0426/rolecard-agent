// 测试环境的最小补齐。
//
// jsdom 只实现了 DOM 的"数据"部分，缺一批"布局与滚动"相关的 API：
//   · `matchMedia`      —— useTheme 用它读系统深色偏好；
//   · `Element.scrollTo` —— 聊天页在消息变化时自动滚到底部。
//
// 不补的话，组件测试的失败原因会是一句 `xxx is not a function`，与"被测逻辑有没有问题"
// 完全无关 —— 这种噪音会让人不再信任测试。补齐是"让环境够用"，不是"把断言改松"。

import { afterEach } from "vitest";

if (typeof window !== "undefined") {
  if (!window.matchMedia) {
    window.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
  }
  if (!Element.prototype.scrollTo) {
    Element.prototype.scrollTo = (() => undefined) as unknown as Element["scrollTo"];
  }
}

afterEach(async () => {
  // 只在有 DOM 的文件里做清理；node 环境没有 document。
  if (typeof document === "undefined") return;
  const { cleanup } = await import("@testing-library/react");
  cleanup();
});

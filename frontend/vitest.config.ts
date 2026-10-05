import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/**
 * 前端测试配置。
 *
 * 默认环境是 **node**（不是 jsdom）：`api.test.ts` 测的是 fetch 封装与 SSE 解析，
 * 用的是 Node 原生的 `Response` / `ReadableStream` —— 而 jsdom **不提供**这两个全局对象，
 * 把全局环境换成 jsdom 会让那批既有测试找不到构造器。
 *
 * 需要 DOM 的组件测试在文件首行用 `// @vitest-environment jsdom` 单独声明，
 * 这样就得到"每个文件用它对的环境"，而不是让所有人迁就最重的那个。
 *
 * 覆盖率（2026-10-04 审查快照「前端 359 用例零覆盖率度量」那条）：`npm test` 自带
 * `--coverage`，只给 `src/lib` 与 `src/hooks` 立线（逻辑层，行覆盖 ≥90）—— 页面与
 * 组件**只报告不设限**：它们的行数大头是 JSX 结构，把线设在那里只会逼人写凑数断言。
 * thresholds 只写 glob 键、不写顶层键：没有全局线，新增目录不会莫名变红。
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["./src/test-setup.ts"],
    coverage: {
      provider: "v8",
      reporter: ["text"],
      include: ["src/**"],
      thresholds: {
        "src/lib/**": { lines: 90 },
        "src/hooks/**": { lines: 90 },
      },
    },
  },
});

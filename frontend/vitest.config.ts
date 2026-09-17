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
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["./src/test-setup.ts"],
  },
});

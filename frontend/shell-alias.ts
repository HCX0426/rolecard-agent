/**
 * shell/ 源码别名的**唯一接线点**：`vite.config.ts` 与 `vitest.config.ts` 都 import
 * 这里，别名值不写第二遍 —— 构建与测试各挂一套解析正是 A-9 要灭的"多套逻辑"。
 * tsconfig 的 `paths` 是 JSON、import 不进 JS 常量，与本文件的对齐由
 * `scripts/consistency` 的 `check_pet_geometry_single_source` 钉住（扫不到 ≠ 通过）。
 *
 * 为什么不用 `fileURLToPath`：本仓前端 tsc 用 `types: ["vite/client"]` 且没有
 * `@types/node`（2026-11-04 实测 `import 'node:url'` = TS2307），所以用纯 ESM 内建
 * 把 file: URL 换成绝对路径：`decodeURIComponent` 先还原被转义的空格/中文，
 * `^\/(?=[A-Za-z]:)` 只剥 Windows 盘符前的斜杠（POSIX 的 `/home/...` 原样保留）。
 * 必须是绝对路径：vite 文档钉死"相对 alias 值原样使用，不会被解析成文件系统路径"。
 */
export const shellAlias = {
  '@shell/': decodeURIComponent(new URL('../shell/main/', import.meta.url).pathname).replace(
    /^\/(?=[A-Za-z]:)/,
    '',
  ),
};

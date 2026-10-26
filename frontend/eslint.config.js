// ESLint 扁平配置（P8，2026-10-26）。
//
// 为什么现在才上：前端此前只有 tsc —— 类型对、逻辑反它管不着，而 hooks 依赖数组
// 那一族错误（漏依赖 = 读到旧闭包值）是 React 事故率最高的一类，没人拦。
//
// 档位选择（首轮刻意保守，红了再逐条升级）：
// * js.configs.recommended —— 未定义变量/无用代码这类"确定的错"；
// * tseslint recommended —— 类型面 lint（no-floating-promises 那族在这里）；
// * react-hooks recommended —— 立项要的那条：hooks 依赖数组完整性；
// * react-refresh vite —— 组件文件混写非组件导出会让 Vite 热更新静默失效，
//   这条与"界面改了看不见"直接相关。
// 不用 strict/typed 全家桶：首轮噪音会把人推回 --no-ignore 心态（P4 那条 hook 纪律
// 的同款理由）；存量问题用下面的 override 登记，不装看不见。

import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    // dist 是构建产物（有意入库的那份不归 lint 管）；schema.gen.ts 是契约生成物
    // —— 手改它等于"下一次生成被覆盖"，lint 它只会教人去改生成器。
    ignores: ["dist/**", "node_modules/**", "src/api/schema.gen.ts"],
  },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      // 未用变量：`_` 前缀是"我故意留着签名位"的登记口（回调形状固定时常见）。
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      // 全角空格（U+3000）在注释与字符串里是**故意的 UI 排版手段**（R26-45 三段折行
      // 那条就是靠它控制间距）；代码位置的 irregular whitespace 照抓。
      "no-irregular-whitespace": ["error", { skipStrings: true, skipComments: true, skipTemplates: true }],
      // React Compiler 族（v7 插件新带的编译器诊断）：本仓**没装 React Compiler**
      // （vite.config.ts 无 babel 插件），这些规则的判据对象（编译器会自动重写 memo/
      // 可变性）在这里不存在 —— 报出来的全是"没有编译器会来救你"的存在性提醒，
      // 不是本次改动的错。要上编译器时整族撤掉这三行。
      "react-hooks/preserve-manual-memoization": "off",
      "react-hooks/immutability": "off",
      // **exhaustive-deps 是立项理由，必须是 error**（P8 验收：坏依赖数组 → 红）。
      // 升到 error 的当趟存量 = 0：仅有的两处真实缺口（ChatPage 漏 setStatus、
      // Live2dPet 漏 onError）在本批里修掉了 —— 零债升档，不需要过渡豁免。
      "react-hooks/exhaustive-deps": "error",
      // 同族但判据半真：`load().catch(e => setStatus(...))` 是"挂载即取数"的标准形状，
      // 规则把 effect 同步调用的函数体里的 setState 一并算成同步 setState。
      // 28 处全在"取数→落状态"的正路上；逐处论证"该不该搬进事件/用 erasableSyntaxOnly
      // 式重写"超出 P8 范围 —— 降 warn 登记，红了看得见、不拦提交（首轮保守档）。
      "react-hooks/set-state-in-effect": "warn",
    },
  },
  {
    // 测试文件放宽一档：断言里空 catch、mock 的 any 形状是测试的表达方式，
    // 不是逻辑错误；lint 的意义在 src。
    files: ["**/*.test.{ts,tsx}", "src/test/**"],
    rules: {
      "@typescript-eslint/no-explicit-any": "off",
      "no-empty": ["error", { allowEmptyCatch: true }],
    },
  },
  {
    // node 环境的非浏览器代码：数据源探针与 vitest 配置自己。
    files: ["vite.config.ts", "scripts/**/*.{ts,js,mjs}"],
    languageOptions: { globals: globals.node },
  },
);

// 再导出门面（2026-10-24 UI 打磨：图标唯一出处搬去了 `components/icons.tsx`，本文件
// 只保留 `from "./icons"` 的既有拼写 —— 与 `api/index.ts` / `deps.py` 的再导出同一家族：
// 消费方要的是这些名字，不是这个住址。新图标请加在 `components/icons.tsx`。
export * from "../icons";

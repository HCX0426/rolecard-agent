import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

// 桌宠窗（壳的第二扇窗）走同一个入口、同一份构建，只是渲染的根不同：
// 它不是控制台的第 7 个页签（没有导航、没有主题切换、也不该有背景色），
// 所以在这里分叉，而不是把三套条件判断塞进 App 里。
// PetPage 也走 lazy：控制台首屏（绝大多数启动）不必连形象窗口的代码一起下载。
const PetPage = lazy(() => import("./pages/PetPage"));
const isPet = window.location.hash.startsWith("#/pet");
if (isPet) document.documentElement.dataset.page = "pet";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Suspense fallback={null}>{isPet ? <PetPage /> : <App />}</Suspense>
  </StrictMode>,
);

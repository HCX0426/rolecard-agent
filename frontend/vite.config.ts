import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 开发态：vite 起 5173，/api 代理到 FastAPI(8000)，前后端各自热更新互不影响。
// 生产态：npm run build 产出 dist/，由 FastAPI 静态托管（api/main.py）。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
  build: {
    rollupOptions: {
      output: {
        // react/react-dom 单独成 vendor 块：它们的版本变化频率远低于业务代码，
        // 拆开后业务改动的失效面不再裹挟框架本身（浏览器缓存可用期更长）。
        manualChunks: {
          react: ["react", "react-dom"],
        },
      },
    },
  },
});

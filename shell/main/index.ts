/**
 * rolecard-agent 桌面壳（里程碑 D②，Electron）。
 *
 * 壳只做三件事：**窗口**、**本地后端的进程生命周期**、**原生能力桥**。界面仍是后端托管的
 * 那一份 `frontend/dist` —— 不为壳重写 UI，也不在壳里再存一份，否则"C/S 与 B/S 看到的是
 * 不是同一个东西"会变成下一个要修的架构问题。
 *
 * 本文件只负责装配：起后端 → 开两扇窗 → 退出时回收自己 spawn 的那个进程。
 */
import { app, ipcMain } from "electron";

import { Backend, consoleUrl, endpoint, serving, type Outcome } from "./backend";
import { createMainWindow, createPetWindow } from "./windows";

const backend = new Backend();

function report(outcome: Outcome): void {
  switch (outcome.kind) {
    case "already-serving":
      console.log(`[shell] ${endpoint().host}:${endpoint().port} 上已有后端（不是本壳起的）→ 直接连它，退出时不动它`);
      break;
    case "spawned":
      console.log(`[shell] 已拉起本地后端（pid ${outcome.pid}），退出时回收`);
      break;
    case "failed":
      // 不静默：着陆页会显示"没能启动"，但原因只有这里说得清。
      console.error(`[shell] 拉不起本地后端：${outcome.reason}`);
      break;
  }
}

void app.whenReady().then(() => {
  ipcMain.handle("shell:backend-url", () => consoleUrl());
  ipcMain.handle("shell:backend-reachable", () => serving());

  void backend.ensure().then(report);

  createMainWindow();
  // 桌宠是"能不能不看我"的开关：默认开，ROLECARD_PET=0 关掉（不为此加设置界面）。
  if (process.env.ROLECARD_PET !== "0") createPetWindow();
});

app.on("window-all-closed", () => {
  // Windows/Linux 上关完窗口就是退出（托盘与"关主窗=收起"的语义属于 D②-3）。
  app.quit();
});

app.on("before-quit", () => backend.stop());

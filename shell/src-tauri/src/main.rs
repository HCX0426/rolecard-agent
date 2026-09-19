//! rolecard-agent 桌面壳（里程碑 D②）。
//!
//! 壳只做三件事：**窗口**、**本地后端的进程生命周期**、**原生能力桥**（D②-3 再接）。
//! 界面仍是后端托管的那一份 `frontend/dist` —— 不为壳重写 UI，也不在壳里再存一份，
//! 否则"C/S 与 B/S 看到的不是同一个东西"会变成下一个要修的架构问题。
//!
//! 见 `web/index.html`：窗口先落在一张着陆页上，由它探活后端再跳进控制台，
//! 于是"后端在启动 / 起不来"是一个看得见的状态，而不是 WebView 的空白错误页。
//!
//! 本文件（`src/main.rs`）只负责装配：建壳 → 拉起后端 → 退出时回收自己 spawn 的进程。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;

use tauri::Manager;

/// 着陆页问"控制台在哪"：端口只有一处定义（`backend::endpoint`），不让页面自己再写一遍。
#[tauri::command]
fn backend_url() -> String {
    backend::console_url()
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![backend_url])
        .manage(backend::Supervisor::default())
        .setup(|app| {
            let supervisor = app.state::<backend::Supervisor>();
            match supervisor.ensure_running() {
                Ok(backend::Outcome::Spawned(pid)) => {
                    println!("[shell] 已拉起本地后端（pid {pid}），退出时会回收")
                }
                Ok(backend::Outcome::AlreadyServing) => println!(
                    "[shell] {addr} 上已有后端在服务（不是本壳起的）→ 直接连它，退出时不动它",
                    addr = backend::endpoint()
                ),
                Err(error) => eprintln!(
                    "[shell] 拉不起本地后端：{error}；界面会停在着陆页并说明原因"
                ),
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("构建桌宠壳失败")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                // 只回收自己 spawn 的那个进程树：外部实例归用户自己管（同 §11.1 的归属纪律）。
                app.state::<backend::Supervisor>().shutdown();
            }
        });
}

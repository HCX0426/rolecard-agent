//! 桌宠窗（D②-2 的极简驻留形态）：200×240、无边框、透明、置顶、不进任务栏。
//!
//! 为什么整扇窗只做"一张色片 + 一句话"：桌宠是**驻留**件，全天在桌面上，任何多余像素都是
//! 干扰；它唯一必须做到的是"角色主动找我时，我抬眼就能看到，并且能点回去"。
//!
//! 界面本身**不在这里写**：窗口先落在壳的本地着陆页（`shell/web/index.html?pet=1`），
//! 探活成功后再跳后端托管的 `#/pet`。为什么不直接指向后端：实测过 —— 后端没起来时
//! WebView2 会显示它自己的"嗯…无法访问此页面"错误页，那块不透明的白底 + 浏览器文案
//! 比桌宠本身还显眼，我们写的"连不上本地服务"根本没机会渲染。主窗本来就是两步走的，
//! 桌宠没理由例外。
//!
//! 于是"C/S 的桌宠"和"B/S 的页面"不可能长得不一样，也不需要第二份前端。
//!
//! 实测目标（这块透明窗到底靠不靠得住，只有真开一扇窗才看得出来）：
//!   1. 背景是否真透明（不是灰矩形）、DWM 是否偷偷加阴影/边框；
//!   2. 置顶与"不抢焦点"（桌宠不该抢走你正在打的字）；
//!   3. 拖拽移动是否顺、位置是否记得住。
//! 点击穿透（透明区域让位给桌面）另说：整窗穿透 = 抓不动它，只能按内容区域切换，
//! 那要等原生桥（D②-3）到位再做，不在这一版里猜。

use tauri::webview::WebviewWindowBuilder;
use tauri::{AppHandle, Manager, WebviewWindow};

pub const PET_LABEL: &str = "pet";
const PET_WIDTH: f64 = 200.0;
const PET_HEIGHT: f64 = 240.0;

/// 落点：主屏工作区右下角（离任务栏与屏幕边各留 24px）。
///
/// 为什么按 work_area 而不是屏幕尺寸：桌宠被任务栏压住半张脸是这类应用最常见的差评。
fn pet_position(app: &AppHandle) -> (f64, f64) {
    let fallback = (80.0, 80.0);
    let Ok(Some(monitor)) = app.primary_monitor() else {
        return fallback;
    };
    let scale = monitor.scale_factor();
    let area = monitor.work_area();
    let width = f64::from(area.size.width) / scale;
    let height = f64::from(area.size.height) / scale;
    let left = f64::from(area.position.x) / scale;
    let top = f64::from(area.position.y) / scale;
    (left + width - PET_WIDTH - 24.0, top + height - PET_HEIGHT - 24.0)
}

/// 打开（或复用已有的）桌宠窗。
pub fn open(app: &AppHandle) -> Result<WebviewWindow, String> {
    if let Some(existing) = app.get_webview_window(PET_LABEL) {
        // 复用已有窗口时只让它显形，不抢焦点（驻留件不该抢走你正在打的字）。
        let _ = existing.show();
        return Ok(existing);
    }
    let (x, y) = pet_position(app);
    WebviewWindowBuilder::new(app, PET_LABEL, tauri::WebviewUrl::App("index.html?pet=1".into()))
    .title("rolecard-agent · 桌宠")
    .inner_size(PET_WIDTH, PET_HEIGHT)
    .position(x, y)
    .decorations(false)
    .transparent(true)
    // 阴影必须关：Windows 的窗口阴影会在那圈透明像素上留一层灰，正是"透明窗不干净"的
    // 典型表现（而不是 WebView 画错了）。
    .shadow(false)
    .always_on_top(true)
    .skip_taskbar(true)
    .resizable(false)
    .maximizable(false)
    .minimizable(false)
    .closable(false)
    .focused(false)
    .build()
    .map_err(|error| format!("建桌宠窗失败：{error}"))
}

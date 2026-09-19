//! 本地后端进程的生命周期归属。
//!
//! 一条不可让的规矩：**只动本壳 spawn 出来的那个进程**。端口上已经有别人起的后端时，
//! 壳连它、用它，退出时**不碰它** —— 用户手动开的实例被壳杀掉，是我们赔不起的事故。
//! 同一条纪律在 §11.1（Ollama 启停）里也写着，所以先应用到自己身上。

use std::net::{SocketAddr, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

/// 默认端口与 `run_api.py` 的绑定一致（开发期固定端口是团队约定，不换端口躲冲突）。
pub const DEFAULT_PORT: u16 = 8000;

/// 壳连后端用的地址（`ROLECARD_API_PORT` 可改端口，打包形态用 `ROLECARD_BACKEND_URL`）。
pub fn endpoint() -> SocketAddr {
    let port = std::env::var("ROLECARD_API_PORT")
        .ok()
        .and_then(|raw| raw.parse::<u16>().ok())
        .unwrap_or(DEFAULT_PORT);
    SocketAddr::from(([127, 0, 0, 1], port))
}

/// 控制台地址：着陆页探活成功后跳这里。界面由后端托管，所以壳里不复制一份 dist。
pub fn console_url() -> String {
    std::env::var("ROLECARD_BACKEND_URL").unwrap_or_else(|_| format!("http://{}", endpoint()))
}

/// `ensure_running` 的结果 —— 三种情况要走三条不同的退出路径，不能糊成一个 bool。
#[derive(Debug)]
pub enum Outcome {
    /// 端口本来就有人应（外部起的实例）：连它，退出时不杀它。
    AlreadyServing,
    /// 本壳起的：记下 pid，退出时回收整棵进程树。
    Spawned(u32),
}

#[derive(Default)]
pub struct Supervisor {
    /// 只持有**自己 spawn** 的那个孩子；None = 端口上是别人的实例（或还没起）。
    child: Mutex<Option<Child>>,
}

impl Supervisor {
    /// 端口没人应就拉起后端；已有人应就直接连。不等待服务就绪 —— 着陆页自己会重试，
    /// 把"还在启动"做成界面状态而不是线程 sleep，用户在着陆页上看到的是进度而不是卡死。
    pub fn ensure_running(&self) -> Result<Outcome, String> {
        if serving() {
            return Ok(Outcome::AlreadyServing);
        }
        let (program, args) = backend_command().ok_or_else(|| {
            "找不到本地后端的启动命令：设 ROLECARD_BACKEND_CMD（整条命令）或 \
             ROLECARD_PYTHON + ROLECARD_API_SCRIPT（开发态用 .venv + run_api.py）"
                .to_string()
        })?;
        let mut command = Command::new(&program);
        command.args(&args).stdin(Stdio::null());
        hide_console(&mut command);
        let child = command
            .spawn()
            .map_err(|error| format!("启动 {program:?} {args:?} 失败：{error}"))?;
        let pid = child.id();
        *self.child.lock().expect("后端进程槽位锁被 panic 打断") = Some(child);
        Ok(Outcome::Spawned(pid))
    }

    /// 退出时回收：连子进程一起（后端在 Windows 下可能带工作进程，杀父不杀子会留下孤儿
    /// 占着端口，下一次启动就变成"端口已被占用"的谜）。
    pub fn shutdown(&self) {
        let mut slot = self.child.lock().expect("后端进程槽位锁被 panic 打断");
        let Some(child) = slot.take() else { return };
        let pid = child.id();
        #[cfg(windows)]
        {
            // `child.kill()` 只杀直接子进程；taskkill /T 才砍整棵树。
            let _ = Command::new("taskkill")
                .args(["/PID", &pid.to_string(), "/T", "/F"])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn();
        }
        #[cfg(not(windows))]
        {
            let _ = child.kill();
        }
        println!("[shell] 已回收本壳启动的后端进程（pid {pid}）");
    }
}

/// 有人在听吗。只做 TCP 层判定 —— 着陆页还要发真请求，两者语义不同，不要合并成一个函数。
fn serving() -> bool {
    TcpStream::connect_timeout(&endpoint(), Duration::from_millis(300)).is_ok()
}

/// 启动命令：显式覆盖优先，否则退回开发态的「仓库 .venv + run_api.py」。
///
/// 打包后的正确形态是把后端做成随壳分发的可执行文件（`ROLECARD_BACKEND_CMD` 指过去），
/// 那属于 D② 的打包步骤，不在这里猜路径。
fn backend_command() -> Option<(String, Vec<String>)> {
    if let Ok(raw) = std::env::var("ROLECARD_BACKEND_CMD") {
        let mut parts = raw.split_whitespace().map(str::to_string);
        let program = parts.next()?;
        return Some((program, parts.collect()));
    }
    let repo = repo_root();
    let python = std::env::var("ROLECARD_PYTHON")
        .unwrap_or_else(|_| repo.join(".venv").join("Scripts").join("python.exe").display().to_string());
    let script = std::env::var("ROLECARD_API_SCRIPT")
        .unwrap_or_else(|_| repo.join("run_api.py").display().to_string());
    if !std::path::Path::new(&python).exists() || !std::path::Path::new(&script).exists() {
        return None;
    }
    Some((python, vec![script]))
}

/// 仓库根：开发态由清单目录（`shell/src-tauri`）往上两级推。
/// 发布态这个推论不成立，届时靠 `ROLECARD_BACKEND_CMD` 给绝对路径。
fn repo_root() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(2)
        .map(std::path::Path::to_path_buf)
        .unwrap_or_else(|| std::path::PathBuf::from("."))
}

#[cfg(windows)]
fn hide_console(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn hide_console(_command: &mut Command) {}

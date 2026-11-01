// 用**真实拖拽**把桌宠拖到某条边，再量趴好之后屏内还剩多少人物像素。
//
// 为什么用拖而不是改存盘位置再重启：`restorePetDock` 不打日志，而启动后页面一有动静
// （悬停拉出 → 宽限期 → 趴回）就会按**当时的窗位**重新判边，我改的那份 `docked` 就被盖掉了
// —— 2026-11-01 白跑了一轮才看出来。拖拽是用户真正的手势，走的正是 `movePetBy` →
// `petDragEnd` → `settlePetDock` 那条链，复现得最干净。
//
// 判据仍是形状与位置（CDP 读 canvas 的 alpha 包围盒 + Win32 读窗矩形），不是"点得到"。
// 用法：node scripts/js/verify_dock_by_drag.cjs [left|right|bottom] [端口]
const { execSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const EDGE = (process.argv[2] || "left").toLowerCase();
const PORT = Number(process.argv[3] || 9222);
const PEEK = 48;
const ANCHOR_W = 200;
const ANCHOR_H = 240;
const CANVAS_X = 180;
const CANVAS_Y = 280;
if (!["left", "right", "bottom"].includes(EDGE)) {
  console.error("只支持 left / right / bottom");
  process.exit(2);
}

const WIN_PS = (me = false) => `
Add-Type -AssemblyName System.Windows.Forms
Add-Type -Namespace W3 -Name U -MemberDefinition '[DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lp); public delegate bool EnumProc(IntPtr h, IntPtr lp); [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid); [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h); [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r); public struct RECT { public int L,T,R,B; }'
$script:hits = @()
$cb = [W3.U+EnumProc] { param($h,$lp)
  $pid2 = [uint32]0
  [W3.U]::GetWindowThreadProcessId($h,[ref]$pid2) | Out-Null
  if (-not [W3.U]::IsWindowVisible($h)) { return $true }
  $p = Get-Process -Id $pid2 -ErrorAction SilentlyContinue
  if ($null -ne $p -and $p.ProcessName -eq 'rolecard-agent') {
    $r = New-Object W3.U+RECT
    [W3.U]::GetWindowRect($h,[ref]$r) | Out-Null
    if (($r.R - $r.L) -eq 560) { $script:hits += ,@($r.L,$r.T,$r.R,$r.B) }
  }
  return $true }
[W3.U]::EnumWindows($cb,[IntPtr]::Zero) | Out-Null
$wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
if ($script:hits.Count -eq 0) { 'NONE' } else { ($script:hits[0] -join ',') + '|' + $wa.X + ',' + ($wa.X + $wa.Width - 1) + ',' + ($wa.Y + $wa.Height - 1) }
`;

function readWindowRect() {
  const tmp = path.join(os.tmpdir(), `rc-dock-${process.pid}.ps1`);
  fs.writeFileSync(tmp, WIN_PS(), "utf8");
  try {
    return execSync(`powershell -NoProfile -ExecutionPolicy Bypass -File "${tmp}"`, { encoding: "utf8" })
      .toString()
      .trim()
      .split(/\r?\n/)
      .filter(Boolean)
      .pop();
  } finally {
    fs.rmSync(tmp, { force: true });
  }
}

const MEASURE = `(() => {
  const cv = document.querySelector('canvas');
  if (!cv) return JSON.stringify({ err: '没有 canvas（当前画的不是 Live2D 包，本次结论不适用）' });
  const r = cv.getBoundingClientRect();
  const c2 = document.createElement('canvas');
  c2.width = cv.width; c2.height = cv.height;
  const g = c2.getContext('2d');
  g.drawImage(cv, 0, 0);
  const d = g.getImageData(0, 0, c2.width, c2.height).data;
  let minX = c2.width, minY = c2.height, maxX = -1, maxY = -1, n = 0;
  for (let y = 0; y < c2.height; y++) for (let x = 0; x < c2.width; x++)
    if (d[(y * c2.width + x) * 4 + 3] > 10) { n++; if (x < minX) minX = x; if (x > maxX) maxX = x; if (y < minY) minY = y; if (y > maxY) maxY = y; }
  // bbox 五元组 [minX, maxX, minY, maxY, n]：纵向那条判据要用 y，别再只回 x
  // （少了 y 时调用方拿到 undefined ⇒ 比较全是 NaN ⇒ 结论看着像"通过"其实是空的）。
  return JSON.stringify({ canvasCss: [Math.round(r.left), Math.round(r.top)], bbox: n === 0 ? null : [minX, maxX, minY, maxY, n] });
})()`;

/**
 * 把色片"拖"到那条边上去。
 *
 * **必须用 CDP 的 Input.dispatchMouseEvent（受信任输入），不能在页面里 new PointerEvent**：
 * `PetPage` 的 pointerdown 会调 `setPointerCapture(pointerId)`，合成事件的 pointerId 没有
 * 对应的活动指针 ⇒ 当场抛 NotFoundError，拖拽根本不会登记（2026-11-01 先试过合成那条，
 * 窗一格没动还以为是自己算错了）。真输入走的就是用户那条路：screenX/screenY 由浏览器算，
 * capture 正常，`pet-drag` → `petDragEnd` → `settlePetDock` 全链都跑到。
 *
 * 坐标用页面坐标（视口内），screenX 由浏览器按窗位加出来；往目标方向给**超过屏宽**的量，
 * 窗被壳夹进工作区 ⇒ 正好停在边上。
 */
async function dragTo(ws, send, dxTotal, dyTotal = 0) {
  const pt = await send("Runtime.evaluate", {
    expression: `(() => { const r = document.querySelector('canvas').getBoundingClientRect();
      return JSON.stringify([Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2)]); })()`,
    returnByValue: true,
  });
  const [x0, y0] = JSON.parse(pt.result.result.value);
  // 落点必须留在**视口内**（560×520）：跑出这个范围 CDP 就不送事件了。
  // 位移是靠**反馈**攒出来的：screenY = clientY + 窗偏移，窗每动一点，同一个 clientY 的
  // screenY 也跟着变，于是 `movePetBy` 每一步都收到同样的增量 —— 所以 clamp 之后并不停，
  // 而是按最后那个步长继续走。步数因此直接决定能走多远：20 步只够横穿半屏，纵向那趟
  // 从中间到工作区下沿差 ~186px，20 步勉强到、测不出结论 —— 取 60 步留足余量。
  const clampX = (v) => Math.max(1, Math.min(v, 559));
  const clampY = (v) => Math.max(1, Math.min(v, 519));
  const steps = 60;
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x: x0, y: y0, button: "left", clickCount: 1, buttons: 1 });
  for (let i = 1; i <= steps; i++) {
    await send("Input.dispatchMouseEvent", {
      type: "mouseMoved",
      x: clampX(x0 + (dxTotal * i) / steps),
      y: clampY(y0 + (dyTotal * i) / steps),
      button: "left",
      buttons: 1,
    });
  }
  await send("Input.dispatchMouseEvent", {
    type: "mouseReleased",
    x: clampX(x0 + dxTotal),
    y: clampY(y0 + dyTotal),
    button: "left",
    buttons: 0,
  });
  return `dragged dx=${dxTotal} dy=${dyTotal} from (${x0},${y0})`;
}

async function main() {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const pet = list.find((t) => t.type === "page" && /#\/pet/.test(t.url));
  if (!pet) throw new Error(`没有 #/pet 目标：${list.map((t) => t.url).join(", ")}`);
  const ws = new WebSocket(pet.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("ws 打不开")); });
  let id = 0;
  const pending = new Map();
  ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
  const send = (method, params) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
  const evl = async (expression) => {
    const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 300));
    return r.result?.result?.value;
  };
  await send("Runtime.enable", {});

  const before = readWindowRect();
  if (before === "NONE") throw new Error("没找到 560 宽的桌宠窗");
  console.log(`拖之前窗位：${before.split("|")[0]}`);

  // 往目标边的方向给超大位移：窗被壳夹进工作区，于是正好停在边上。
  // 吸 right ⇒ 往右推；吸 left ⇒ 往左推；bottom ⇒ 往下推（这一条测的是"该**不**吸"）。
  const dx = EDGE === "right" ? 4000 : EDGE === "left" ? -4000 : 0;
  const dy = EDGE === "bottom" ? 4000 : 0;
  console.log(await dragTo(ws, send, dx, dy));
  await new Promise((r) => setTimeout(r, 2600)); // 等滑窗 280ms + 判边 + 趴回

  const P = JSON.parse(await evl(MEASURE));
  if (P.err) { console.log("!! " + P.err); process.exit(1); }
  const line = readWindowRect();
  const [wl, wt] = line.split("|")[0].split(",").map(Number);
  const [workL, workR, workB] = line.split("|")[1].split(",").map(Number);
  if (!P.bbox) { console.log("=> 画布零非透明像素：模型没画出来"); process.exit(1); }
  const [bx0, bx1, by0, by1, n] = P.bbox;
  const contentW = bx1 - bx0 + 1;
  const contentBottom = wt + P.canvasCss[1] + by1;

  // 纵向那条（bottom）判的不是"露多少"，而是**根本没推**：往下推必然切脚（用户 2026-11-01
  // 「底下和上都不需要隐藏」）。判据 = 锚点下沿不超过工作区下沿，且人物的脚在屏内。
  if (EDGE === "bottom") {
    const anchorBottom = wt + CANVAS_Y + ANCHOR_H - 1;
    const push = anchorBottom - workB;
    const headTop = wt + P.canvasCss[1] + by0;
    console.log(`\n拖到下边缘：窗 T=${wt} 锚点下沿=${anchorBottom} 工作区下沿=${workB}`);
    console.log(`纵向推出 = ${push}px（该 ≤ 0 ⇒ 根本没吸、没推）`);
    console.log(`人物在屏幕 y[${headTop}..${contentBottom}]（头在上的小值、脚在下的大值），屏内判据 ≤ ${workB}`);
    if (push <= 0 && contentBottom <= workB) {
      console.log("✅ 下边缘不藏：整只（含脚）都在屏内");
      process.exit(0);
    }
    console.log(`❌ 下边缘仍在藏脚（推出 ${push}px，脚在 ${contentBottom}）`);
    process.exit(1);
  }

  const anchorL = wl + CANVAS_X;
  const tuck = EDGE === "left" ? workL - anchorL : anchorL + ANCHOR_W - 1 - workR;
  const sL = wl + P.canvasCss[0] + bx0;
  const sR = wl + P.canvasCss[0] + bx1;
  const visLo = EDGE === "left" ? workL : 0;
  const seen = Math.max(0, Math.min(sR, workR) - Math.max(sL, visLo) + 1);
  const want = Math.min(PEEK, contentW);

  console.log(`\n吸【${EDGE}】窗位 L=${wl}（${line.split("|")[0]}）锚点 L=${anchorL}`);
  console.log(`壳实际推出 ${tuck}px`);
  console.log(`人物 bbox（画布局部）x[${bx0}..${bx1}] y[${by0}..${by1}] 宽=${contentW} n=${n}`);
  console.log(`人物落在屏幕 x[${sL}..${sR}]`);
  console.log(`屏内可见内容宽 = ${seen}px（应 ${want}px）`);
  if (seen >= want) {
    console.log(`✅ 趴${EDGE === "left" ? "左" : "右"}边缘看得见：露出的 ${seen}px 全是人物本体`);
    process.exit(0);
  }
  console.log(`❌ 趴${EDGE === "left" ? "左" : "右"}边缘看不清：只剩 ${seen}px（人物宽 ${contentW}）`);
  process.exit(1);
}

main().catch((e) => { console.error("失败：" + e.message); process.exit(1); });

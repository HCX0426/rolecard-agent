// 真机复验：桌宠趴边时，屏内那一条里到底有没有**人物本体**。
//
// 为什么必须单独有这么个脚本（而不是靠截图）：那扇窗是**透明置顶的 GPU 窗**，两种"看图"
// 的办法都会骗人，而且这次排查里各骗过一轮：
//   ① `CopyFromScreen` 屏摄拍的是**合成后**的画面 —— 那扇窗可以被别的窗整个盖住
//      （实际就被盖住过，裁出来一片纯白 ⇒ 差点结论"什么都没画"）；
//   ② `PrintWindow`（含 PW_RENDERFULLCONTENT）对透明 GPU 窗只回残缺一块
//      （160×184 里只抓到 53×18 ⇒ 会把"抓取失败"误读成"内容错位"）。
// 所以这里问两个**真值来源**并把它们合起来算：
//   · CDP 问渲染进程：canvas 上非透明像素的真实包围盒（形状与位置，不是"点得到"）；
//   · Win32 问那扇窗：它在屏幕上的矩形（= 壳实际摆出来的位置）。
//
// 判据是用户定的那条：**看形状和位置**。"能 hover 到""能点到"都不算数 ——
// 透明区在 DOM 里照样命中，曾经就是这么把"看不见"误判成"没问题"的。
//
// 用法：
//   ① 先去掉 ELECTRON_RUN_AS_NODE（否则 Electron 被当纯 node 跑、秒退且零日志），
//      再用调试端口起壳：
//        Remove-Item Env:ELECTRON_RUN_AS_NODE
//        Start-Process <安装目录>\rolecard-agent.exe -ArgumentList '--remote-debugging-port=9222'
//   ② node scripts/js/verify_pet_dock.cjs [端口]
//   想验某一条边：把桌宠拖到那条边上松手（松手那一刻壳自己判边并吸进去）。
const { execSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

function readWindowRect() {
  const tmp = path.join(os.tmpdir(), `rc-pet-win-${process.pid}.ps1`);
  fs.writeFileSync(tmp, WIN_PS, "utf8");
  try {
    return execSync(`powershell -NoProfile -ExecutionPolicy Bypass -File "${tmp}"`, {
      encoding: "utf8",
    })
      .toString()
      .trim()
      .split(/\r?\n/)
      .filter(Boolean)
      .pop();
  } finally {
    fs.rmSync(tmp, { force: true });
  }
}

const PEEK = 48;
const ANCHOR_W = 200;
const ANCHOR_H = 240;
const CANVAS_X = 180; // 锚点左上角在画布里的偏移
const CANVAS_Y = 280;

const PAGE_EXPR = `(() => {
  const cv = document.querySelector('canvas');
  if (!cv) return JSON.stringify({ err: '画面上没有 canvas（当前画的可能是序列帧包或 SVG 兜底 —— 那本身就是结论）' });
  const r = cv.getBoundingClientRect();
  const c2 = document.createElement('canvas');
  c2.width = cv.width; c2.height = cv.height;
  const g = c2.getContext('2d');
  g.drawImage(cv, 0, 0);
  const d = g.getImageData(0, 0, c2.width, c2.height).data;
  let minX = c2.width, minY = c2.height, maxX = -1, maxY = -1, n = 0;
  for (let y = 0; y < c2.height; y++) {
    for (let x = 0; x < c2.width; x++) {
      if (d[(y * c2.width + x) * 4 + 3] > 10) {
        n++;
        if (x < minX) minX = x; if (x > maxX) maxX = x;
        if (y < minY) minY = y; if (y > maxY) maxY = y;
      }
    }
  }
  const sp = document.querySelector('canvas').closest('.pet-nodrag').getBoundingClientRect();
  return JSON.stringify({
    buf: [c2.width, c2.height],
    canvasCss: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
    spriteBoxCss: [Math.round(sp.left), Math.round(sp.top), Math.round(sp.width), Math.round(sp.height)],
    bbox: n === 0 ? null : [minX, minY, maxX, maxY, n],
    failed: !!document.querySelector('[data-testid="pet-live2d-failed"]'),
  });
})()`;

// 那扇窗的矩形用 Win32 量。**写成临时 .ps1 再 -File 执行**：把多行脚本塞进
// `powershell -Command "…"` 会把换行变成字面量 \n，第一条 cmdlet 就解析不出来
// （2026-11-01 这么踩过一次，报的是「'\nAdd-Type' 不是 cmdlet」，很容易误读成 API 不通）。
const WIN_PS = `
Add-Type -AssemblyName System.Windows.Forms
Add-Type -Namespace W2 -Name U -MemberDefinition '[DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lp); public delegate bool EnumProc(IntPtr h, IntPtr lp); [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid); [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h); [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r); public struct RECT { public int L,T,R,B; }'
$script:hits = @()
$cb = [W2.U+EnumProc] {
  param($h,$lp)
  $pid2 = [uint32]0
  [W2.U]::GetWindowThreadProcessId($h,[ref]$pid2) | Out-Null
  if (-not [W2.U]::IsWindowVisible($h)) { return $true }
  $p = Get-Process -Id $pid2 -ErrorAction SilentlyContinue
  if ($null -ne $p -and $p.ProcessName -eq 'rolecard-agent') {
    $r = New-Object W2.U+RECT
    [W2.U]::GetWindowRect($h,[ref]$r) | Out-Null
    if (($r.R - $r.L) -eq 560) { $script:hits += ,@($r.L,$r.T,$r.R,$r.B) }
  }
  return $true
}
[W2.U]::EnumWindows($cb,[IntPtr]::Zero) | Out-Null
$wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
if ($script:hits.Count -eq 0) { 'NONE' } else { ($script:hits[0] -join ',') + '|' + $wa.X + ',' + ($wa.X + $wa.Width - 1) + ',' + ($wa.Y + $wa.Height - 1) }
`;

async function main() {
  const port = Number(process.argv[2] || 9222);
  const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const pet = list.find((t) => t.type === "page" && /#\/pet/.test(t.url));
  if (!pet) throw new Error(`没有 #/pet 目标，只有：${list.map((t) => t.url).join(", ")}`);

  const ws = new WebSocket(pet.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.onopen = res;
    ws.onerror = () => rej(new Error("CDP ws 打不开"));
  });
  let id = 0;
  const pending = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      pending.get(m.id)(m);
      pending.delete(m.id);
    }
  };
  const send = (method, params) =>
    new Promise((res) => {
      const i = ++id;
      pending.set(i, res);
      ws.send(JSON.stringify({ id: i, method, params }));
    });
  await send("Runtime.enable", {});
  const r = await send("Runtime.evaluate", { expression: PAGE_EXPR, returnByValue: true });
  ws.close();

  if (r.result?.exceptionDetails) {
    throw new Error("页面里跑炸了：" + JSON.stringify(r.result.exceptionDetails).slice(0, 300));
  }
  const P = JSON.parse(r.result.result.value);
  if (P.err) {
    console.log("!! " + P.err);
    process.exit(1);
  }

  const line = readWindowRect();
  if (!line || line === "NONE") {
    console.log("!! 没找到 560 宽的桌宠画布窗（壳没起？或它此刻不显示）");
    process.exit(1);
  }
  const [wl, wt, wr, wb] = line.split("|")[0].split(",").map(Number);
  // 工作区三个数都从探针带（左沿、右沿、下沿）—— 多屏时主屏工作区左沿**不一定**是 0，
  // 硬编 0 会让"吸左边该推多少"在副屏上算错。
  const [workL, workR, workB] = line.split("|")[1].split(",").map(Number);

  console.log(`窗   560x${wb - wt} @ L=${wl} T=${wt}  workArea 右=${workR} 下=${workB}`);
  console.log(`canvas css=${P.canvasCss.join(",")}  色片盒 css=${P.spriteBoxCss.join(",")}  缓冲=${P.buf.join("x")}`);
  console.log(`live2dFailed=${P.failed}`);
  if (!P.bbox) {
    console.log("=> 画布上**零非透明像素**：模型没画出来（看 live2dFailed，或它落到了默认序列帧包）");
    process.exit(1);
  }
  const [bx0, by0, bx1, by1, n] = P.bbox;
  const contentW = bx1 - bx0 + 1;
  const contentH = by1 - by0 + 1;
  console.log(`alpha bbox（画布局部）= x[${bx0}..${bx1}] y[${by0}..${by1}]  ${contentW}x${contentH}  n=${n}`);

  // 锚点框在屏幕上的位置，与"窗被推出去多少"（从实际窗位反推，不看代码里的常数）。
  // ⚠ 方向是**推出去为正**：吸右边 ⇒ 锚点右沿越过 workR；吸下边 ⇒ 锚点下沿越过 workB；
  // 吸左边 ⇒ 锚点左沿跑到 workL 左边（负值）。第一版把符号写反了，于是"趴右边缘"
  // 被认成"bottom"、走了错的分支还照样打印 ✅ —— 判据脚本蒙对结论比不判更糟。
  const anchorL = wl + CANVAS_X;
  const anchorT = wt + CANVAS_Y;
  const pushRight = anchorL + ANCHOR_W - 1 - workR; // >0 ⇒ 吸右边
  const pushBottom = anchorT + ANCHOR_H - 1 - workB; // >0 ⇒ 吸下边
  const pushLeft = workL - anchorL; // >0 ⇒ 吸左边
  const cand = [
    ["right", pushRight],
    ["left", pushLeft],
    ["bottom", pushBottom],
  ].filter(([, v]) => v > 20);
  const [edge, tuck] = cand.length
    ? cand.reduce((a, b) => (b[1] > a[1] ? b : a))
    : ["没吸边", 0];

  console.log(`\n判边：锚点 L=${anchorL} T=${anchorT} ⇒ 现在吸在【${edge}】，壳实际推出 ${tuck}px（旧常数 132/140）`);

  // 人物落在屏幕上的绝对区间
  const sL = wl + P.canvasCss[0] + bx0;
  const sR = wl + P.canvasCss[0] + bx1;
  const sT = wt + P.canvasCss[1] + by0;
  const sB = wt + P.canvasCss[1] + by1;
  console.log(`人物落在屏幕：x[${sL}..${sR}]  y[${sT}..${sB}]`);

  if (edge === "right") {
    const seen = Math.max(0, Math.min(sR, workR) - sL + 1);
    const want = Math.min(PEEK, contentW);
    console.log(`屏内（x ≤ ${workR}）可见内容宽 = ${seen}px，应 ${want}px`);
    console.log(seen >= want ? `✅ 趴右边缘看得见：露出的那 ${seen}px 全是人物本体` : `❌ 趴右边缘看不清（只剩 ${seen}px）`);
    process.exit(seen >= want ? 0 : 1);
  }
  if (edge === "bottom") {
    const seen = Math.max(0, Math.min(sB, workB) - sT + 1);
    const want = Math.min(PEEK, contentH);
    console.log(`屏内（y ≤ ${workB}）可见内容高 = ${seen}px，应 ${want}px`);
    console.log(seen >= want ? `✅ 趴下边缘看得见：露出的那 ${seen}px 全是人物本体` : `❌ 趴下边缘看不清（只剩 ${seen}px）`);
    process.exit(seen >= want ? 0 : 1);
  }
  if (edge === "left") {
    // 吸左边：窗往左推 T，屏内保留的是锚点的**右段**，也就是人物的右段。
    const seen = Math.max(0, sR - Math.max(sL, 0) + 1);
    const want = Math.min(PEEK, contentW);
    console.log(`屏内（x ≥ 0）可见内容宽 = ${seen}px，应 ${want}px`);
    console.log(
      seen >= want
        ? `✅ 趴左边缘看得见：露出的那 ${seen}px 全是人物本体`
        : `❌ 趴左边缘看不清（只剩 ${seen}px）—— 这就是"同一个 bug 的对称版本"`,
    );
    process.exit(seen >= want ? 0 : 1);
  }
  console.log("现在没吸边 ⇒ 只验摆位：内容应当**水平居中**（不趴边时不该偏左）。");
  const centerOff = Math.round((bx0 + bx1) / 2) - Math.round(P.buf[0] / 2);
  console.log(`内容中心离画布中心 = ${centerOff}px（居中判据：|off| ≤ 3）`);
  process.exit(Math.abs(centerOff) <= 3 ? 0 : 1);
}

main().catch((e) => {
  console.error("复验失败：" + e.message);
  process.exit(1);
});

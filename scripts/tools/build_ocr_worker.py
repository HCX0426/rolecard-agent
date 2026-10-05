"""打随包 OCR worker：PyInstaller onedir，产物落在 `build/ocrworker/ocr-worker/`。

**必须用独立 OCR venv 的解释器跑**（那份环境里才有 rapidocr / cv2 / onnxruntime）：

    .venv-ocr\\Scripts\\python.exe -m pip install -r requirements-package-ocr.txt   # 只装打包器
    .venv-ocr\\Scripts\\python.exe scripts\\build_ocr_worker.py

打完**自己冒烟一次**：造一张写着 `HELLO-OCR-2026` 的图，直接跑产物 exe，
断言 stdout 里认得出这串 —— 这一步过了才谈得上"装机版有本地 OCR"。
不这么做的后果是这条链上最贵的一次教训：`collect_submodules()` 对没安装的包返回空列表
且**不报错**，于是能"构建成功"地打出一个跑不起来的包（后端 spec 的 `RUNTIME_PACKAGES`
那道闸就是为此而设，这里再加一发实弹）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys

# Windows 控制台默认 GBK，而这份输出里有 ✅ 与中文 —— 不重配编码，脚本会在**打印时**崩
# （门禁 `console encoding` 那条尺子第一次运行就照到了这里）。与 make_app_icon.py 同一处理。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = ROOT / "packaging" / "ocr-worker.spec"
OUT = ROOT / "build" / "ocrworker"
WORK = ROOT / "build" / "ocrworker-work"
BUNDLE = OUT / "ocr-worker"
EXE = BUNDLE / "ocr-worker.exe"
SMOKE_PNG = ROOT / "build" / "ocr-smoke.png"
#: 冒烟图上的字。**只用 ASCII 与数字**：Windows 自带字体里 arial 没有中文，
#: 造一张"根本没印上字"的图去测 OCR，测到的是自己的夹具。
SMOKE_TEXT = "HELLO-OCR-2026"


def _size_mb(path: pathlib.Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1024 / 1024


def _make_smoke_image() -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (760, 180), "white")
    draw = ImageDraw.Draw(img)
    font = None
    for cand in ("arialbd.ttf", "arial.ttf", "segoeui.ttf"):
        p = pathlib.Path("C:/Windows/Fonts") / cand
        if p.exists():
            font = ImageFont.truetype(str(p), 84)
            break
    if font is None:  # 一个字体都没找到：用默认位图字体，字小但 OCR 仍认（并说清发生了什么）
        font = ImageFont.load_default()
        print("（没找到 Windows 自带字体，冒烟图用 PIL 默认位图字体 —— 字会很小）", flush=True)
    draw.text((24, 40), SMOKE_TEXT, fill="black", font=font)
    SMOKE_PNG.parent.mkdir(parents=True, exist_ok=True)
    img.save(SMOKE_PNG)


def _smoke() -> int:
    """跑一次产物。认得出那串字才算成，认不出就是包坏了。"""
    if not EXE.exists():
        print(f"产物不在：{EXE}", file=sys.stderr)
        return 1
    _make_smoke_image()
    done = subprocess.run(  # noqa: S603
        [str(EXE), str(SMOKE_PNG)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False, timeout=180,
    )
    out = (done.stdout or "").replace(" ", "").replace("\n", "")
    if done.returncode != 0:
        print(f"❌ worker 退出码 {done.returncode}：{(done.stderr or '')[-400:]}", file=sys.stderr)
        return 1
    if SMOKE_TEXT not in out:
        print(
            f"❌ 冒烟没认出 {SMOKE_TEXT}：stdout={out[:200]!r}",
            f"stderr尾={(done.stderr or '')[-200:]}",
            file=sys.stderr,
            sep=" ",
        )
        return 1
    print(f"✅ 冒烟通过：产物认出了 {SMOKE_TEXT}（stdout 前 80 字：{out[:80]}）")
    return 0


def main() -> int:
    if "--smoke-only" in sys.argv:
        return _smoke()
    missing = [p for p in ("rapidocr", "cv2", "onnxruntime") if importlib.util.find_spec(p) is None]
    if missing:
        print(
            "当前解释器没有 OCR 运行栈：" + ", ".join(missing)
            + f"\n  请用 .venv-ocr\\Scripts\\python.exe 跑本脚本（现在：{sys.executable}）",
            file=sys.stderr,
        )
        return 2
    if importlib.util.find_spec("PyInstaller") is None:
        print(
            "这个 venv 里没有 PyInstaller：\n"
            "  .venv-ocr\\Scripts\\python.exe -m pip install -r requirements-package-ocr.txt",
            file=sys.stderr,
        )
        return 2
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", str(OUT), "--workpath", str(WORK), str(SPEC),
    ]
    print(" ".join(cmd), flush=True)
    result = subprocess.run(cmd, check=False)  # noqa: S603
    if result.returncode != 0 or not EXE.exists():
        print(f"PyInstaller 失败（exit={result.returncode}），见上面的日志", file=sys.stderr)
        return result.returncode or 1
    print(f"\n✅ 随包 OCR worker：{BUNDLE}（{_size_mb(BUNDLE):.0f} MB）", flush=True)
    code = _smoke()
    if code == 0:
        print(
            "   下一步：electron-builder 会把它收进 resources/ocr-worker"
            "（见 shell/electron-builder.yml）"
        )
    else:
        shutil.rmtree(BUNDLE, ignore_errors=True)
        print("❌ 冒烟没过，已删掉这份产物 —— 别让它进安装包", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

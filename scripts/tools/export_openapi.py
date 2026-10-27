"""Export the FastAPI OpenAPI schema for frontend type generation (P1).

用法：
    python scripts/tools/export_openapi.py            # 刷新入库副本 frontend/openapi.json
    python scripts/tools/export_openapi.py --check    # 现导出对账入库副本（漂移=红）

为什么要入库一份（frontend/openapi.json）：前端类型生成物 `schema.gen.ts` 与它同一次提交
走 —— 漂移检查两端各有一份事实，判据只认"入库的 json == 现导出的 json"。build/ 在
gitignore 里，判据不读 gitignore 的东西（第六条规矩）。

为什么不用运行中的服务拉 /openapi.json：那要先起服务、再处理端口与鉴权；`create_app()`
不连网（模型构造惰性），直接拿 app.openapi() 是同一份事实、少一层进程编排。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile

# 中文与「❌」在管道输出上会撞 GBK（`console encoding` 尺子盯这个；同 scan_secrets.py 的形状）
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

REPO_COPY = ROOT / "frontend" / "openapi.json"


def export_schema() -> dict:
    from rolecard_agent.api.main import create_app

    app = create_app(sqlite_path=pathlib.Path(tempfile.mkdtemp()) / "openapi-export.db")
    return app.openapi()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只比对，不写盘")
    args = parser.parse_args()

    fresh = export_schema()
    fresh_text = json.dumps(fresh, ensure_ascii=False, indent=1, sort_keys=True) + "\n"

    if args.check:
        if not REPO_COPY.exists():
            print(f"❌ {REPO_COPY} 不存在 —— 先跑一次不带 --check 的导出并提交")
            return 1
        committed = REPO_COPY.read_text(encoding="utf-8")
        if committed != fresh_text:
            print("❌ OpenAPI 契约漂移：入库的 frontend/openapi.json 与现导出不一致。")
            print("   修法：python scripts/tools/export_openapi.py 然后重新生成前端类型")
            print("   （cd frontend && npm run api:types），两者与代码改动同一次提交。")
            return 1
        print(f"OK OpenAPI 契约与代码同步（{len(fresh.get('paths', {}))} 路径）")
        return 0

    REPO_COPY.write_text(fresh_text, encoding="utf-8", newline="\n")
    print(f"written {REPO_COPY} ({len(fresh.get('paths', {}))} 路径)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

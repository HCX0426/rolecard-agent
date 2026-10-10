"""前后端接线的静态护栏。

为什么需要：`frontend/src/api/` 的 `request()` 曾经只设 `Content-Type` 却没有
`JSON.stringify(body)` —— 于是 fetch 把对象 body 退化成 `"[object Object]"`，
**页面 GET 全正常、所有写操作静默 422**。pytest 与 smoke_check 都直接打 API，
对"前端接线"这一类故障完全看不见（README 截图也看不出来，因为截图都是只读视图）。

这条断言把该故障钉死：谁把序列化删掉，测试立刻红。

住址：`api.ts` 现为 `api/index.ts`（快照 P3-1 第三刀的启用步）。67 处消费者写的是
`./api`/`../api`，靠 bundler 的目录解析接上，调用点没动 —— 所以**这个路径常量是仓库里
唯一一处按全路径钉着前端出口的地方**，搬家时只有它会脱靶。脱靶的形状已实测：
`read_text()` 抛 `FileNotFoundError` ⇒ 大声红（不是静默绿），但仍然是"护栏不再看东西"，
所以它必须跟着改，不能靠"反正会红"混过去。
"""

from __future__ import annotations

import re
from pathlib import Path

API_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "api" / "index.ts"
# `ChatEvent` 联合随 P3-1 第三刀搬进 `api/sse.ts`（SSE 那一族的住址）。**按声明处读，
# 不按再导出处读**：index 里那行 `export type { ChatEvent } from "./sse"` 只是个转发面，
# 拿它当源头会让这条差分读到接口面而不是实现面 —— 哪天有人改了转发的目标，词表就没人查了。
API_SSE_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "api" / "sse.ts"
STREAM_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "stream.ts"


def test_sse_event_vocabulary_matches_the_parser() -> None:
    """内核声明的轮次事件名 == 前端 `ChatEvent` 联合里声明的那些（一个不多一个不少）。

    为什么单独钉这条（架构审计报告 §7 / D 的前置）：事件名是**跨语言的线协议**，改一边不会
    让另一边编译失败，症状只是"那一类东西再也不显示了"——比如 guard 改写后不替换气泡，
    用户看到半截违规文本还以为是模型的问题。`core/agent/turn.py` 的 `EVENT_TYPES` 与
    `frontend/src/api/sse.ts` 的 `ChatEvent` 各自是唯一声明处，这里做双向差分。
    """
    from rolecard_agent.core.agent.turn import EVENT_TYPES

    api_src = API_SSE_TS.read_text(encoding="utf-8")
    block = re.search(r"export type ChatEvent =(.+?\};)", api_src, flags=re.S)
    assert block, "frontend/src/api/sse.ts 里找不到 ChatEvent 联合 —— 词表源头挪位置了？"
    declared_frontend = set(re.findall(r'type:\s*"([a-z_]+)"', block.group(1)))
    assert set(EVENT_TYPES) == declared_frontend, (
        f"内核发了前端没声明的：{sorted(set(EVENT_TYPES) - declared_frontend)}；"
        f"前端声明了内核不发的：{sorted(declared_frontend - set(EVENT_TYPES))}"
    )
    # 解析方（stream.ts 的 switch）必须处理除"收尾类"之外的每一个事件。
    stream_src = STREAM_TS.read_text(encoding="utf-8")
    handled = set(re.findall(r'case "([a-z_]+)"', stream_src))
    no_render = {"end"}  # end 只由调用方结束"生成中"态，没有要渲染的载荷
    assert declared_frontend - no_render <= handled, (
        f"前端解析器漏了这些事件：{sorted(declared_frontend - no_render - handled)}"
    )


def test_complete_vocabulary_prose_lists_are_not_stale() -> None:
    """任何一条「首尾=start…end」的斜杠链 = 它在宣称自己就是**整份**轮次词表 ⇒ 必须真是全部。

    为什么单独一条（ENGI-36 B 加 `answered_by` 的现场）：上面那条差分只比对
    `EVENT_TYPES` ↔ 前端 `ChatEvent`/`stream.ts` 的 case，**看不见**散在 `api/chat.py` docstring
    与 `docs/架构总览.md` 里那些人写的"完整列举" —— 加一个事件它们不会红，只是静默少列一个，
    下一位照文档接线的人就以为词表长那样。这正是本仓一路在治的「同一事实抄多处、改一处漏两处」，
    而**散文提醒（"别忘了改这里"）本身就是漂移的成因**，所以我把它升级成这条尺子。

    判据只用「首尾=start…end」这一个形状把"宣称是全集"的链与"只列了其中几类"分开：
    后者（如 `stream.ts` 归约讲解里的 `token…context_trimmed`）不该被强求齐全，也不被本条误伤。
    实测全仓活文件命中的正是那两处全集 —— 分母非 0 由下面那条 `found >= 2` 兜住
    （"一条都没扫到"与"扫到了都齐"长得一模一样，是台账里那族恒绿尺子的病）。
    """
    from rolecard_agent.core.agent.turn import EVENT_TYPES

    root = Path(__file__).resolve().parents[2]
    vocab = set(EVENT_TYPES)
    # CamelCase → snake_case：`ToolCall` 与 `tool_call` 认成同一个词（两份列举各用一种写法）。
    chain = re.compile(
        r"(?<![A-Za-z0-9_/])"
        r"([A-Za-z_][A-Za-z0-9_]*(?:\s*/\s*[A-Za-z_][A-Za-z0-9_]*){2,})"
        r"(?![A-Za-z0-9_/])"
    )

    def snake(tok: str) -> str:
        return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", tok).lower()

    files = [
        p
        for p in [
            *sorted((root / "src").rglob("*")),
            *sorted((root / "frontend" / "src").rglob("*")),
            *sorted((root / "docs").glob("*.md")),
        ]
        if p.is_file()
        and p.suffix in {".py", ".ts", ".tsx", ".md"}
        and ".test." not in p.name
    ]
    offenders: list[str] = []
    found = 0
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in chain.finditer(text):
            toks = [snake(t.strip()) for t in m.group(1).split("/")]
            if toks[0] != "start" or toks[-1] != "end":
                continue  # 首尾不齐 ⇒ 它没宣称自己是全集
            if sum(t in vocab for t in toks) < 3:
                continue  # 不足三个真事件名 ⇒ 无关三词链（如 start / stop / end）
            found += 1
            if set(toks) != vocab:
                line = text[: m.start()].count("\n") + 1
                rel = path.relative_to(root).as_posix()
                offenders.append(f"{rel}:{line} 缺={sorted(vocab - set(toks))}")
    assert found >= 2, (
        f"只扫到 {found} 处「整份词表」的列举 ⇒ 扫描口径漂了（这两处正是本条要看住的东西）"
    )
    assert not offenders, "整份轮次词表的人写列举过期了（加事件时漏补）：" + "; ".join(offenders)


def test_json_request_bodies_are_serialised() -> None:
    """JSON 请求体必须显式序列化 —— 否则写操作（启停插件 / 改角色 / 切模型）全部 422。"""
    src = API_TS.read_text(encoding="utf-8")
    assert "JSON.stringify(body)" in src, (
        "frontend/src/api/index.ts 的 request() 缺少 JSON.stringify(body)：fetch 不会自动序列化"
        "对象，body 会变成 '[object Object]'，服务端 JSON 解析失败 → 所有写操作 422。"
    )


def test_formdata_is_not_json_stringified() -> None:
    """FormData 必须走 multipart 原样提交 —— 序列化它会破坏文件上传的边界。"""
    src = API_TS.read_text(encoding="utf-8")
    assert "body instanceof FormData" in src, "FormData 分支丢失：multipart 上传会被当成 JSON"


def test_every_frontend_endpoint_literal_resolves_to_a_backend_route() -> None:
    """前端源码里的每一个 `/api/…` 字面量都必须指得到后端真路由（`R102-50`/`R102-12`）。

    这把尺子收的是"改一个端点名要手找 82+31 处"的那笔形状债的**可达性半边**：从前改一条
    router 路径，前端指不到它只有人眼能发现（界面静默显示"—"）。双向差分的另一半
    （后端有哪些路由前端没引用）只计数不拦——脚本与探针合法引用着一批前端不用的路由。
    变异：把任一前端字面量的路径改成不存在的 ⇒ 本条红。
    """
    import sys
    from collections.abc import Iterator

    frontend_src = Path(__file__).resolve().parents[2] / "frontend" / "src"

    def frontend_literals() -> Iterator[tuple[str, int, str]]:
        # 口径沿开轮批 R102-12：引号/反引号起的 `/api/…` 字面量；剔 *.test.*（mock 自足）。
        # 块注释 /** … */ 整段剔除：api 层的讲解里拿 Ollama `/api/show` 当证据（`R102-50`
        # 的取证现场），那是知识不是接线。
        pat = re.compile(r"[\"'`](/api/[A-Za-z0-9_{}$./-]*)[\"'`]")
        block_comment = re.compile(r"/\*.*?\*/", flags=re.S)
        for path in sorted(frontend_src.rglob("*")):
            if path.suffix not in {".ts", ".tsx"} or ".test." in path.name:
                continue
            cleaned = block_comment.sub(
                "", path.read_text(encoding="utf-8", errors="ignore")
            )
            for lineno, line in enumerate(cleaned.splitlines(), 1):
                if line.lstrip().startswith("//"):
                    continue
                for hit in pat.findall(line):
                    yield str(path.relative_to(frontend_src)), lineno, hit

    def normalize(literal: str) -> str:
        # 前端模板串 `${threadId}` → 后端 `{thread_id}` 形态：段级归一，参数名不参与比对。
        return re.sub(r"\$\{[^}]*\}", "{p}", literal)

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from rolecard_agent.api.main import create_app  # noqa: PLC0415

    app = create_app()
    # 口径与基线同源：`app.openapi()`（`app.routes` 在这个 FastAPI 版本里把 router 包成
    # `_IncludedRouter`，不暴露 path —— 别换回去）。
    backend_paths = set(app.openapi()["paths"].keys())
    # 后端的路径参数名不参与比对：/api/session/{tid} 按"段数 + 常量段"归一
    def shape(path: str) -> tuple[str, ...]:
        return tuple(
            re.sub(r"\{[^}]*\}", "{p}", seg) for seg in path.strip("/").split("/") if seg
        )

    backend_shapes = {shape(p) for p in backend_paths}
    dangling: list[str] = []
    checked: set[str] = set()
    for rel, lineno, literal in frontend_literals():
        key = normalize(literal)
        if key in checked:
            continue
        checked.add(key)
        if shape(key) not in backend_shapes:
            dangling.append(f"{rel}:{lineno} {literal}")
    assert not dangling, (
        "前端有指不到后端路由的端点字面量（R102-12 的可达性半边）："
        f"{dangling[:8]}"
    )

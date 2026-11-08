"""真实能力探测：按能力发**最小真请求**，结论记进 `ServiceEndpointService` 的探测台账。

为什么"看配置算可用"不行（2026-11-08 实测）：硅基流动余额耗尽时回 HTTP 402
（`30001 balance is insufficient`），而 `/models` 照回 200 —— 服务页徽标写"可用"、
对话必挂。配置齐 ≠ 能花钱，判可用的唯一诚实办法是真发一次。

三条纪律（判据在 `tests/test_services_api.py` 的"真实能力探测"段）：

  1. **只由 `POST /api/services/probe` 发起**（前端仅首载与「重新检测」）——普通
     GET 只读台账、零外发，配额是钱；
  2. **探能力端点，不探 `/models`**：嵌入 → `{base}/embeddings`，重排 →
     `{base}/rerank`，对话行 → `{base}/chat/completions`（走 model_probe 的
     `resolve_target` + `probe_chat`：SSRF 校验、按风格拼 URL 与鉴权头，与运行时
     同一把钥匙）；
  3. **没凭据就不发**：本地行没有外发（模型在位归 vision_model_ready 的 TTL 探活）、
     无 key 行没有凭据可发；OCR 云端行（OCR.space）不是 OpenAI 兼容形状 —— 硬发
     chat 会把"形状不对"误报成"不可用"，宁可留给配置缺省。

结论进台账而非直接回显：GET 读账零外发；身份键含 key 指纹，换了 key/端点/模型
旧结论自动作废（见 `probe_identity`）。并行 4 个 worker；正常失败（402/401）是
毫秒级返回，10s 超时只留给真挂了的厂商（对话行沿用 model_probe 的补全超时）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from rolecard_agent.base import outbound
from rolecard_agent.core.model_settings import ModelSettingsService, client_style
from rolecard_agent.core.models.services import (
    SERVICE_CATEGORIES,
    EndpointConfig,
    ProbeOutcome,
    ServiceEndpointService,
    probe_identity,
)
from rolecard_agent.core.probes.model_probe import probe_chat, resolve_target

#: 能力 → URL 尾段与请求体（全是**只读**语义：嵌入一段短文本、重排一条文档）。
#: 对话行不在这里 —— 它走 model_probe，别在第三处实现 chat 请求（审查打过的靶：
#: URL 拼法/鉴权头漂移）。
_CAP_PATH: dict[str, str] = {"embedding": "embeddings", "rerank": "rerank"}
_CAP_BODY: dict[str, Callable[[str], dict[str, object]]] = {
    "embedding": lambda model: {"model": model, "input": ["服务页探测"]},
    "rerank": lambda model: {
        "model": model,
        "query": "服务页探测",
        "documents": ["服务页探测"],
        "top_n": 1,
    },
}

_CAP_TIMEOUT = 10.0  # 秒：402/401 毫秒级返回；这个上限只兜"厂商真挂了"的连接等待
_REASON_OK = "实测可用（HTTP 200）"
_HTTP_LINE = re.compile(r"HTTP (\d+)：")


def _outcome(error: str) -> ProbeOutcome:
    """错误串 → 给人看的结论。错误串沿用 model_probe 的约定：
    `HTTP {code}：{正文前120字}` 或 `{异常类型}: {说明}`；空串 = 成功。

    402/401/404 翻译成处置话术（用户要的是"下一步做什么"，不是裸状态码）。
    """
    if not error:
        return ProbeOutcome(ok=True, reason=_REASON_OK)
    m = _HTTP_LINE.match(error)
    if m is None:
        if ": " in error:  # 异常形状 `Type: msg` —— 连都没连上
            return ProbeOutcome(ok=False, reason=f"实测不可达：{error}"[:180])
        return ProbeOutcome(ok=False, reason=f"实测不可用：{error}"[:180])
    code = int(m.group(1))
    if code == 402:
        return ProbeOutcome(
            ok=False,
            reason="实测不可用：402 额度不足（去厂商页面充值，或去模型页换 key）",
        )
    if code == 401:
        return ProbeOutcome(ok=False, reason="实测不可用：401 Key 无效（去模型页换 key）")
    if code == 404:
        return ProbeOutcome(ok=False, reason="实测不可用：404 端点路径不存在（检查 base_url）")
    return ProbeOutcome(ok=False, reason=f"实测不可用：{error}"[:180])


def _capability_thunk(e: EndpointConfig, url: str, body: dict[str, object]) -> Callable[[], str]:
    """一个能力行的一次真请求 → 错误串（空 = 可用）。

    鉴权 = Bearer key：嵌入/重排的云端引用都是 OpenAI 兼容形状（SiliconFlow 与各类
    中转站；原生 Ollama 没有 key，早在入口被"无 key 跳过"）。这是**第三处**发
    POST 的地方吗？不是：请求形状只有两处 —— 这里（能力）与 model_probe（对话），
    各自只有自己的 URL 尾段，鉴权头同形。
    """

    def run() -> str:
        try:
            res = outbound.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {e.api_key}"},
                timeout=_CAP_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - 调不通就是结论
            return f"{type(exc).__name__}: {exc}"[:180]
        if res.status_code != 200:
            return f"HTTP {res.status_code}：{res.text[:120]}"
        return ""

    return run


def probe_all(
    svc: ServiceEndpointService,
    ms: ModelSettingsService,
    *,
    user_id: str,
) -> list[dict[str, object]]:
    """把本轮该探的行全部探完（并行），结论记进台账，返回响应的结果行。

    结果行的 `category` 是**视图类别名**（对话行用 `models`，与服务页分组一致）；
    台账身份键里对话行的类目是 `chat`（内部命名，别与能力三类撞名）。
    停用行不探（状态无人消费，不花配额）；没 key / 本地 / OCR 云端按第 3 条纪律跳过。
    """
    # (结果类别, 行 id, 台账身份, 请求 thunk) —— 闭包靠**参数默认绑定**取值，
    # 否则循环变量在 pool.map 起跑前就被下一轮覆盖，全部探成最后一行。
    jobs: list[tuple[str, str, str, Callable[[], str]]] = []

    for cat in SERVICE_CATEGORIES:
        for e in svc.rows(cat.key):
            if not e.enabled:
                continue
            if e.kind != "cloud" or not e.api_key:
                continue
            path = _CAP_PATH.get(cat.key)
            if path is None:
                # OCR 云端（OCR.space）不是 OpenAI 兼容形状：硬发 chat/能力请求会把
                # "形状不对"误报成"不可用"。留给配置缺省（key 齐缺在视图里答）。
                continue
            url = f"{(e.base_url or '').rstrip('/')}/{path}"
            body = _CAP_BODY[cat.key](e.model or "")
            identity = probe_identity(
                category=cat.key,
                row_id=e.id,
                base_url=e.base_url,
                model=e.model,
                api_key=e.api_key,
            )
            jobs.append((cat.key, e.id, identity, _capability_thunk(e, url, body)))

    # 对话行：与状态视图**同一张池**（usage=chat、同序）、同一把钥匙
    # （group_for 取真 key 算指纹；resolve_target 拼靶子走 SSRF 校验）。
    backends = ms.list_backends(user_id=user_id)
    for row in backends:
        if str(row.get("usage", "chat")) != "chat":
            continue
        if client_style(str(row.get("provider", ""))) == "native":
            # 本地 Ollama 不外发：模型在位由 vision_model_ready 的 TTL 探活回答。
            continue
        if not row.get("has_key") or not row.get("provider_id"):
            continue  # 没有可发的凭据（视图那边也保持"还没测过"的缺省）
        name = str(row["name"])
        provider_id = str(row["provider_id"])
        row_model = str(row.get("model", ""))
        group = ms.group_for(user_id=user_id, group_id=provider_id)
        raw_key = str((group or {}).get("api_key") or "")
        if not raw_key:
            continue
        identity = probe_identity(
            category="chat",
            row_id=name,
            base_url=str(row.get("base_url") or ""),
            model=row_model,
            api_key=raw_key,
        )

        def chat_thunk(
            *, _provider_id: str = provider_id, _model: str = row_model, _uid: str = user_id
        ) -> str:
            try:
                target = resolve_target(ms, user_id=_uid, provider_id=_provider_id, model=_model)
            except Exception as exc:  # noqa: BLE001 - 拼不出靶子本身就是结论
                return f"{type(exc).__name__}: {exc}"[:180]
            ok, error = probe_chat(target)
            return "" if ok else error

        jobs.append(("models", name, identity, chat_thunk))

    if not jobs:
        return []
    # 两个 thunk 各自把网络层异常吃成错误串（能力路在 _capability_thunk、对话路在
    # resolve/probe_chat 的口径里）—— 它们是**全函数**，这里不再包一层兜底：
    # 一个永远接不到异常的 except 分支比没有更糟（服务端点测试的教训原样适用）。
    with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
        errors = list(pool.map(lambda j: j[3](), jobs))
    results: list[dict[str, object]] = []
    for (cat_key, row_id, identity, _thunk), error in zip(jobs, errors, strict=True):
        outcome = _outcome(error)
        svc.record_probe(identity, outcome)
        results.append(
            {
                "category": cat_key,
                "id": row_id,
                "available": outcome.ok,
                "reason": outcome.reason,
            }
        )
    return results

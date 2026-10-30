"""SillyTavern 角色卡导入器（2026-10-24，2c）—— 站在协议上借 ST 的卡生态。

**为什么是"解析成自家的卡"而不是"跑 ST"**：ST 的卡格式（PNG 内嵌 JSON 的 chara 规范）
是公开的事实标准，而 ST 本体是给人用的扮演前端 —— 导入**数据格式**零上游依赖
（AGPL 也不沾），这是 2026-10-24 架构对话里定下的边界："依赖协议，不依赖框架"。

**字段映射表**（改映射前先读；`report` 里逐项留痕，绝不静默丢）：

  ST 字段                    → RoleCard            说明
  name                       → role_name + role_id   role_id 是 slug 化的（见 `_slugify_role_id`）
  description                → system_prompt 【人设】
  personality                → system_prompt 【性格】
  scenario                   → system_prompt 【场景】
  system_prompt (v2)         → system_prompt 开头     ST 的显式覆盖段，有就放在最前
  first_mes                  → system_prompt 【开场白】  内核没有"第一句话"概念，作为参考段落保留
  mes_example                → exemplars             `<START>` 分块逐块拆 (user, char) 对；
                                                       `{{char}}`→角色名、`{{user}}`→"用户"；
                                                       超 MAX_EXEMPLARS / 字数预算 → 整条丢弃并计数
  creator_notes              → description
  character_book（世界书）    → **不导入**，report 计数  知识要走本仓的 RAG（知识页上传建作用域），
                                                       卡导入不该悄悄建向量库
  tags / extensions / 其它    → report 计数，不导入

PNG 卡：tEXt 块 keyword `chara`（V1/V2）或 `ccv3`（V3）+ base64(JSON)。**ccv3 优先**
（ST 规范要求读取方在两块并存时优先 ccv3，且 ccv3 的 `data` 与 V2 同构，落同一张
映射表；report 的 `spec` 字段如实记来路）。
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any

from rolecard_agent.roles.models import (
    MAX_EXEMPLAR_CHARS,
    MAX_EXEMPLARS,
    RoleCardCreate,
    RoleExemplar,
)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class CardImportError(ValueError):
    """卡数据读不进来（不是 PNG / 不是 JSON / PNG 里没有卡块 / 缺 name）。"""


@dataclass(frozen=True, slots=True)
class CardImportResult:
    """解析产物：可创建的卡 + 一份"哪些东西去了哪 / 哪些被丢了"的对账单。"""

    card: RoleCardCreate
    report: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- 载体层（PNG / JSON）


def _chara_json_from_png(raw: bytes) -> dict[str, Any]:
    """PNG 的 tEXt 块里找角色卡 JSON：**ccv3 优先，`chara` 兜底**（R1031-14）。

    ST 规范（character-card-spec-v3）写明 PNG 可以同时带 `ccv3` 与 `chara` 两块、
    读取方应当优先 ccv3 —— 新版 ST 导出的卡大多两块都有；从前只认 `chara`，
    撞上"只有 ccv3 块"的新卡会报"没有卡数据块（它可能只是一张普通图片）"，
    对用户是双重误导。ccv3 的 JSON 与 V2 同构（`{spec, data}`），落进同一张映射表。
    """
    chara_text: bytes | None = None
    ccv3_text: bytes | None = None
    pos = 8  # 跳过 PNG 签名
    while pos + 8 <= len(raw):
        length = int.from_bytes(raw[pos : pos + 4], "big")
        chunk_type = raw[pos + 4 : pos + 8]
        chunk_data = raw[pos + 8 : pos + 8 + length]
        if chunk_type == b"tEXt":
            keyword, _, text = chunk_data.partition(b"\x00")
            if keyword == b"chara":
                chara_text = text
            elif keyword == b"ccv3":
                ccv3_text = text
        pos += 12 + length  # 4 长度 + 4 类型 + 数据 + 4 CRC
    chosen = ccv3_text if ccv3_text is not None else chara_text
    if chosen is None:
        raise CardImportError("PNG 里没有角色卡数据块（chara/ccv3 都不在，它可能只是一张普通图片）")
    try:
        decoded = json.loads(base64.b64decode(chosen).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CardImportError(f"PNG 卡数据块解不开（base64/JSON 损坏）：{exc}") from exc
    if not isinstance(decoded, dict):
        raise CardImportError("PNG 卡数据块不是 JSON 对象")
    return decoded


def load_card_bytes(raw: bytes) -> dict[str, Any]:
    """PNG 或裸 JSON → 卡数据 dict。两条载体一个入口，路由层不关心区别。"""
    if raw[:8] == _PNG_SIGNATURE:
        return _chara_json_from_png(raw)
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CardImportError("不是 PNG 角色卡，也不是能解析的 JSON") from exc
    if not isinstance(decoded, dict):
        raise CardImportError("卡文件顶层必须是 JSON 对象")
    return decoded


# ---------------------------------------------------------------- 映射层（ST → 本仓）


def _slugify_role_id(name: str) -> str:
    """角色名 → 合法 role_id（`ROLE_ID_PATTERN`：小写字母开头，a-z0-9_，≤64）。

    纯中文名 slug 出来是空串、数字开头也不合法 —— 那就 `st_` + 名字哈希前 8 位：
    确定性（同名同 id，重导撞 409 是对的），不掷骰子。
    """
    import hashlib  # noqa: PLC0415 - 只在中文纯名这一条支路上用

    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower())
    slug = re.sub(r"_+", "_", slug).strip("_")[:63].strip("_")
    if slug and not slug[0].isalpha():
        slug = f"st_{slug}"[:63]
    if not slug:
        slug = f"st_{hashlib.md5(name.encode('utf-8')).hexdigest()[:8]}"
    return slug


def _example_blocks(mes_example: str, role_name: str) -> list[RoleExemplar]:
    """`<START>` 分块 → (user, char) 对。`{{char}}`/`{{user}}` 宏换成会落进提示词的字面。"""
    out: list[RoleExemplar] = []
    blocks = [b.strip() for b in mes_example.split("<START>") if b.strip()]
    for block in blocks:
        user_lines: list[str] = []
        char_lines: list[str] = []
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("{{user}}:"):
                user_lines.append(stripped[len("{{user}}:") :].strip())
            elif stripped.startswith("{{char}}:"):
                char_lines.append(stripped[len("{{char}}:") :].strip())
            elif char_lines:
                # 同一角色的多行发言：接在上一条后面
                char_lines[-1] = f"{char_lines[-1]}\n{stripped}"
        if user_lines and char_lines:
            # 宏换字面：本仓的范例不做宏替换（那是 ST 渲染层的事），落进提示词前先定稿。
            def resolve(text: str) -> str:
                return text.replace("{{user}}", "用户").replace("{{char}}", role_name)

            out.append(
                RoleExemplar(
                    user=resolve("\n".join(user_lines)),
                    assistant=resolve("\n".join(char_lines)),
                )
            )
    return out


def _budgeted_exemplars(examples: list[RoleExemplar], report: dict[str, Any]) -> list[RoleExemplar]:
    """卡预算（`MAX_EXEMPLARS` / `MAX_EXEMPLAR_CHARS`）的显式执行：超了整条丢、丢了几条进 report。

    为什么不静默截断：validation 一抛就是整张卡 400，用户对着一锅粥不知道哪条超标；
    这里丢整条并对账，界面能如实说"倒了 3 条"。
    """
    kept: list[RoleExemplar] = []
    total = 0
    for example in examples:
        size = len(example.user) + len(example.assistant)
        if len(kept) >= MAX_EXEMPLARS or total + size > MAX_EXEMPLAR_CHARS:
            report["exemplars_dropped"] = report.get("exemplars_dropped", 0) + 1
            continue
        kept.append(example)
        total += size
    return kept


def _section(label: str, body: str) -> str:
    return f"【{label}】\n{body.strip()}" if body.strip() else ""


def parse_sillytavern_card(data: dict[str, Any]) -> CardImportResult:
    """ST 卡 JSON（V2 嵌套 / V1 平铺都认）→ 可创建的卡 + 对账单。"""
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(inner, dict):
        raise CardImportError("卡里既没有 v2 的 data 对象也不是 v1 平铺形状")
    name = str(inner.get("name") or "").strip()
    if not name:
        raise CardImportError("卡里没有 name —— 没有名字的卡没法挂进角色列表")

    report: dict[str, Any] = {"spec": str(data.get("spec") or "v1"), "source_name": name}
    role_id = _slugify_role_id(name)

    description = str(inner.get("description") or "").strip()
    personality = str(inner.get("personality") or "").strip()
    scenario = str(inner.get("scenario") or "").strip()
    st_system = str(inner.get("system_prompt") or "").strip()
    first_mes = str(inner.get("first_mes") or "").strip()

    sections = [
        _section("人设", description),
        _section("性格", personality),
        _section("场景", scenario),
    ]
    prompt_parts = [st_system] + [s for s in sections if s]
    if first_mes:
        prompt_parts.append(_section("开场白（首次问候可参考）", first_mes))
    system_prompt = "\n\n".join(p for p in prompt_parts if p).strip()
    if not system_prompt:
        raise CardImportError(
            "卡里没有可用的正文（description / personality / scenario / first_mes 全空）"
        )

    examples = _example_blocks(str(inner.get("mes_example") or ""), name)
    report["exemplar_blocks"] = len(examples)
    kept = _budgeted_exemplars(examples, report)

    creator_notes = str(inner.get("creator_notes") or "").strip()
    book = inner.get("character_book")
    if isinstance(book, dict):
        entries = book.get("entries")
        report["worldbook_entries_dropped"] = len(entries) if isinstance(entries, list) else 0
    tags = inner.get("tags")
    report["tags_dropped"] = len(tags) if isinstance(tags, list) else 0
    report["alternate_greetings_dropped"] = len(inner.get("alternate_greetings") or [])

    try:
        card = RoleCardCreate(
            role_id=role_id,
            role_name=name,
            system_prompt=system_prompt,
            description=creator_notes or None,
            exemplars=kept or None,
        )
    except Exception as exc:  # noqa: BLE001 - 形状校验失败要给人话，不能 500
        raise CardImportError(f"卡数据过了映射但没过本仓的形状校验：{exc}") from exc
    report["role_id"] = role_id
    return CardImportResult(card=card, report=report)


# ---------------------------------------------------------------- 反向映射（本仓 → ST V2）


def build_st_v2_card(
    card: Any,
    memory_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """本仓的卡 → ST V2 形状（`spec: "chara_card_v2"`），设计稿 §3 那条"隐私出口"的落地。

    反向映射的取舍（与正向映射表对读）：
      * system_prompt 整体放进 `data.description` —— 正向导入时人设/性格/场景已并进
        system_prompt，反向不猜拆（拆错比不拆更糟），ST 侧拿到的就是完整人格载体；
      * exemplars 反向拼回 `<START>` 块 + `{{char}}`/`{{user}}` 宏（ST 的原生形状）；
      * creator_notes 放回 `data.creator_notes`；description（本仓的备注字段）以
        `extensions.rolecard_agent.description` 随行，不在 ST 侧占位置；
      * **记忆默认不随卡走**：调用方显式给了 `memory_items`（pinned+active，见路由层
        `?include_memory=pinned_active`）才写进 `data.extensions.rolecard_agent.memory`。
        这是设计稿 §3 的原话口径 —— 导出/分享是卡文件的出口，不是记忆的出口。
    """
    data: dict[str, Any] = {
        "name": card.role_name,
        "description": card.system_prompt,
        "personality": "",
        "scenario": "",
        "first_mes": "",
        "mes_example": _exemplars_to_st(card.exemplars or [], card.role_name),
        "creator_notes": card.description or "",
    }
    extensions: dict[str, Any] = {
        "rolecard_agent": {
            "role_id": card.role_id,
            "temperature": card.temperature,
            "description": card.description,
        }
    }
    if memory_items:
        extensions["rolecard_agent"]["memory"] = memory_items
    data["extensions"] = extensions
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


def _exemplars_to_st(exemplars: list[Any], role_name: str) -> str:
    """范例对 → ST 的 `<START>` 分块文本（宏用回 `{{char}}`/`{{user}}`）。"""
    blocks: list[str] = []
    for ex in exemplars:
        lines = [f"{{{{user}}}}: {ex.user}", f"{{{{char}}}}: {ex.assistant}"]
        blocks.append("\n".join(lines))
    return "\n<START>\n".join(blocks)

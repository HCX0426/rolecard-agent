"""DTOs: RoleCard, RoleCardCreate, RoleCardUpdate.

Reproducing a role takes four channels, not one. Two belong to the role card, one to the
kernel, one to the plugin layer:

  人设规则    system_prompt        rules and boundaries
  行为范例    exemplars            how the role answers - the strongest lever per token
  能力权限    tool_whitelist       what it may do
  事实知识    knowledge_scopes     which retrieval scopes it may read (declared, not owned)

Fields of note:

  is_builtin        built-in roles cannot be deleted (D6)
  model_name        a backend NAME from config.MODEL_BACKENDS, not a raw model id - this is
                    what makes per-role routing work (medical role -> local, chat -> cloud)
  tool_whitelist    None = all tools of enabled plugins, [] = no tools at all
  knowledge_scopes  None = no retrieval, [] = no retrieval, [..] = those collections only.
                    A role never owns a vector store; N roles x M stores would duplicate
                    indexes and leave no single source of truth.
  temperature       a SUGGESTION, not a guarantee. Some reasoning models ignore it entirely,
                    and its valid range differs per provider. Never let downstream logic
                    depend on it being honoured (C15).

Both lists are stored as JSON for simplicity in v1. Trade-off to state out loud in the
interview: it costs queryability ("which roles can use tool X" needs a full scan);
production would split them into join tables.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, field_validator

from rolecard_agent.base.identity import DEFAULT_USER_ID

ROLE_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
SCOPE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
#: 桌宠形象包的 slug。为什么不是 `SCOPE_PATTERN` 的复用：包名要能写成 `elysia-live2d`
#: 这种带连字符的样子（丢进目录的那只手是人，人在起名字时用 `-`），而检索作用域名不许。
#: 只校验形状，**不校验"这个包到底存不存在"** —— 那份清单长在素材目录里
#: （见 `features/pet_packs.py`），在这里再问一遍就是第二个事实源，而且改素材要重启后端才生效。
PET_PACK_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
#: 固定时刻问候（`greeting_at`）的形状：24 小时制 HH:MM。校验只管形状 —— "这个时刻合不
#: 合适"由角色卡的主人自己判断，调度器侧的当日去重与静默闸负责不吵。
GREETING_AT_PATTERN = r"([01]\d|2[0-3]):[0-5]\d"
#: 角色强调色（2026-10-25 玻璃重设计）：hex 三/六位。空 = 由 role_id 哈希派生
# （确定性、不掷骰子——同名同色）。校验只管形状，"好不好看"是主人的事。
ACCENT_PATTERN = r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"

# Exemplar budget. Chars are used rather than tokens because it needs no tokenizer
# dependency, and for Chinese text a character budget is a conservative proxy. The cap is
# not bureaucracy: examples compete with the conversation for context, and past a handful the
# marginal example mostly dilutes the earlier ones.
MAX_EXEMPLARS = 4
MAX_EXEMPLAR_CHARS = 3000


class RoleExemplar(BaseModel):
    """One worked example of how this role answers.

    规矩（09-27 定，`Q8`）：**范例教的是句式与边界，不是事实**。一条写着"尿酸 488.0"的
    范例，模型有概率在**别的问句**上把那个数当事实复读出来 —— 那正是 A3 那把引用尺子里的
    `answer_absent_value`。所以默认写法是"这一句该有什么结构、哪里要停"，数值交给工具去取。

    唯一的例外要写清楚，因为出厂那张档案管理员卡就落在例外里：当一条范例的**存在理由**就是
    教一个带格式的读数（例如结尾那句【未经人工校验】标记），数值可以给，但必须是
    **演示数据里真实存在的那一个** —— 不许编（编的数会被当成可引用的真值），也不许是评测题的
    答案（`check_consistency.py` 那条"范例泄漏评测答案"的机器检查治的就是这个：一旦是答案，
    模型学会的是背范例而不是调工具）。
    """

    user: str = Field(min_length=1, max_length=1000)
    assistant: str = Field(min_length=1, max_length=2000)


def _check_exemplar_budget(value: list[RoleExemplar] | None) -> list[RoleExemplar] | None:
    if value is None:
        return None
    if len(value) > MAX_EXEMPLARS:
        raise ValueError(f"at most {MAX_EXEMPLARS} exemplars, got {len(value)}")
    total = sum(len(item.user) + len(item.assistant) for item in value)
    if total > MAX_EXEMPLAR_CHARS:
        raise ValueError(f"exemplars total {total} chars, budget is {MAX_EXEMPLAR_CHARS}")
    return value


def _check_scopes(value: list[str] | None) -> list[str] | None:
    """Scope names become collection identifiers - validate rather than trust."""
    if value is None:
        return None
    for name in value:
        if not re.match(SCOPE_PATTERN, name):
            raise ValueError(f"invalid knowledge scope name: {name!r}")
    return value


def _check_accent(value: str | None) -> str | None:
    """强调色形状校验：#RGB / #RRGGBB；空 = 派生。存小写归一。"""
    if not value:
        return None
    if not re.fullmatch(ACCENT_PATTERN, value):
        raise ValueError("accent 要写成 hex 颜色（#4f6bed 或 #4fe）")
    return value.lower()


def _check_greeting_at(value: str | None) -> str | None:
    """`greeting_at` 的形状校验与归一：空串与 None 同义 = 没排，归一成 None。"""
    if not value:
        return None
    if not re.fullmatch(GREETING_AT_PATTERN, value):
        raise ValueError("greeting_at 要写成 HH:MM（24 小时制，本地时区），如 09:00")
    return value


def _check_workspace_dir(value: str | None) -> str | None:
    """`workspace_dir` 的形状校验：空 = 跟随全局；非空必须是**绝对路径**。

    为什么要求绝对：这一列会被 `Path(value).resolve()` 直接当根用，而相对路径是按
    **进程 CWD** 解析的 —— 同一个库在壳里与服务里 CWD 不同，于是"这个角色能用哪个目录"
    取决于它从哪儿启动，那不是授权边界，是巧合。绝对路径是唯一能跨形态稳定的写法。
    （越界防护不在这里：`resolve_within` 才是那道闸。）
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if not Path(text).is_absolute():
        raise ValueError("工作区要写成绝对路径（相对路径会按启动目录解析，不稳定）")
    return str(Path(text))


ExemplarList = Annotated[list[RoleExemplar] | None, AfterValidator(_check_exemplar_budget)]
ScopeList = Annotated[list[str] | None, AfterValidator(_check_scopes)]
GreetingAt = Annotated[str | None, AfterValidator(_check_greeting_at)]
Accent = Annotated[str | None, AfterValidator(_check_accent)]
WorkspaceDir = Annotated[str | None, AfterValidator(_check_workspace_dir)]


class RoleCard(BaseModel):
    """A role as stored. `is_builtin` roles cannot be deleted."""

    role_id: str = Field(pattern=ROLE_ID_PATTERN)
    #: 归属哪个主人（列定义见 `roles/schema.sql`）。**这是读模型上的字段，不是入参**：
    #: 写路径由 `RoleCards` 视图绑定的那个人盖章，请求体给不了
    #: （`RoleCardCreate` 刻意没有 `user_id`）。默认值只为让"随手构造一张卡"（内部逻辑与
    #: 测试里造个角色）不必假装知道归属。
    user_id: str = DEFAULT_USER_ID
    role_name: str = Field(min_length=1)
    system_prompt: str
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)
    model_name: str | None = None
    tool_whitelist: list[str] | None = None
    exemplars: ExemplarList = None
    knowledge_scopes: ScopeList = None
    description: str | None = None
    # 主动开口（架构计划 B）：角色是否会主动来找用户（还需要全局 REACHOUT_ENABLED 开着）。
    # 默认 False = 出厂即静默 —— 主动打扰是 opt-in，不是默认行为。
    reachout_enabled: bool = False
    # 关系驱动主动开口（架构总览 §5）：两类关系驱动触发源的 per-role 开关。
    # 默认 True = 一旦开启 reachout_enabled，回忆 / 时段规律两类关系驱动开口即生效。
    recall_enabled: bool = True
    time_pattern_enabled: bool = True
    #: 「关系数值到阈值就想开口」这一档的开关。09-26 轮 R26-23：触发源是排他的一条 `or`
    #: 链，affinity 触顶后这一档永久命中，后面的时段规律 / 回忆 / 定时再也轮不到。
    affinity_enabled: bool = True
    # 文件事件触发（架构总览 §5）per-role 闸门：该角色可否被任务目录变化触发。
    file_watch_enabled: bool = True
    #: 固定时刻问候（2026-10-24，2a）：每天这个本地时刻主动开口一次（HH:MM）。
    #: None = 不排。它**过不了**静默闸（基线间隔没到照旧被压）—— 排好的问候不享有
    #: 越过"别太吵"纪律的特权；当日去重读投递台账（`agent_reachout.fired_by='scheduled'`），
    #: 不另建状态表。触发实现在 `features/reachout/triggers.trigger_scheduled`。
    greeting_at: GreetingAt = None
    #: 收件箱自动保留条数：0 = 不自动删（默认）；N>0 = 只留最近 N 条。删的是投递记录，
    #: 不是她说出口的那句话（那句留在主动会话里 —— 那是她下次开口的依据）。
    reachout_keep: int = Field(default=0, ge=0, le=1000)
    #: 桌宠用哪个形象包（列定义见 `roles/schema.sql`）。空串 = 从没配过 → 渲染侧落默认包。
    pet_pack: str = ""
    #: 角色强调色（2026-10-25 玻璃重设计）：这个角色的糖衣色——侧栏角色块、气泡签、
    #: 界面雾底都从它派生。None = 由 role_id 哈希派生（确定性）。前端落地见 App.tsx
    #: 的 accent 注入。
    accent: Accent = None
    #: 这个角色自己的工作区（2026-10-30）。None = 跟随全局默认（Settings.workspace_dir
    #: 或全局覆盖）；非空 = 这张卡独享的目录 —— fs 工具与命令执行的根按"当前角色"解析，
    #: 授权边界跟着角色走，而不是跟着"当时谁在看那张卡"走。
    workspace_dir: WorkspaceDir = None
    is_builtin: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RoleCardCreate(BaseModel):
    """Input for creating a role. `is_builtin` is intentionally absent - it is set by
    `roles/seed.py`, never by a caller."""

    role_id: str = Field(pattern=ROLE_ID_PATTERN)
    role_name: str = Field(min_length=1)
    system_prompt: str
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)
    model_name: str | None = None
    tool_whitelist: list[str] | None = None
    exemplars: ExemplarList = None
    knowledge_scopes: ScopeList = None
    description: str | None = None
    reachout_enabled: bool = False
    recall_enabled: bool = True
    time_pattern_enabled: bool = True
    affinity_enabled: bool = True
    file_watch_enabled: bool = True
    greeting_at: GreetingAt = None
    reachout_keep: int = Field(default=0, ge=0, le=1000)
    pet_pack: str = ""
    accent: Accent = None
    #: 新建时就带上工作区（一般留空 = 跟随全局默认；要单独目录就在这儿给绝对路径）。
    workspace_dir: WorkspaceDir = None

    @field_validator("role_id")
    @classmethod
    def _lowercase(cls, value: str) -> str:
        if value != value.lower():
            raise ValueError("role_id must be lowercase ASCII")
        return value

    @field_validator("pet_pack")
    @classmethod
    def _pet_pack_slug(cls, value: str) -> str:
        # 空串是合法值（"没配过"），所以先放行再比形状。
        if value and not re.fullmatch(PET_PACK_PATTERN, value):
            raise ValueError("pet_pack 只能是包名 slug（小写字母开头，a-z0-9_- 至多 64 位）")
        return value


class RoleCardUpdate(BaseModel):
    """Partial update. Unset fields are left untouched - use `model_fields_set` to tell
    "not provided" apart from "explicitly set to null"."""

    role_name: str | None = Field(default=None, min_length=1)
    system_prompt: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=1.0)
    model_name: str | None = None
    tool_whitelist: list[str] | None = None
    exemplars: ExemplarList = None
    knowledge_scopes: ScopeList = None
    description: str | None = None
    reachout_enabled: bool | None = None
    recall_enabled: bool | None = None
    time_pattern_enabled: bool | None = None
    affinity_enabled: bool | None = None
    file_watch_enabled: bool | None = None
    #: 排固定时刻问候：HH:MM；空串/None = 清除（当日不再触发）。不传这个键 = 不动。
    greeting_at: GreetingAt = None
    #: 改强调色：hex；空串 = 回落派生色。不传这个键 = 不动。
    accent: Accent = None
    reachout_keep: int | None = Field(default=None, ge=0, le=1000)
    #: 改桌宠形象。传空串 = 回到"没配过"（渲染落默认包）；不传这个键 = 不动。
    pet_pack: str | None = None
    #: 改这个角色自己的工作区：绝对路径 = 独享一份；传空串 = 回落全局默认。
    #: 不传这个键 = 不动（"没这个字段"与"清空它"要分得开 —— 靠 `model_fields_set`）。
    workspace_dir: WorkspaceDir = None

    @field_validator("pet_pack")
    @classmethod
    def _pet_pack_slug(cls, value: str | None) -> str | None:
        if value and not re.fullmatch(PET_PACK_PATTERN, value):
            raise ValueError("pet_pack 只能是包名 slug（小写字母开头，a-z0-9_- 至多 64 位）")
        return value

    def changes(self) -> dict[str, object]:
        """Only the fields the caller actually provided."""
        return {name: getattr(self, name) for name in self.model_fields_set}

    def is_empty(self) -> bool:
        return not self.model_fields_set

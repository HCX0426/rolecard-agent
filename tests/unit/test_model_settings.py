"""模型配置服务（core/model_settings.py）的单元测试。

覆盖代码审查修复的不变量（H5/M2/M8）与拆层后的三条新纪律：

  * **一把 key 只有一个家**：同一 (供应商, 端点) 下的多个模型共用一条 `model_provider`，
    省略 api_key = 保留组里已存的（不是"这行没 key"）；组里最后一个模型被删 = 组随 key 一起消失。
  * **用途是派生的**：`usage`/`used_by` 来自 `service_endpoint` 引用行，`save_chat_pool`
    写它即定义它 —— 模型页不再有可写的用途列。
  * **能力位三态**：省略 = 保留库里已存的（含"没测过"），不是回落到默认值。
  * **搬层无损**：旧库的 N 行折叠成 M 个组时，模型行、key、默认与回退顺序一个都不能变少。
"""

from __future__ import annotations

import pytest

from rolecard_agent.config import ModelBackend, Settings
from rolecard_agent.core.model_settings import (
    UNASSIGNED_USAGE,
    ModelSettingsError,
    ModelSettingsService,
    migrate_to_provider_layers,
    unmanaged_backend_columns,
    validate_base_url,
)
from rolecard_agent.storage.db import bootstrap, connect


def _conn() -> object:
    c = connect(":memory:")
    bootstrap(c, enabled_domains=())
    return c


# --------------------------------------------------------------------------- #
# H5: effective_settings 不复活被删的 env 后端
# --------------------------------------------------------------------------- #

def test_effective_settings_falls_back_to_env_before_seed() -> None:
    env = Settings(
        model_backends={
            "local": ModelBackend(
                provider="ollama", model="qwen2.5:7b",
                base_url="http://localhost:11434", api_key="ollama",
            ),
        },
        model_default="local",
        model_fallbacks=[],
    )
    svc = ModelSettingsService(_conn())
    # 表空（首启前）→ 直接退回 env 对象，不做任何合并。
    eff = svc.effective_settings(env)
    assert eff is env


def test_effective_settings_does_not_revive_deleted_env_backend() -> None:
    env = Settings(
        model_backends={
            "local": ModelBackend(
                provider="ollama", model="qwen2.5:7b",
                base_url="http://localhost:11434", api_key="ollama",
            ),
            # env 仍提供 ghost —— 但操作员已在 UI 删掉它。
            "ghost": ModelBackend(
                provider="openai", model="gpt-4o",
                base_url="https://api.openai.com/v1", api_key="sk-x",
            ),
        },
        model_default="local",
        model_fallbacks=[],
    )
    svc = ModelSettingsService(_conn())
    svc.seed_from_env(env)  # 首启：env 后端进表
    # 操作员删除 ghost（重存为只剩 local）。
    svc.save(
        default="local",
        backends=[{
            "name": "local", "provider": "ollama", "model": "qwen2.5:7b",
            "base_url": "http://localhost:11434", "api_key": "ollama",
            "usage": "chat",
        }],
    )
    eff = svc.effective_settings(env)
    # 关键：env 里还有 ghost，但表已删 → 绝不复活。
    assert "ghost" not in eff.model_backends
    assert set(eff.model_backends) == {"local"}
    assert eff.model_default == "local"


# --------------------------------------------------------------------------- #
# M2: 对话默认后端必须落在 chat 用途行
# --------------------------------------------------------------------------- #

def test_save_rejects_non_chat_default() -> None:
    svc = ModelSettingsService(_conn())
    with pytest.raises(ModelSettingsError, match="必须是 chat 用途"):
        svc.save(
            default="emb",
            backends=[
                {"name": "emb", "provider": "ollama", "model": "bge-m3",
                 "usage": "embedding"},
                {"name": "chat", "provider": "ollama", "model": "qwen2.5:7b",
                 "usage": "chat"},
            ],
        )


def test_chat_pool_rejects_unknown_backend() -> None:
    """对话序列只能写模型页已有的行（凭空造引用 = 指向不存在的模型）。"""
    svc = ModelSettingsService(_conn())
    svc.save(
        default="chat",
        backends=[
            {"name": "emb", "provider": "ollama", "model": "bge-m3",
             "usage": "embedding"},
            {"name": "chat", "provider": "ollama", "model": "qwen2.5:7b",
             "usage": "chat"},
        ],
    )
    with pytest.raises(ModelSettingsError, match="不在模型页配置里"):
        svc.save_chat_pool(["ghost"])
    # 空序列也不行：那等于把对话彻底关掉，而界面上没有任何地方说得通。
    with pytest.raises(ModelSettingsError, match="不能为空"):
        svc.save_chat_pool([])
    # 已存在的行**可以**被加进对话序列 —— 它原本"用于嵌入"不构成障碍：一行模型服务谁是
    # 服务页的决定，不是行上写死的属性（拆层前的死循环正是 usage 自己定义了自己）。
    svc.save_chat_pool(["chat", "emb"])
    assert svc.list_fallbacks() == ["emb"]


def test_save_prunes_stale_fallbacks_when_backends_shrink() -> None:
    """缩容保存（fallbacks 缺省 = 保留当前值）不得被陈旧链卡死（smoke 12/13 → 13/13）。

    旧链引用的 b 被删除后，"保留当前链"只能修剪为存活子集 —— 运行时
    `resolve_fallbacks` 本就丢弃未知名字，保存时对齐同一语义。
    """
    svc = ModelSettingsService(_conn())
    svc.save(
        default="a",
        backends=[
            {"name": "a", "provider": "ollama", "model": "m-a", "usage": "chat"},
            {"name": "b", "provider": "ollama", "model": "m-b", "usage": "chat"},
        ],
        fallbacks=["b"],
    )
    assert svc.list_fallbacks() == ["b"]
    svc.save(
        default="a",
        backends=[{"name": "a", "provider": "ollama", "model": "m-a", "usage": "chat"}],
    )  # 缩容：b 没了，链缺省保留 → 修剪后为空，保存成功
    assert svc.list_fallbacks() == []


# --------------------------------------------------------------------------- #
# R26-01: 旧整表 PUT 会静默清零"它没写"的那些列
# --------------------------------------------------------------------------- #

def _row_values(conn: object, name: str) -> dict[str, object]:
    row = conn.execute("SELECT * FROM model_backend WHERE name = ?", (name,)).fetchone()  # type: ignore[attr-defined]
    assert row is not None
    return dict(row)  # `sqlite3.Row` 直接 dict() 就是"列名 → 值"


def _backend(svc: ModelSettingsService, name: str) -> dict[str, object]:
    return next(b for b in svc._raw_backends() if str(b["name"]) == name)


def test_save_preserves_the_sampling_penalties_it_does_not_manage() -> None:
    """审计里那条实测复现：`set_sampling` 之后再走一次整表 `save()`，惩罚列不许消失。

    `save()` 写的是 `DELETE FROM model_backend` + 一份手写列清单的 INSERT，所以清单外的列
    过去会被静默清零 —— 而它**不会红**：`num_ctx` 在清单里，活着；只有惩罚三档没了。
    """
    svc = ModelSettingsService(_conn())
    svc.save(
        default="a",
        backends=[{"name": "a", "provider": "ollama", "model": "m-a", "usage": "chat"}],
    )
    svc.set_sampling("a", {"repeat_penalty": 1.25, "frequency_penalty": 0.1})
    assert svc.sampling("a")["repeat_penalty"] == 1.25  # 前置条件：确实设上了

    svc.save(
        default="a",
        backends=[
            {
                "name": "a",
                "provider": "ollama",
                "model": "m-a",
                "usage": "chat",
                "num_ctx": 8192,
            }
        ],
    )
    after = svc.sampling("a")
    assert (after["repeat_penalty"], after["frequency_penalty"]) == (1.25, 0.1), after
    # 同一行里"它管理的"列照常按请求覆盖 —— 保留不是"整行不动"。
    assert _backend(svc, "a")["num_ctx"] == 8192


def test_save_carries_over_any_column_it_does_not_manage() -> None:
    """机制而不是那三个名字：给表加一列全新的、`save()` 不认识的，它也必须原样留着。

    这条是上面那条的**通用面** —— 只补三个列名等于把同一个坑留给下一列（R26-04 抓的是同一族）。
    """
    conn = _conn()
    svc = ModelSettingsService(conn)
    svc.save(
        default="a",
        backends=[{"name": "a", "provider": "ollama", "model": "m-a", "usage": "chat"}],
    )
    conn.execute("ALTER TABLE model_backend ADD COLUMN something_new REAL")  # type: ignore[attr-defined]
    conn.execute(  # type: ignore[attr-defined]
        "UPDATE model_backend SET something_new = 0.75 WHERE name = 'a'"
    )
    conn.commit()  # type: ignore[attr-defined]

    assert "something_new" in unmanaged_backend_columns(conn)
    svc.save(
        default="a",
        backends=[{"name": "a", "provider": "ollama", "model": "m-a", "usage": "chat"}],
    )
    assert _row_values(conn, "a")["something_new"] == 0.75


def test_save_rejects_explicit_unknown_fallback() -> None:
    """显式给的链仍严格校验：指向不存在的后端要大声拒绝（手滑不能静默吞掉）。"""
    svc = ModelSettingsService(_conn())
    with pytest.raises(ModelSettingsError, match="不在已配置的后端列表里"):
        svc.save(
            default="a",
            backends=[{"name": "a", "provider": "ollama", "model": "m-a",
                       "usage": "chat"}],
            fallbacks=["ghost"],
        )


# --------------------------------------------------------------------------- #
# M8: base_url SSRF 校验
# --------------------------------------------------------------------------- #

def test_validate_base_url_rejects_bad_scheme() -> None:
    with pytest.raises(ModelSettingsError, match="必须包含协议"):
        validate_base_url("localhost:11434")
    with pytest.raises(ModelSettingsError, match="仅支持 http/https"):
        validate_base_url("file:///etc/passwd")
    with pytest.raises(ModelSettingsError, match="仅支持 http/https"):
        validate_base_url("gopher://evil")
    with pytest.raises(ModelSettingsError, match="缺少主机名"):
        validate_base_url("http://")


def test_validate_base_url_accepts_http_and_https_and_none() -> None:
    assert validate_base_url(None) is None
    assert validate_base_url("") is None
    assert validate_base_url("http://localhost:11434") == "http://localhost:11434"
    assert validate_base_url("https://api.openai.com/v1") == "https://api.openai.com/v1"


def test_save_rejects_bad_base_url() -> None:
    svc = ModelSettingsService(_conn())
    with pytest.raises(ModelSettingsError, match="仅支持 http/https"):
        svc.save(
            default="local",
            backends=[{
                "name": "local", "provider": "ollama", "model": "qwen2.5:7b",
                "base_url": "file:///etc/passwd", "usage": "chat",
            }],
        )


def test_num_ctx_roundtrip_and_validation() -> None:
    """上下文窗口（num_ctx）：落库/回读/过小拒绝；单列 set_num_ctx 同规则。"""
    svc = ModelSettingsService(_conn())
    svc.save(
        default="a",
        backends=[
            {"name": "a", "provider": "ollama", "model": "qwen3-vl:8b",
             "usage": "chat", "num_ctx": 8192},
        ],
    )
    rows = svc.list_backends()
    assert rows[0]["num_ctx"] == 8192

    # 太小（<512）大声拒绝，不落半套配置
    with pytest.raises(ModelSettingsError, match="不得小于 512"):
        svc.save(
            default="a",
            backends=[{"name": "a", "provider": "ollama", "model": "m", "num_ctx": 128}],
        )

    # 单列写入：正常 / 清除回落 / 未知名称 KeyError
    svc.set_num_ctx("a", 16384)
    assert svc.list_backends()[0]["num_ctx"] == 16384
    svc.set_num_ctx("a", None)
    assert svc.list_backends()[0]["num_ctx"] is None
    with pytest.raises(KeyError):
        svc.set_num_ctx("ghost", 4096)
    with pytest.raises(ModelSettingsError, match="不得小于 512"):
        svc.set_num_ctx("a", 100)


def test_capability_flags_roundtrip_and_defaults() -> None:
    """后端能力位（supports_vision/supports_tools）：显式落库回读；缺省按 vision=0/tools=1。"""
    svc = ModelSettingsService(_conn())
    svc.save(
        default="vl",
        backends=[
            {"name": "vl", "provider": "siliconflow", "model": "Qwen3-VL", "usage": "chat",
             "api_key": "sk-x", "supports_vision": True, "supports_tools": False},
        ],
    )
    row = svc.list_backends()[0]
    assert row["supports_vision"] is True
    assert row["supports_tools"] is False

    # 不传能力位 → 默认（不支持视觉、支持工具），与历史行为一致
    svc.save(
        default="plain",
        backends=[{"name": "plain", "provider": "ollama", "model": "m", "usage": "chat"}],
    )
    row2 = svc.list_backends()[0]
    assert row2["supports_vision"] is False
    assert row2["supports_tools"] is True


# --------------------------------------------------------------------------- #
# 拆层①：一把 key 只有一个家
# --------------------------------------------------------------------------- #

def _silicon(row: dict[str, object]) -> dict[str, object]:
    return {
        "name": str(row["name"]),
        "provider": str(row["provider"]),
        "base_url": row.get("base_url"),
        "model": str(row["model"]),
        "usage": str(row.get("usage", "chat")),
    }


def test_same_endpoint_shares_one_credential_group() -> None:
    """两个模型同一个端点 = 一个组、一把 key（用户："为啥不用供应商和模型名组成一个键"）。"""
    svc = ModelSettingsService(_conn())
    svc.save(
        default="vl",
        backends=[
            {"name": "chat", "provider": "siliconflow", "model": "DeepSeek-V4",
             "api_key": "sk-shared", "usage": "chat"},
            {"name": "vl", "provider": "siliconflow", "usage": "chat",
             "base_url": "https://api.siliconflow.cn/v1", "model": "Qwen3-VL-30B"},
        ],
    )
    groups = svc.list_providers()
    assert len(groups) == 1, "同一端点被拆成两组 = key 又有两个家"
    assert groups[0]["provider"] == "siliconflow"
    assert [m["name"] for m in groups[0]["models"]] == ["chat", "vl"]
    # 两行都"有 key"，因为 key 属于组；掩码也来自组。
    assert {b["has_key"] for b in svc.list_backends()} == {True}
    assert {b["key_masked"] for b in svc.list_backends()} == {groups[0]["key_masked"]}


def test_omitted_key_keeps_the_group_key_for_a_new_model() -> None:
    """在已有供应商下再加一个模型不必重输凭据（这正是拆层要解决的日常动作）。"""
    svc = ModelSettingsService(_conn())
    svc.save(
        default="chat",
        backends=[{"name": "chat", "provider": "siliconflow", "model": "m-a",
                   "api_key": "sk-1", "usage": "chat"}],
    )
    assert svc.has_key_for_endpoint("siliconflow", "https://api.siliconflow.cn/v1")
    svc.save(
        default="chat",
        backends=[
            _silicon({"name": "chat", "provider": "siliconflow",
                      "base_url": "https://api.siliconflow.cn/v1", "model": "m-a"}),
            _silicon({"name": "second", "provider": "siliconflow",
                      "base_url": "https://api.siliconflow.cn/v1", "model": "m-b"}),
        ],
    )
    assert [str(g["api_key"]) for g in svc._provider_rows()] == ["sk-1"]  # noqa: SLF001
    # 空串 = 清除，且一次清掉整组（不会出现"半组模型没 key"的状态）。
    svc.save(
        default="chat",
        backends=[
            {**_silicon({"name": "chat", "provider": "siliconflow",
                         "base_url": "https://api.siliconflow.cn/v1", "model": "m-a"}),
             "api_key": ""},
            _silicon({"name": "second", "provider": "siliconflow",
                      "base_url": "https://api.siliconflow.cn/v1", "model": "m-b"}),
        ],
    )
    assert [str(g["api_key"]) for g in svc._provider_rows()] == ["None"]  # noqa: SLF001


def test_group_disappears_with_its_last_model() -> None:
    """删掉组里最后一个模型 = 这个端点不再存在，key 随组一起消失（不留看不见的凭据）。"""
    svc = ModelSettingsService(_conn())
    svc.save(
        default="chat",
        backends=[
            {"name": "chat", "provider": "siliconflow", "model": "m-a",
             "api_key": "sk-1", "usage": "chat"},
            {"name": "extra", "provider": "siliconflow", "model": "m-b",
             "base_url": "https://api.siliconflow.cn/v1", "usage": "chat"},
        ],
    )
    assert len(svc.list_providers()) == 1
    svc.save(
        default="chat",
        backends=[{"name": "chat", "provider": "ollama", "model": "m-local", "usage": "chat"}],
    )
    assert [g["provider"] for g in svc.list_providers()] == ["ollama"]
    assert svc.list_providers()[0]["has_key"] is False


def test_local_provider_never_stores_a_key() -> None:
    """Ollama 组一律不存 key：历史上测试与种子往里塞过占位串，界面因此谎报"已存凭据"。"""
    svc = ModelSettingsService(_conn())
    svc.save(
        default="local",
        backends=[{"name": "local", "provider": "ollama", "model": "qwen3-vl:8b",
                   "api_key": "ollama", "usage": "chat"}],
    )
    group = svc.list_providers()[0]
    assert group["has_key"] is False and group["key_masked"] is None
    assert group["needs_key"] is False


# --------------------------------------------------------------------------- #
# 拆层②：用途派生自引用行
# --------------------------------------------------------------------------- #

def test_usage_is_derived_from_service_references() -> None:
    """`used_by` 就是 service_endpoint 的引用集；从对话序列里摘掉一行 ≠ 删掉这行配置。"""
    conn = _conn()
    svc = ModelSettingsService(conn)
    svc.save(
        default="a",
        backends=[
            {"name": "a", "provider": "ollama", "model": "m-a", "usage": "chat"},
            {"name": "b", "provider": "ollama", "model": "m-b", "usage": "chat"},
        ],
    )
    assert [b["usage"] for b in svc.list_backends()] == ["chat", "chat"]
    conn.execute(
        "INSERT INTO service_endpoint (category, id, kind, ref_backend, enabled, sort_order, "
        "builtin) VALUES ('embedding', 'b', 'cloud', 'b', 1, 0, 0)"
    )
    conn.commit()
    by_name = {str(b["name"]): b for b in svc.list_backends()}
    assert by_name["b"]["used_by"] == ["chat", "embedding"]

    svc.save_chat_pool(["a"])  # b 退出对话，但仍服务嵌入
    assert svc.default_backend() == "a"
    assert svc.list_fallbacks() == []
    by_name = {str(b["name"]): b for b in svc.list_backends()}
    assert by_name["b"]["usage"] == "embedding"
    assert by_name["b"]["used_by"] == ["embedding"]
    # 关键：行还在，配置没变小 —— 只是"用于对话"这件事没了。
    assert set(by_name) == {"a", "b"}
    assert svc.effective_settings(Settings(model_backends={})).model_backends.keys() == {
        "a",
        "b",
    }


def test_unassigned_model_is_not_mistaken_for_a_chat_backend() -> None:
    """刚加进来、还没被任何服务引用的行 = `unassigned`，不进对话可选列表。"""
    conn = _conn()
    svc = ModelSettingsService(conn)
    conn.execute(
        "INSERT INTO model_provider (id, provider, base_url, api_key, sort_order) "
        "VALUES ('deepseek', 'deepseek', 'https://api.deepseek.com/v1', 'sk-x', 0)"
    )
    conn.execute(
        "INSERT INTO model_backend (name, provider_id, model, sort_order) "
        "VALUES ('fresh', 'deepseek', 'deepseek-chat', 0)"
    )
    conn.commit()
    row = svc.list_backends()[0]
    assert row["usage"] == UNASSIGNED_USAGE
    assert row["used_by"] == []
    # 服务页的模型推理候选按 usage=='chat' 过滤：它不该出现，但配置本身仍然可读。
    assert [b["name"] for b in svc.list_backends() if b["usage"] == "chat"] == []
    assert svc.default_backend() is None


# --------------------------------------------------------------------------- #
# 拆层③：能力位三态（NULL = 没测过，界面要渲染成 `?`）
# --------------------------------------------------------------------------- #

def test_capability_is_a_tri_state_and_survives_an_omitted_save() -> None:
    """省略能力位 = 库里是什么就还是什么（探测结论与"没测过"都不能被无关保存改掉）。"""
    conn = _conn()
    svc = ModelSettingsService(conn)
    svc.save(
        default="vl",
        backends=[{"name": "vl", "provider": "siliconflow", "model": "Qwen3-VL",
                   "api_key": "sk-x", "usage": "chat", "supports_vision": True}],
    )
    assert svc.list_providers()[0]["models"][0]["supports_vision"] is True
    # 另起一行"从没测过"的（NULL）：新添加流程就是这个状态，界面要渲染成 `?`。
    conn.execute(
        "INSERT INTO model_provider (id, provider, base_url, api_key, sort_order) "
        "VALUES ('openai', 'openai', 'https://api.openai.com/v1', 'sk-k', 1)"
    )
    conn.execute(
        "INSERT INTO model_backend (name, provider_id, model, sort_order) "
        "VALUES ('unprobed', 'openai', 'gpt-x', 1)"
    )
    conn.commit()
    models = {m["name"]: m for g in svc.list_providers() for m in g["models"]}
    assert models["unprobed"]["supports_vision"] is None
    assert models["unprobed"]["supports_tools"] is None
    # 运行时那侧必须有确定值：未探测 = 放行（与 P1-2"只拦确定的否"同一条纪律）。
    rows = {str(b["name"]): b for b in svc.list_backends()}
    assert rows["unprobed"]["supports_vision"] is False
    assert rows["unprobed"]["supports_tools"] is True

    svc.save(
        default="vl",
        backends=[
            {"name": "vl", "provider": "siliconflow", "model": "Qwen3-VL-32B",
             "usage": "chat"},
            {"name": "unprobed", "provider": "openai", "model": "gpt-x", "usage": "chat",
             "base_url": "https://api.openai.com/v1"},
        ],
    )
    models = {m["name"]: m for g in svc.list_providers() for m in g["models"]}
    assert models["vl"]["supports_vision"] is True  # 没提交这一列 → 探测结论仍在
    assert models["unprobed"]["supports_vision"] is None  # NULL 也没被悄悄写成"不支持"


# --------------------------------------------------------------------------- #
# 拆层④：搬层无损（旧库 → 两层 + chat 引用）
# --------------------------------------------------------------------------- #

_LEGACY_DDL = """
CREATE TABLE model_provider (
    id TEXT PRIMARY KEY, provider TEXT NOT NULL, label TEXT, base_url TEXT,
    api_key TEXT, sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE service_endpoint (
    category TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'cloud',
    ref_backend TEXT, enabled INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0,
    builtin INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (category, id)
);
CREATE TABLE kernel_meta (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP);
CREATE TABLE model_backend (
    name TEXT PRIMARY KEY, provider TEXT NOT NULL, base_url TEXT, model TEXT NOT NULL,
    api_key TEXT, sort_order INTEGER NOT NULL DEFAULT 0, usage TEXT NOT NULL DEFAULT 'chat',
    num_ctx INTEGER, supports_vision INTEGER NOT NULL DEFAULT 0,
    supports_tools INTEGER NOT NULL DEFAULT 1
);
"""


def test_legacy_db_moves_into_two_layers_without_losing_config() -> None:
    """三行旧数据（两行同一把 key）→ 两个组、一把 key、chat 顺序与默认一个不变。"""
    conn = connect(":memory:")
    conn.executescript(_LEGACY_DDL)
    conn.executescript(
        "INSERT INTO model_backend (name, provider, base_url, model, api_key, sort_order, "
        "usage, num_ctx, supports_vision, supports_tools) VALUES "
        "('vl',  'siliconflow', 'https://api.siliconflow.cn/v1', 'Qwen3-VL', 'sk-shared', 1, "
        " 'chat', NULL, 1, 0),"
        "('chat','siliconflow', 'https://api.siliconflow.cn/v1', 'DeepSeek', 'sk-shared', 0, "
        " 'chat', NULL, 0, 1),"
        "('local','ollama', 'http://localhost:11434', 'qwen3-vl:8b', NULL, 2, 'chat', 8192, 1, 1);"
    )
    conn.execute("INSERT INTO kernel_meta (key, value) VALUES ('model_default', 'local')")
    conn.execute("INSERT INTO kernel_meta (key, value) VALUES ('model_fallbacks', "
                 "'[\"chat\"]')")
    conn.commit()

    assert migrate_to_provider_layers(conn) == 2
    assert migrate_to_provider_layers(conn) == 0  # 幂等：搬过就不再搬

    svc = ModelSettingsService(conn)
    groups = {str(g["id"]): g for g in svc._provider_rows()}  # noqa: SLF001
    assert len(groups) == 2, "同一端点的两行必须归成一个组"
    assert groups["siliconflow"]["api_key"] == "sk-shared"
    assert [b["name"] for b in svc.list_backends()] == ["chat", "vl", "local"]
    # 默认与回退顺序照搬：local 仍是第 1 位，chat 第二（旧链），vl 跟在后面。
    assert svc.default_backend() == "local"
    assert svc.list_fallbacks() == ["chat", "vl"]
    # 旧列的三行用途都在：都是 chat；能力位与 num_ctx 原样跟行。
    by_name = {str(b["name"]): b for b in svc.list_backends()}
    assert by_name["vl"]["supports_vision"] is True and by_name["vl"]["supports_tools"] is False
    assert by_name["local"]["num_ctx"] == 8192
    # kernel_meta 里的第二处默认/回退链被清掉（事实面只剩引用行的顺序）。
    left = conn.execute(
        "SELECT key FROM kernel_meta WHERE key IN ('model_default','model_fallbacks')"
    ).fetchall()
    assert left == []




def test_a_declared_column_without_a_reader_fails_loud(conn: object) -> None:
    """S-1 的那道守卫：加进 `ModelBackend` 却没写读取器 ⇒ 当场抛，不是静默读成 None。

    从前"加一列要动 7 处"里最坏的一处就是读侧：漏了不报错，只是那一列永远是 None，
    症状是"设了但看不见"。现在它变成一条明确的错误。
    """
    from rolecard_agent.config import ModelBackend
    from rolecard_agent.core.model_settings import _value_columns

    bootstrap(conn, enabled_domains=())  # type: ignore[attr-defined]
    conn.execute("ALTER TABLE model_backend ADD COLUMN new_thing REAL")  # type: ignore[attr-defined]
    conn.commit()  # type: ignore[attr-defined]
    ModelBackend.model_fields["new_thing"] = ModelBackend.model_fields["num_ctx"]
    try:
        with pytest.raises(ModelSettingsError, match="没写怎么读它"):
            _value_columns(conn)
    finally:
        ModelBackend.model_fields.pop("new_thing", None)

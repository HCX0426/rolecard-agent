"""Runtime-editable model backend configuration - the data layer behind the settings page.

Why a DB table instead of env-only: `MODEL_BACKENDS` (config.py) is a deploy-time contract.
The settings page needs an OPERATOR-time contract: add a SiliconFlow/OpenAI-compatible
endpoint, set the default, and have the next conversation turn use it WITHOUT restarting.
Env stays the bootstrap truth; the first settings save takes over (see `effective_settings`).

Two rules worth calling out:

  * **API keys are write-only over the wire.** `list_backends` never returns a key (only a
    `has_key` flag) so a browser session can never read secrets back. `save` therefore treats
    a missing/None `api_key` as "keep the stored one for this backend name" and an empty
    string as "clear it" - otherwise every save that did not retype the key would erase it.
  * **Plaintext at rest, stated rather than hidden.** Keys live in the local demo SQLite
    file, which never leaves the machine. Production would move to a secret manager - that
    is a v2 concern, and pretending otherwise in a demo would be worse than the limitation.
"""

from __future__ import annotations

import json
import re
import sqlite3

from rolecard_agent.config import MAX_FALLBACKS, ModelBackend, Settings

# Backend names become keys in MODEL_BACKENDS-merged maps and UI list items: keep them tame.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# 供应商目录：设置页「模型」页签的下拉不再写死前端，改由后端提供（动态扩展）。
# id 是**供应商身份**（界面分组/展示用），style 才是运行时客户端风格：
#   native = Ollama 原生端点（base_url 不带 /v1）；openai = OpenAI 兼容（带 /v1）。
# 两者刻意分离：同一"OpenAI 兼容"风格下有很多厂商（SiliconFlow/DeepSeek/…），把厂商
# 写进 provider 才能让界面正确显示"硅基流动"，而不是一句无意义的"openai"。
MODEL_PROVIDERS: tuple[dict[str, str], ...] = (
    {"id": "ollama", "label": "本地 Ollama", "needs_key": "0",
     "base_url_hint": "http://localhost:11434（可留空）", "style": "native"},
    {"id": "openai", "label": "OpenAI 兼容", "needs_key": "1",
     "base_url_hint": "https://api.openai.com/v1", "style": "openai"},
    {"id": "siliconflow", "label": "硅基流动", "needs_key": "1",
     "base_url_hint": "https://api.siliconflow.cn/v1", "style": "openai"},
    {"id": "deepseek", "label": "DeepSeek", "needs_key": "1",
     "base_url_hint": "https://api.deepseek.com/v1", "style": "openai"},
)

# 历史 alias：旧数据/旧配置里的 "local" 一律视作 ollama（不再作为可选供应商出现）。
PROVIDER_ALIASES = {"local": "ollama"}

KEYLESS_PROVIDERS = frozenset({"ollama", "local"})

# 模型页配置行的合法用途（架构归一化：一行配置服务一种能力，服务页按用途引用）。
BACKEND_USAGES = frozenset({"chat", "embedding", "rerank", "ocr"})


def _canonical(provider: str) -> str:
    p = (provider or "").strip().lower()
    return PROVIDER_ALIASES.get(p, p)


def normalize_provider(provider: str, base_url: str | None = None) -> str:
    """把历史/风格性 provider 值归一到供应商目录 id。

    此前云端种子只记端点风格（SiliconFlow 存成 "openai"），界面因此显示错误的供应商。
    这里按 alias 折叠 + base_url 厂商特征推断；识别不出就原样保留（自定义网关仍算 openai）。
    """
    p = _canonical(provider)
    url = (base_url or "").lower()
    if p in ("", "openai"):
        if "siliconflow" in url:
            return "siliconflow"
        if "deepseek" in url:
            return "deepseek"
    return p or "openai"


def client_style(provider: str) -> str:
    """供应商 id → 运行时客户端风格：native 走 Ollama 原生，其余走 OpenAI 兼容。

    `init_chat_model` 只认 "ollama"/"openai" 两类 provider；目录化之后界面上的
    siliconflow/deepseek 都映射到 openai 兼容客户端（base_url 指向各自厂商）。
    """
    p = _canonical(provider)
    for entry in MODEL_PROVIDERS:
        if entry["id"] == p:
            return entry["style"]
    return "openai"


def is_keyless_provider(provider: str) -> bool:
    """本地类 provider（Ollama 及其别名 local）不需要 api_key。"""
    return (provider or "").strip().lower() in KEYLESS_PROVIDERS


def provider_catalog() -> list[dict[str, str]]:
    """返回供应商目录（前端下拉用）。新增供应商只改这里，无需动前端。"""
    return [dict(p) for p in MODEL_PROVIDERS]


class ModelSettingsError(Exception):
    """A settings write that would produce an unusable configuration. Message is user-safe."""


class ModelSettingsService:
    MODEL_DEFAULT_KEY = "model_default"
    MODEL_SEEDED_KEY = "model_backends_seeded"
    FALLBACKS_KEY = "model_fallbacks"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- reads -----------------------------------------------------------------

    def _raw_backends(self) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT name, provider, base_url, model, api_key, usage, sort_order "
            "FROM model_backend ORDER BY sort_order, name"
        ).fetchall()
        return [dict(r) for r in rows]

    def raw_backends(self) -> list[dict[str, object]]:
        """进程内配置解析用（服务引用行取凭据、工厂实例化）。

        含 api_key 明文 —— 只允许在服务层/工厂内部消费，**绝不**直接进任何 API 响应
        （对外形状见 `list_backends`：只回 has_key + 掩码）。
        """
        return self._raw_backends()

    def list_backends(self) -> list[dict[str, object]]:
        """Public shape: NO api_key ever leaves the service, only `has_key` + a masked preview."""
        return [
            {
                k: row[k]
                for k in ("name", "provider", "base_url", "model", "usage", "sort_order")
            }
            | {"has_key": bool(row["api_key"]), "key_masked": self.key_masked(row["name"])}
            for row in self._raw_backends()
        ]

    def key_masked(self, name: str) -> str | None:
        """回读的**掩码**密钥（如 `sk-…abcd`），仅用于页面"查看已保存密钥"；绝不回明文。"""
        raw = self.stored_api_key(name)
        if not raw:
            return None
        raw = str(raw)
        if len(raw) <= 6:
            return "•" * len(raw)
        return f"{raw[:3]}…{raw[-4:]}"

    def default_backend(self) -> str | None:
        """The operator-chosen default, or None = fall through to env's `model_default`."""
        row = self._conn.execute(
            "SELECT value FROM kernel_meta WHERE key = ?", (self.MODEL_DEFAULT_KEY,)
        ).fetchone()
        value = str(row["value"]) if row and row["value"] else None
        return value or None

    def stored_api_key(self, name: str) -> str | None:
        """已保存的 key（只在本进程内使用，绝不经 API 回传）。"""
        for row in self._raw_backends():
            if str(row["name"]) == name:
                return row["api_key"]  # type: ignore[return-value]
        return None

    def list_fallbacks(self) -> list[str] | None:
        """Operator-configured fallback chain, or None = not configured (use env's)."""
        row = self._conn.execute(
            "SELECT value FROM kernel_meta WHERE key = ?", (self.FALLBACKS_KEY,)
        ).fetchone()
        if row is None or not row["value"]:
            return None
        try:
            return [str(x) for x in json.loads(str(row["value"]))]
        except json.JSONDecodeError:
            return None

    def save_fallbacks(self, fallbacks: list[str], *, allowed_names: set[str]) -> None:
        """Standalone chain write, used by tests and tools that only touch fallbacks.
        Prefer `save(fallbacks=...)` for full-config writes."""
        if len(fallbacks) > MAX_FALLBACKS:
            raise ModelSettingsError(f"回退链最多 {MAX_FALLBACKS} 级（过长只会掩盖降级质量）。")
        if len(set(fallbacks)) != len(fallbacks):
            raise ModelSettingsError("回退链里出现了重复的后端名。")
        for name in fallbacks:
            if name not in allowed_names:
                raise ModelSettingsError(f"回退后端 {name!r} 不在已配置的后端列表里。")
        self._conn.execute(
            "INSERT INTO kernel_meta (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "  updated_at = CURRENT_TIMESTAMP",
            (self.FALLBACKS_KEY, json.dumps(fallbacks)),
        )
        self._conn.commit()

    def seed_from_env(self, env_settings: Settings) -> int:
        """First-boot migration: copy env backends into the table ONCE, then env is out of
        the loop — the settings UI (this table) is the single source of truth afterwards.

        The `model_backends_seeded` flag makes the migration one-way: a backend the operator
        deletes in the UI stays deleted even if env still provides it, and env edits after
        the first boot are deliberately ignored. 迁移是一次性的，这正是"以后都在界面配置"
        的含义。
        """
        flag = self._conn.execute(
            "SELECT value FROM kernel_meta WHERE key = ?", (self.MODEL_SEEDED_KEY,)
        ).fetchone()
        if flag is not None:
            return 0

        existing = {str(r["name"]) for r in self._raw_backends()}
        inserted = 0
        for name, backend in env_settings.model_backends.items():
            if name in existing:
                continue
            self._conn.execute(
                "INSERT OR IGNORE INTO model_backend "
                "(name, provider, base_url, model, api_key, usage, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    backend.provider,
                    backend.base_url,
                    backend.model,
                    backend.api_key,
                    backend.usage,
                    len(existing) + inserted,
                ),
            )
            inserted += 1
        if inserted and self.default_backend() is None:
            self._conn.execute(
                "INSERT OR IGNORE INTO kernel_meta (key, value) VALUES (?, ?)",
                (self.MODEL_DEFAULT_KEY, env_settings.model_default),
            )
        self._conn.execute(
            "INSERT OR IGNORE INTO kernel_meta (key, value) VALUES (?, ?)",
            (self.MODEL_SEEDED_KEY, "1"),
        )
        self._conn.commit()
        return inserted

    def normalize_providers(self) -> int:
        """启动时一次性归一化历史行的 provider，并清掉无 key 供应商误存的 key。

        幂等：归一化后的值再跑一遍不再变化。修复两类历史脏数据 ——
        ① 云端种子把 SiliconFlow 写成 provider="openai"（只记了风格没记厂商），
           设置页因此显示"供应商：openai"这种错误身份；
        ② 无 key 供应商（Ollama）被早期测试写入了无意义的占位 key。
        """
        changed = 0
        for row in self._raw_backends():
            old = str(row["provider"])
            norm = normalize_provider(old, row["base_url"] and str(row["base_url"]))
            drop_key = is_keyless_provider(norm) and bool(row["api_key"])
            if norm != old or drop_key:
                self._conn.execute(
                    "UPDATE model_backend SET provider = ?, api_key = ? WHERE name = ?",
                    (norm, None if drop_key else row["api_key"], str(row["name"])),
                )
                changed += 1
        if changed:
            self._conn.commit()
        return changed

    # -- writes ----------------------------------------------------------------

    def save(
        self,
        *,
        default: str,
        backends: list[dict[str, object]],
        fallbacks: list[str] | None = None,
    ) -> None:
        """Replace the whole backend set in one transaction (the UI edits a list, then saves).

        `api_key` semantics per entry: None/absent = keep the stored key for this name;
        "" = clear; a non-empty string = set. Anything else about the row is replaced.

        `fallbacks` (None = keep current) is the ordered failure chain. It is validated in the
        SAME transaction as the backends, so a config can never be saved with a fallback
        pointing at a backend that does not exist.
        """
        if not backends:
            raise ModelSettingsError("至少需要保留一个模型后端。")
        names: list[str] = []
        prepared: list[tuple[object, ...]] = []
        existing_keys = {str(row["name"]): row["api_key"] for row in self._raw_backends()}
        for i, item in enumerate(backends):
            name = str(item.get("name") or "").strip()
            provider = str(item.get("provider") or "").strip()
            model = str(item.get("model") or "").strip()
            base_url = str(item.get("base_url") or "").strip() or None
            usage = str(item.get("usage") or "chat").strip().lower() or "chat"
            if not _NAME_RE.match(name):
                raise ModelSettingsError(
                    f"后端名 {name!r} 不合法：小写字母开头，只含小写字母/数字/下划线/连字符。"
                )
            if name in names:
                raise ModelSettingsError(f"后端名重复：{name}")
            if not provider:
                raise ModelSettingsError(f"后端 {name} 缺少 provider（从供应商目录选择）。")
            if not model:
                raise ModelSettingsError(f"后端 {name} 缺少模型名。")
            if usage not in BACKEND_USAGES:
                raise ModelSettingsError(
                    f"后端 {name} 的用途 {usage!r} 不合法（chat/embedding/rerank/ocr）。"
                )
            # 写入即归一：目录外的风格值（如历史 "openai"+硅基流动 URL）折叠成厂商 id。
            provider = normalize_provider(provider, base_url)
            names.append(name)

            raw_key = item.get("api_key")
            if raw_key is None:
                key = existing_keys.get(name)  # omitted -> keep whatever is stored
            elif str(raw_key).strip() == "":
                key = None  # explicit clear
            else:
                key = str(raw_key).strip()
            prepared.append((name, provider, base_url, model, key, usage, i))

        if default not in names:
            raise ModelSettingsError(f"默认后端 {default!r} 不在列表里。")

        chain = list(fallbacks) if fallbacks is not None else (self.list_fallbacks() or [])
        if len(chain) > MAX_FALLBACKS:
            raise ModelSettingsError(f"回退链最多 {MAX_FALLBACKS} 级（过长只会掩盖降级质量）。")
        if len(set(chain)) != len(chain):
            raise ModelSettingsError("回退链里出现了重复的后端名。")
        for name in chain:
            if name not in names:
                raise ModelSettingsError(f"回退后端 {name!r} 不在已配置的后端列表里。")

        self._conn.execute("DELETE FROM model_backend")
        self._conn.executemany(
            "INSERT INTO model_backend "
            "(name, provider, base_url, model, api_key, usage, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            prepared,
        )
        for key, value in (
            (self.MODEL_DEFAULT_KEY, default),
            (self.FALLBACKS_KEY, json.dumps(chain)),
        ):
            self._conn.execute(
                "INSERT INTO kernel_meta (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "  updated_at = CURRENT_TIMESTAMP",
                (key, value),
            )
        self._conn.commit()

    # -- merge -------------------------------------------------------------------

    def effective_settings(self, env_settings: Settings) -> Settings:
        """DB rows overlaid on env config. Empty table = env config untouched.

        The built-in `local` backend from env is preserved (a laptop without the settings
        page open still has its Ollama default); DB rows add or override by name, and the
        default falls through to env's when the operator has not picked one. The fallback
        chain follows the same rule: unset in DB = env's list.
        """
        raw = self._raw_backends()
        if not raw:
            return env_settings
        merged = dict(env_settings.model_backends)
        for row in raw:
            merged[str(row["name"])] = ModelBackend(
                model=str(row["model"]),
                base_url=row["base_url"],  # type: ignore[arg-type]
                api_key=row["api_key"],  # type: ignore[arg-type]
                provider=str(row["provider"]),
                usage=str(row["usage"]),
            )
        default = self.default_backend()
        fallbacks = self.list_fallbacks()
        return env_settings.model_copy(
            update={
                "model_backends": merged,
                "model_default": default if default in merged else env_settings.model_default,
                "model_fallbacks": fallbacks
                if fallbacks is not None
                else env_settings.model_fallbacks,
            }
        )

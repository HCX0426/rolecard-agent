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
            "SELECT name, provider, base_url, model, api_key, sort_order "
            "FROM model_backend ORDER BY sort_order, name"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_backends(self) -> list[dict[str, object]]:
        """Public shape: NO api_key ever leaves the service, only `has_key`."""
        return [
            {k: row[k] for k in ("name", "provider", "base_url", "model", "sort_order")}
            | {"has_key": bool(row["api_key"])}
            for row in self._raw_backends()
        ]

    def default_backend(self) -> str | None:
        """The operator-chosen default, or None = fall through to env's `model_default`."""
        row = self._conn.execute(
            "SELECT value FROM kernel_meta WHERE key = ?", (self.MODEL_DEFAULT_KEY,)
        ).fetchone()
        value = str(row["value"]) if row and row["value"] else None
        return value or None

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
                "(name, provider, base_url, model, api_key, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    name,
                    backend.provider,
                    backend.base_url,
                    backend.model,
                    backend.api_key,
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
            if not _NAME_RE.match(name):
                raise ModelSettingsError(
                    f"后端名 {name!r} 不合法：小写字母开头，只含小写字母/数字/下划线/连字符。"
                )
            if name in names:
                raise ModelSettingsError(f"后端名重复：{name}")
            if not provider:
                raise ModelSettingsError(f"后端 {name} 缺少 provider（如 ollama / openai）。")
            if not model:
                raise ModelSettingsError(f"后端 {name} 缺少模型名。")
            names.append(name)

            raw_key = item.get("api_key")
            if raw_key is None:
                key = existing_keys.get(name)  # omitted -> keep whatever is stored
            elif str(raw_key).strip() == "":
                key = None  # explicit clear
            else:
                key = str(raw_key).strip()
            prepared.append((name, provider, base_url, model, key, i))

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
            "INSERT INTO model_backend (name, provider, base_url, model, api_key, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
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

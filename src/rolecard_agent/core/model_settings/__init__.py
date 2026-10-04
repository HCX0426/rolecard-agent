"""模型配置包：把 1624 行的 `model_settings.py` 按职责拆开（2026-10-04 审查快照 P1-6）。

分工（调用方只认本文件导出的那一份名字，导出面与拆分前**逐字相同**）：

  * `rules`     —— 常量与规则：provider 目录 / 别名 / 无 key 厂商 / 用途枚举 / URL 校验 / 掩码；
  * `migration` —— 搬层迁移（旧形态 → 两层 + chat 引用行），已注册进 `core/migrations.py`
                   的 `shape.model_backend_provider_layers` 步骤；
  * `rows`      —— 行与列的事实面：哪些列归本服务管、后端行怎么从 SQL 行解析出来；
  * `read`      —— 读侧与清单（`list_backends` / `list_providers` / 默认 / 凭据探测 / 播种）；
  * `save`      —— 写入口（`save` / `group_for` / `add_model`）；
  * `write`     —— 逐项写（采样 / 上下文 / 删除 / 能力位）与卫生清扫；
  * `effective` —— 配置合成（`effective_settings`，运行期"这份配置是谁的"的唯一咽喉）；
  * `service`   —— 把上面四个 mixin 装成 `ModelSettingsService`。

包内只许**单向**依赖：`service` → 各 mixin → `rows` / `rules`；`migration` 只依赖 `rules`。
"""

from __future__ import annotations

from rolecard_agent.core.model_settings.migration import (
    endpoint_key,
    migrate_to_provider_layers,
)
from rolecard_agent.core.model_settings.rows import (
    SAVE_MANAGED_COLUMNS,
    _backend_from_row,
    _capability_of,
    _derived_usage,
    _kind_of_names,
    _opt_float,
    _opt_int,
    _sorted_usages,
    _tools_of,
    _tri_state,
    _value_columns,
    _vision_of,
    declared_model_names,
    unmanaged_backend_columns,
)
from rolecard_agent.core.model_settings.rules import (
    BACKEND_USAGES,
    CHAT_CATEGORY,
    KEYLESS_PROVIDERS,
    MODEL_PROVIDERS,
    PROVIDER_ALIASES,
    UNASSIGNED_USAGE,
    USAGE_DISPLAY_ORDER,
    ModelSettingsError,
    client_style,
    is_keyless_provider,
    mask_key,
    normalize_provider,
    provider_catalog,
    provider_label,
    validate_base_url,
)
from rolecard_agent.core.model_settings.service import ModelSettingsService

__all__ = [
    "BACKEND_USAGES",
    "CHAT_CATEGORY",
    "KEYLESS_PROVIDERS",
    "MODEL_PROVIDERS",
    "ModelSettingsError",
    "ModelSettingsService",
    "PROVIDER_ALIASES",
    "SAVE_MANAGED_COLUMNS",
    "UNASSIGNED_USAGE",
    "USAGE_DISPLAY_ORDER",
    "_backend_from_row",
    "_capability_of",
    "_derived_usage",
    "_kind_of_names",
    "_opt_float",
    "_opt_int",
    "_sorted_usages",
    "_tools_of",
    "_tri_state",
    "_value_columns",
    "_vision_of",
    "client_style",
    "declared_model_names",
    "endpoint_key",
    "is_keyless_provider",
    "mask_key",
    "migrate_to_provider_layers",
    "normalize_provider",
    "provider_catalog",
    "provider_label",
    "unmanaged_backend_columns",
    "validate_base_url",
]

from __future__ import annotations

from rolecard_agent.config import MAX_FALLBACKS
from rolecard_agent.core.model_settings.migration import _dedupe, _group_id, endpoint_key
from rolecard_agent.core.model_settings.rows import (
    _OWNED_MODEL_ROWS,
    _capability_of,
    _PreparedBackend,
    unmanaged_backend_columns,
)
from rolecard_agent.core.model_settings.rules import (
    _NAME_RE,
    BACKEND_USAGES,
    ModelSettingsError,
    is_keyless_provider,
    normalize_provider,
    validate_base_url,
)
from rolecard_agent.core.model_settings.write import _WriteMixin
from rolecard_agent.storage.db import quote_ident


class _SaveMixin(_WriteMixin):
    def save(
        self,
        *,
        user_id: str,
        default: str,
        backends: list[dict[str, object]],
        fallbacks: list[str] | None = None,
    ) -> None:
        """Replace the whole backend set in one transaction（过渡期：旧模型页的整表保存）。

        **`user_id` 是"整表"的范围**：这里的"全量替换"替换的是**这个人**的那一集，不是库里的
        全部。旧语义在没有归属列之前是同一件事（整个库里只有一族配置），有了主人之后它就成了
        最危险的一处 —— 不加过滤，A 存一次盘就把 B 的模型行与 key 抹了（`DELETE FROM
        model_backend` 无 WHERE + 末尾那句按 id 列表删组，都是全表）。

        入参仍是**旧形状**：每行带 provider/base_url/api_key/usage。内部按 (供应商, base_url)
        归并成凭据组，模型行只留模型名 + 能力位 + num_ctx；`usage='chat'` 翻译成 chat 引用行
        （第 1 位 = `default`，其后 = `fallbacks`）。旧界面下线后本方法随
        `PUT /api/settings/models` 一起退役，新界面走逐条增删改的端点。

        `api_key` 语义（组内聚合后落到组上）：任一行给了非空串 = 设成它；给了空串且无人给
        新值 = 清除；所有行都省略 = 保留组里已存的。无 key 供应商（Ollama）一律不存 key。
        """
        if not backends:
            raise ModelSettingsError("至少需要保留一个模型。")
        stored_rows = {str(row["name"]): row for row in self._raw_backends(user_id=user_id)}
        existing_groups = {str(g["id"]): g for g in self._provider_rows(user_id=user_id)}
        stored_gid_of_endpoint = {
            endpoint_key(str(g["provider"]), g["base_url"]): str(g["id"])
            for g in existing_groups.values()
        }
        names: list[str] = []
        usage_by_name: dict[str, str] = {}
        prepared: list[_PreparedBackend] = []
        for i, item in enumerate(backends):
            name = str(item.get("name") or "").strip()
            provider = str(item.get("provider") or "").strip()
            model = str(item.get("model") or "").strip()
            usage = str(item.get("usage") or "chat").strip().lower() or "chat"
            if not _NAME_RE.match(name):
                raise ModelSettingsError(
                    f"模型名 {name!r} 不合法：小写字母开头，只含小写字母/数字/下划线/连字符。"
                )
            if name in names:
                raise ModelSettingsError(f"模型名重复：{name}")
            if not provider:
                raise ModelSettingsError(f"模型 {name} 缺少 provider（从供应商目录选择）。")
            if not model:
                raise ModelSettingsError(f"模型 {name} 还没有填模型名。")
            if usage not in BACKEND_USAGES:
                raise ModelSettingsError(
                    f"模型 {name} 的用途 {usage!r} 不合法（chat/embedding/rerank/ocr）。"
                )
            # num_ctx（本地 Ollama 上下文窗口）：None 允许；给了必须是不小于 512 的整数
            # ——太小的窗口等于把历史截没，宁可大声拒绝。
            raw_ctx = item.get("num_ctx")
            num_ctx: int | None = None
            if raw_ctx not in (None, ""):
                try:
                    num_ctx = int(str(raw_ctx))
                except (TypeError, ValueError) as exc:
                    raise ModelSettingsError(
                        f"模型 {name} 的 num_ctx 必须是整数（tokens）"
                    ) from exc
                if num_ctx < 512:
                    raise ModelSettingsError(f"模型 {name} 的 num_ctx 不得小于 512（tokens）")
            raw_base = item.get("base_url")
            # 写入即归一：目录外的风格值（如历史 "openai"+硅基流动 URL）折叠成厂商 id。
            provider = normalize_provider(provider, str(raw_base) if raw_base else None)
            # M8：base_url 落库前校验 scheme/host，拒绝异常协议与裸 host（允许 localhost/私网）。
            base_url = validate_base_url(str(raw_base) if raw_base else None)
            names.append(name)
            usage_by_name[name] = usage
            raw_key = item.get("api_key")
            prepared.append(
                _PreparedBackend(
                    name=name,
                    endpoint=endpoint_key(provider, base_url),
                    model=model,
                    # 三态：省略/None = 这行没给（保留组里已存的）；空串 = 清除；非空 = 设值。
                    api_key=None if raw_key is None else str(raw_key).strip(),
                    sort_order=i,
                    num_ctx=num_ctx,
                    # 能力位三态：省略 = 保留库里已存的（包括"没测过"）；显式给了才写。
                    supports_vision=_capability_of(
                        item, "supports_vision", stored_rows.get(name), default=False
                    ),
                    supports_tools=_capability_of(
                        item, "supports_tools", stored_rows.get(name), default=True
                    ),
                )
            )

        if default not in names:
            raise ModelSettingsError(f"默认模型 {default!r} 不在列表里。")
        # M2：对话默认后端必须是 chat 用途（对话/抽取）；embedding/rerank/ocr 不能当默认。
        if usage_by_name.get(default) != "chat":
            raise ModelSettingsError(
                f"默认模型 {default!r} 必须是 chat 用途（对话/抽取），"
                f"不能是 {usage_by_name.get(default)}。"
            )

        # 回退链：显式给链 → 原样校验（操作员手滑必须大声拒绝）；缺省（=保留当前值）→
        # **修剪掉引用已删后端的项** —— 后端集缩小时旧链可能指向已删行，此时拒绝会让
        # 一次普通的缩容保存永远卡死；运行时 `resolve_fallbacks` 本就丢弃未知名字，
        # 保存时对齐这一语义（smoke：缩容保存 200，链被清空）。
        kept = self.list_fallbacks(user_id=user_id) or []
        chain = list(fallbacks) if fallbacks is not None else [n for n in kept if n in names]
        if len(chain) > MAX_FALLBACKS:
            raise ModelSettingsError(f"回退链最多 {MAX_FALLBACKS} 级（过长只会掩盖降级质量）。")
        if len(set(chain)) != len(chain):
            raise ModelSettingsError("回退链里出现了重复的模型名。")
        for name in chain:
            if name not in names:
                raise ModelSettingsError(f"回退用的模型 {name!r} 不在已配置的模型列表里。")
            if usage_by_name[name] != "chat":
                raise ModelSettingsError(
                    f"回退用的模型 {name!r} 不是 chat 用途（对话/抽取），不能进回退链。"
                )

        # 组 key：先按端点聚合各行信号（给了新值 > 显式清除 > 保留已存的）。
        orders: dict[tuple[str, str | None], int] = {}
        signals: dict[tuple[str, str | None], str] = {}
        keyless: dict[tuple[str, str | None], bool] = {}
        for row in prepared:
            endpoint = row.endpoint
            orders.setdefault(endpoint, row.sort_order)
            keyless[endpoint] = is_keyless_provider(endpoint[0])
            if keyless[endpoint] or row.api_key is None:
                continue
            if row.api_key:
                signals[endpoint] = row.api_key
            else:
                signals.setdefault(endpoint, "")

        # 删之前先按后端名留住"这个端点不管理"的那些列（见 `SAVE_MANAGED_COLUMNS`）。
        # 快照与删除同范围（都只碰本人的行）：全表快照会把别人的行读进来，而全表删除会
        # 把别人的行删掉 —— 两边不一致时，症状是"我保存一次，他的配置变小了"。
        carried = unmanaged_backend_columns(self._conn)
        carried_values = {
            str(r["name"]): {c: r[c] for c in carried}
            for r in self._conn.execute(
                "SELECT b.* FROM model_backend b JOIN model_provider p ON p.id = b.provider_id"
                " WHERE p.user_id = ?",
                (user_id,),
            )
        }
        self._conn.execute(f"DELETE FROM model_backend WHERE {_OWNED_MODEL_ROWS}", (user_id,))
        used_ids: set[str] = set()
        gid_of_endpoint: dict[tuple[str, str | None], str] = {}
        for endpoint, order in orders.items():
            stored_gid = stored_gid_of_endpoint.get(endpoint)
            if keyless[endpoint]:
                api_key = None  # 本地类供应商不存 key（历史脏数据也在这次保存里被清掉）
            elif endpoint in signals:
                api_key = signals[endpoint] or None
            elif stored_gid is not None:
                api_key = str(existing_groups[stored_gid]["api_key"] or "") or None
            else:
                api_key = None
            if stored_gid is not None:
                gid = stored_gid
                self._conn.execute(
                    "UPDATE model_provider SET base_url = ?, api_key = ?, sort_order = ? "
                    "WHERE id = ? AND user_id = ?",
                    (endpoint[1], api_key, order, gid, user_id),
                )
            else:
                # `taken` 是**全局**的（主键是全局的，见 `_all_group_ids`）；而 gid 一旦发就
                # 只写进本人名下。同一个 (供应商, 端点) 被两个人各配一次 = 两组各带一把 key，
                # 而不是共享 A 的那一把 —— 这正是"key 跟人走"要的形状。
                gid = _group_id(self._all_group_ids() | used_ids, endpoint[0])
                self._conn.execute(
                    "INSERT INTO model_provider "
                    "(id, user_id, provider, base_url, api_key, sort_order) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (gid, user_id, endpoint[0], endpoint[1], api_key, order),
                )
            gid_of_endpoint[endpoint] = gid
            used_ids.add(gid)
        insert_cols = [
            "name",
            "provider_id",
            "model",
            "sort_order",
            "num_ctx",
            "supports_vision",
            "supports_tools",
            *carried,
        ]
        insert_sql = (
            f"INSERT INTO model_backend ({', '.join(quote_ident(c) for c in insert_cols)}) "
            f"VALUES ({', '.join('?' * len(insert_cols))})"
        )
        for row in prepared:
            keep = carried_values.get(row.name, {})
            self._conn.execute(
                insert_sql,
                (
                    row.name,
                    gid_of_endpoint[row.endpoint],
                    row.model,
                    row.sort_order,
                    row.num_ctx,
                    row.supports_vision,
                    row.supports_tools,
                    *(keep.get(c) for c in carried),
                ),
            )
        # 组里最后一个模型被删掉 = 这个端点不再存在。key 随组一起消失，不是"留着备用"：
        # 界面上已经没有它，留在盘上就是一处看不见的凭据。
        # `user_id = ?` 是这一句的范围：`used_ids` 只装了本次涉及的组，不加过滤就是
        # "A 存一次盘，把 B 的凭据组全删了"（这是本方法最要命的那一条，也是它进验收用例的原因）。
        self._conn.execute(
            f"DELETE FROM model_provider WHERE user_id = ? "
            f"AND id NOT IN ({','.join('?' * len(used_ids))})",
            (user_id, *used_ids),
        )
        # 用途（chat 引用行）：默认永远第 1 位，其后依次是回退链，再后面是其余对话后端。
        chat_names = [n for n in names if usage_by_name[n] == "chat"]
        self._write_chat_refs(
            _dedupe([default, *chain, *chat_names], set(chat_names)), user_id=user_id
        )
        self._conn.commit()

    # -- 逐条写入（新模型页的添加抽屉 / 删除 / 探测写回） -----------------------------

    def group_for(
        self,
        *,
        user_id: str,
        group_id: str | None = None,
        provider: str | None = None,
        base_url: str | None = None,
    ) -> dict[str, object] | None:
        """按组 id 或按 (供应商, 端点) 找那条凭据组（含 api_key 明文，**只在进程内用**）。

        端点走 `endpoint_key` 归一，所以"没填 URL 的硅基流动"能命中"填了默认 URL 的那一组"。
        只在这个人的组里找 —— 别人的组在这里就是不存在（探测/添加因此花不到他的 key）。
        """
        for group in self._provider_rows(user_id=user_id):
            if group_id is not None:
                if str(group["id"]) == group_id:
                    return group
            elif provider is not None and endpoint_key(provider, base_url) == endpoint_key(
                str(group["provider"]), group["base_url"]
            ):
                return group
        return None

    def add_model(
        self,
        *,
        user_id: str,
        model: str,
        provider: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        group_id: str | None = None,
        name: str | None = None,
        num_ctx: int | None = None,
        supports_vision: bool | None = None,
        supports_tools: bool | None = None,
    ) -> dict[str, str]:
        """加一行模型（添加抽屉测连通过后走这里）；组不存在就顺手建。

        与整表 `save()` 的区别是它**只动这一行**：不重排别的行、不覆写回退链、不要求前端
        持有全部配置（写放大与并发互相覆盖都少了）。新行进对话序列的尾部（能加进来就是要
        能聊），序列本身仍是"哪些模型用于对话"的唯一事实面。

        `name` 是这行的身份键（session/角色卡引用它）。缺省时由 (供应商, 模型名) 生成一个
        可读的短键，冲突就加后缀 —— 让用户少填一格，同时名字仍然说得出它是谁。
        返回 `{"name":…, "provider_id":…}`。
        """
        model = (model or "").strip()
        if not model:
            raise ModelSettingsError("缺少模型名。")
        group = self.group_for(user_id=user_id, group_id=group_id) if group_id else None
        if group is None and provider:
            group = self.group_for(user_id=user_id, provider=provider, base_url=base_url)
        if group is None:
            if not provider:
                raise ModelSettingsError("要么选一个已配置的供应商，要么填 provider。")
            catalog = normalize_provider(provider, base_url)
            pinned = validate_base_url(base_url)
            if is_keyless_provider(catalog):
                api_key = None
            gid = _group_id(self._all_group_ids(), catalog)
            self._conn.execute(
                "INSERT INTO model_provider "
                "(id, user_id, provider, base_url, api_key, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    gid,
                    user_id,
                    catalog,
                    pinned,
                    api_key.strip() or None if api_key else None,
                    len(self._provider_rows(user_id=user_id)),
                ),
            )
            group = {"id": gid, "provider": catalog, "base_url": pinned, "api_key": api_key}
        gid = str(group["id"])
        if name:
            key = name.strip()
            if not _NAME_RE.match(key):
                raise ModelSettingsError(
                    f"模型名 {key!r} 不合法：小写字母开头，只含小写字母/数字/下划线/连字符。"
                )
            # 冲突判定是**全局**的，不是按人的：`model_backend.name` 是全局主键，按人过滤
            # 只会把"撞主键"变成一个 500。这里的取舍是"宁可报一次占用，也不静默改名"
            # （改名会让用户下次找不到自己那行）。
            if self._conn.execute("SELECT 1 FROM model_backend WHERE name = ?", (key,)).fetchone():
                raise ModelSettingsError(
                    f"这个配置名已经存在：{key}（换一个，或直接编辑原来那行）。"
                )
        else:
            key = self._free_name(gid, str(group["provider"]), model)
        # 能力位：探测结果原样写（None 保持"没测过"），不让一次添加把未知说成已知。
        order = len(self._raw_backends(user_id=user_id))
        self._conn.execute(
            "INSERT INTO model_backend "
            "(name, provider_id, model, sort_order, num_ctx, supports_vision, supports_tools) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                gid,
                model,
                order,
                num_ctx,
                None if supports_vision is None else int(supports_vision),
                None if supports_tools is None else int(supports_tools),
            ),
        )
        # 新行默认进对话序列的尾部（拆层前的行为：加一个模型就是为了能跟它说话）。
        # 不想让它参与对话 → 在「服务」页的模型推理序列里把它摘掉；只服务嵌入的那行
        # 也是在那里加回来（批次③ 补这个入口）。
        self._write_chat_refs([*self._chat_ref_names(user_id=user_id), key], user_id=user_id)
        self._conn.commit()
        return {"name": key, "provider_id": gid}

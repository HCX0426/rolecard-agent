"""模型凭据层的归属（M2d，§4.1「key 跟人走」）。

要买的东西：**一把 api_key 只有一个人能花。** 这一层以前整库只有一族配置，所以
"谁的"这个问题根本没有答案 —— 而现在 `model_provider.user_id` 有了，读写的每一道
口都必须交出主人。三条最硬的坏法各有用例钉着：

  * 整表保存（旧 `PUT /api/settings/models` 的原语）从前是 `DELETE FROM model_backend`
    无 WHERE + 末尾按 id 列表删组 —— 加了归属之后如果这两句不带范围，A 存一次盘就把
    B 的模型行与 key 抹了，而且界面上一切正常；
  * `effective_settings` 是"这份进程配置里有谁的凭据"的唯一咽喉。别人的组进不了那份
    快照，运行时就**不存在**"要不要检查这把 key 是不是他的"这回事（没有第二道判断要写，
    也就没有漏写那一道的可能）；
  * 按名改一行的那几个 PATCH：名字是可枚举的短串，别人的行必须回 404 而不是 403，
    更不能真的被改掉。

`model_backend` 刻意**不挂** `user_id`：模型行的主人从所属的组继承（最后一用例钉住它，
理由是"多存一份就是第二个事实面"）。
"""

from __future__ import annotations

import sqlite3

import pytest

from rolecard_agent.config import ModelBackend, Settings
from rolecard_agent.core.model_probe import resolve_target
from rolecard_agent.core.model_settings import (
    ModelSettingsError,
    ModelSettingsService,
)
from rolecard_agent.storage.db import bootstrap, connect

A = "local-user"  # 这台实例默认那份
B = "u2"  # 第二个身份（`model_provider` 无外键，所以不必真有 app_user 行）
KEY_A = "sk-aaaaaaaaaaaaaaaa"
KEY_B = "sk-bbbbbbbbbbbbbbbbbb"


def _conn() -> sqlite3.Connection:
    conn = connect(":memory:")
    bootstrap(conn, enabled_domains=())
    return conn


def _backend_row(name: str, model: str, key: str) -> dict[str, object]:
    return {
        "name": name,
        "provider": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": model,
        "api_key": key,
        "usage": "chat",
    }


@pytest.fixture
def svc() -> ModelSettingsService:
    """两个人各自的硅基流动组：同名端点、两把不同的 key。"""
    conn = _conn()
    service = ModelSettingsService(conn)
    service.save(
        user_id=A,
        default="a-chat",
        backends=[_backend_row("a-chat", "Qwen3-8B", KEY_A)],
    )
    service.save(
        user_id=B,
        default="b-chat",
        backends=[_backend_row("b-chat", "Qwen3-32B", KEY_B)],
    )
    return service


# -- 1：看得见谁、看不见谁 ----------------------------------------------------------


def test_each_identity_sees_only_its_own_credential_groups(
    svc: ModelSettingsService,
) -> None:
    a = svc.list_providers(user_id=A)
    b = svc.list_providers(user_id=B)
    assert [str(g["label"] or g["provider"]) for g in a] and len(a) == 1
    assert len(b) == 1
    assert a[0]["id"] != b[0]["id"], "同端点的两组必须各是一个组，不是共享 A 的那一把 key"
    # 掩码也不给：`key_masked` 露出的头尾足以认出是"哪一把"。
    assert str(a[0]["key_masked"]) != str(b[0]["key_masked"])


def test_the_backend_view_a_person_gets_carries_only_their_rows(
    svc: ModelSettingsService,
) -> None:
    assert [str(r["name"]) for r in svc.list_backends(user_id=A)] == ["a-chat"]
    assert [str(r["name"]) for r in svc.list_backends(user_id=B)] == ["b-chat"]


# -- 2：花不到别人的 key（这一条是整步的底线）---------------------------------------


def test_effective_settings_never_carries_another_identitys_key(
    svc: ModelSettingsService,
) -> None:
    env = Settings(model_backends={"env-only": ModelBackend(model="m")}, model_default="env-only")
    eff_a = svc.effective_settings(env, user_id=A)
    names = set(eff_a.model_backends)
    assert "b-chat" not in names and "a-chat" in names
    assert KEY_B not in repr(eff_a.model_backends), "别人的 key 一个字都不该出现在这份快照里"
    assert eff_a.backend("a-chat").api_key == KEY_A
    # 这台实例从此不知道 b-chat 的存在：拿它去构造模型会大声失败，而不是悄悄花 B 的 key。
    with pytest.raises(KeyError):  # Settings.backend 对不存在的名字是大声失败
        eff_a.backend("b-chat")


def test_probe_target_cannot_address_another_identitys_group(
    svc: ModelSettingsService,
) -> None:
    """探测是"让服务器替他发一次真请求"，所以它比读列表更要紧：花的是真金白银的配额。"""
    foreign = str(svc.list_providers(user_id=B)[0]["id"])
    with pytest.raises(ModelSettingsError, match="不存在"):
        resolve_target(svc, user_id=A, provider_id=foreign)
    # 同一 (供应商, 端点) 换个身份来问"有没有 key"：答案必须是"没有"，他要自己填。
    assert svc.has_key_for_endpoint("siliconflow", None, user_id=A) is True
    assert svc.has_key_for_endpoint("siliconflow", None, user_id=B) is True
    assert svc.has_key_for_endpoint("siliconflow", None, user_id="u3") is False


# -- 3：整表保存的范围（最狠的一条）--------------------------------------------------


def test_whole_table_save_does_not_touch_another_identitys_rows(
    svc: ModelSettingsService,
) -> None:
    """A 把自己那一集整个换掉之后，B 的组、key、模型行一个都不能少。

    这是本步最容易写错的一处：`save()` 的语义本来就是"全量替换"，而"全量"的范围在加了
    归属列之后必须从"库里的全部"变成"这个人的全部"。
    """
    svc.save(
        user_id=A,
        default="a-new",
        backends=[_backend_row("a-new", "Qwen3-14B", "sk-aaaaaaaaaaaaaaaa")],
    )
    assert [str(r["name"]) for r in svc.list_backends(user_id=A)] == ["a-new"]
    left = svc.list_backends(user_id=B)
    assert [str(r["name"]) for r in left] == ["b-chat"], "A 的一次保存把 B 的行删了"
    assert svc.stored_api_key("b-chat", user_id=B) == KEY_B


def test_a_new_group_for_one_identity_does_not_reuse_anothers_key(
    svc: ModelSettingsService,
) -> None:
    """同一个端点，第二个身份没填 key 就是没有 key —— 不会白捡第一个人的那把。"""
    added = svc.add_model(user_id="u3", model="DeepSeek-V3", provider="siliconflow")
    assert svc.stored_group_key(added["provider_id"], user_id="u3") is None
    assert svc.stored_api_key(added["name"], user_id="u3") is None
    # 组键不撞（全局主键）：`siliconflow` 与 `siliconflow-2` 已被占，第三个拿到下一个后缀。
    ids = {
        str(g["id"])
        for g in (svc.list_providers(user_id=A) + svc.list_providers(user_id=B)
                  + svc.list_providers(user_id="u3"))
    }
    assert len(ids) == 3, ids


# -- 4：按名改一行的四道口子全部 404 -------------------------------------------------


def test_renaming_or_editing_a_foreign_row_is_a_404_not_an_edit(
    svc: ModelSettingsService,
) -> None:
    with pytest.raises(KeyError):
        svc.set_num_ctx("b-chat", 8192, user_id=A)
    with pytest.raises(KeyError):
        svc.set_sampling("b-chat", {"frequency_penalty": 0.5}, user_id=A)
    with pytest.raises(KeyError):
        svc.set_capabilities("b-chat", {"supports_vision": True}, user_id=A)
    with pytest.raises(KeyError):
        svc.remove_model("b-chat", user_id=A)
    assert svc.sampling("b-chat", user_id=B) == {
        "repeat_penalty": None, "frequency_penalty": None, "presence_penalty": None,
    }
    conn = svc._conn  # noqa: SLF001 - 断言的是"库里真的没被改"
    assert conn.execute(
        "SELECT num_ctx FROM model_backend WHERE name = 'b-chat'"
    ).fetchone()[0] is None
    assert conn.execute("SELECT 1 FROM model_backend WHERE name = 'b-chat'").fetchone()


def test_reading_back_another_identitys_sampling_is_not_a_read(
    svc: ModelSettingsService,
) -> None:
    """回显也按主人读：`sampling` 不带过滤就成了"按名猜别人的设置"。"""
    svc.set_sampling("b-chat", {"frequency_penalty": 0.7}, user_id=B)
    with pytest.raises(KeyError):
        svc.sampling("b-chat", user_id=A)
    assert svc.sampling("b-chat", user_id=B)["frequency_penalty"] == 0.7


# -- 5：两把钥匙各自独立（删除只清自己的）--------------------------------------------


def test_removing_the_last_model_only_evicts_that_persons_group(
    svc: ModelSettingsService,
) -> None:
    gid_a = str(svc.list_providers(user_id=A)[0]["id"])
    svc.remove_model("a-chat", user_id=A)
    assert svc.list_providers(user_id=A) == []
    assert str(svc.list_providers(user_id=B)[0]["id"]) != gid_a
    assert svc.stored_api_key("b-chat", user_id=B) == KEY_B


# -- 6：主键是全局的，所以这里"不分身份"是对的 --------------------------------------


def test_start_up_repair_reaches_every_identity(svc: ModelSettingsService) -> None:
    """`normalize_providers` 是数据卫生清扫，不改归属，所以它跨身份才是对的。

    按主人过滤会漏：库里躺着第二个身份的脏 provider 时，他下次看见自己的供应商名仍然错。
    """
    conn = svc._conn  # noqa: SLF001
    # 历史脏形状：无 key 供应商被写成别名 `local` 且揣了一把占位 key。
    conn.execute("INSERT INTO model_provider (id, user_id, provider, base_url, api_key) "
                 "VALUES ('ollama-b', ?, 'local', 'http://localhost:11434', 'stale')", (B,))
    conn.commit()
    assert svc.normalize_providers() == 1
    rows = {
        str(r["id"]): (str(r["provider"]), r["api_key"])
        for r in conn.execute("SELECT id, provider, api_key FROM model_provider")
    }
    assert rows["ollama-b"] == ("ollama", None), "第二个人的占位 key 也要被清掉"


def test_model_backend_deliberately_has_no_owner_column() -> None:
    """`model_backend` 没有 `user_id` 是**设计**，不是漏（与那几张"主人跟着父行走"的表同理）。

    多存一份就是第二个事实面：一行模型属于谁，答案在它所属的组里；两处不一致时没有仲裁者。
    """
    conn = _conn()
    cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(model_backend)")}
    assert "user_id" not in cols, cols
    assert {"name", "provider_id", "model"} <= cols


def test_seed_from_env_stamps_the_instance_owner(
    svc: ModelSettingsService,
) -> None:
    """env 里的 key 是"这个进程带着的凭据"，所以种子落在本机主人名下。"""
    conn = _conn()
    env = Settings(
        model_backends={
            "cloud": ModelBackend(
                provider="siliconflow", model="M", base_url="https://api.siliconflow.cn/v1",
                api_key="sk-from-env",
            ),
        },
        model_default="cloud",
        model_fallbacks=[],
    )
    assert ModelSettingsService(conn).seed_from_env(env, user_id=B) == 1
    assert conn.execute(
        "SELECT user_id FROM model_provider WHERE base_url LIKE '%siliconflow%'"
    ).fetchone()[0] == B
    assert ModelSettingsService(conn).stored_api_key("cloud", user_id=B) == "sk-from-env"
    # A 看不见这次播种：env 的 key 不会因为"同一个进程"就变成他的可用凭据。
    assert ModelSettingsService(conn).list_backends(user_id=A) == []

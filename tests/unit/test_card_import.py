"""ST 角色卡导入器（`features/card_import.py` + `POST /api/roles/import`）的单元测试。

钉住：两条载体（PNG 内嵌 chara / 裸 JSON）、V1/V2 两代形状、字段映射的落点与对账单
（超预算的范例整条丢并计数、世界书/标签不静默）、role_id 的 slug 规则、端点的
400/409/201 三态。
"""

from __future__ import annotations

import base64
import json
import re
import zlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rolecard_agent.api.main import create_app
from rolecard_agent.features.card_import import (
    CardImportError,
    load_card_bytes,
    parse_sillytavern_card,
)

V1_CARD: dict[str, Any] = {
    "name": "Alice-4B",
    "description": "一位认真的助手。",
    "personality": "温和、直接。",
    "scenario": "一间安静的办公室。",
    "first_mes": "你好呀，今天有什么要查的？",
    "mes_example": (
        "<START>\n{{user}}: 早\n{{char}}: 早呀。今天的安排已经理好了。\n"
        "第二行接着说。\n<START>\n{{user}}: 晚安\n{{char}}: 晚安，明天见。"
    ),
}


def _png_with_chara(card: dict[str, Any]) -> bytes:
    """手工拼一张最小的 PNG：IHDR + 带 chara 数据的 tEXt + IEND（CRC 用 zlib 现算）。"""

    def chunk(ctype: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
        return len(data).to_bytes(4, "big") + ctype + data + crc.to_bytes(4, "big")

    ihdr = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes([8, 0, 0, 0, 0])
    text = b"chara\x00" + base64.b64encode(json.dumps(card).encode("utf-8"))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"tEXt", text)
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------- 载体层


def test_load_card_bytes_accepts_json_and_png() -> None:
    raw = json.dumps(V1_CARD).encode("utf-8")
    assert load_card_bytes(raw)["name"] == "Alice-4B"
    assert load_card_bytes(_png_with_chara(V1_CARD))["name"] == "Alice-4B"


def test_load_card_bytes_rejects_junk_and_plain_png() -> None:
    with pytest.raises(CardImportError, match="不是 PNG"):
        load_card_bytes(b"not a card")
    with pytest.raises(CardImportError, match="没有 chara"):
        load_card_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0aIHDR" + b"\x00" * 10)


# ---------------------------------------------------------------- 映射层


def test_v1_card_maps_prompt_sections_and_examples() -> None:
    result = parse_sillytavern_card(V1_CARD)
    card = result.card
    assert card.role_id == "alice_4b"
    assert card.role_name == "Alice-4B"
    assert card.system_prompt.startswith("【人设】\n一位认真的助手。")
    assert "【性格】\n温和、直接。" in card.system_prompt
    assert "【场景】\n一间安静的办公室。" in card.system_prompt
    assert "【开场白（首次问候可参考）】\n你好呀" in card.system_prompt
    assert card.exemplars is not None and len(card.exemplars) == 2
    assert card.exemplars[0].user == "早"
    assert card.exemplars[0].assistant == "早呀。今天的安排已经理好了。\n第二行接着说。"
    assert card.description is None
    assert result.report["spec"] == "v1"


def test_v2_card_counts_what_it_does_not_import() -> None:
    v2 = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            **V1_CARD,
            "creator_notes": "作者的说明。",
            "character_book": {"entries": [{"keys": ["a"]}, {"keys": ["b"]}]},
            "tags": ["助手", "办公室"],
            "alternate_greetings": ["另一版开场", "再一版"],
        },
    }
    result = parse_sillytavern_card(v2)
    assert result.report["spec"] == "chara_card_v2"
    assert result.report["worldbook_entries_dropped"] == 2
    assert result.report["tags_dropped"] == 2
    assert result.report["alternate_greetings_dropped"] == 2
    assert result.card.description == "作者的说明。"


def test_v2_system_prompt_takes_the_head_position() -> None:
    v2 = {"spec": "chara_card_v2", "data": {**V1_CARD, "system_prompt": "覆盖段"}}
    result = parse_sillytavern_card(v2)
    assert result.card.system_prompt.startswith("覆盖段\n\n【人设】")


def test_example_macros_are_resolved_with_literal_names() -> None:
    card = parse_sillytavern_card(V1_CARD).card
    assert card.exemplars is not None
    assert "{{char}}" not in card.exemplars[1].assistant
    assert "{{user}}" not in card.exemplars[1].user


def test_over_budget_exemplars_are_dropped_loudly() -> None:
    many = dict(V1_CARD)
    # 8 条各 ~700 字的范例必超条数与字数预算；末尾再补一条短的确认预算内仍在
    blocks = "".join(
        f"<START>\n{{{{user}}}}: 消息{i}\n{{{{char}}}}: {'回' * 700}\n" for i in range(8)
    )
    many["mes_example"] = blocks + "<START>\n{{user}}: 早\n{{char}}: 早呀"
    result = parse_sillytavern_card(many)
    assert result.card.exemplars is not None
    assert len(result.card.exemplars) <= 4  # MAX_EXEMPLARS
    assert result.report["exemplars_dropped"] >= 4  # 超预算的整条丢弃且对账


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Alice-4B", "alice_4b"),
        ("4chan bot", "st_4chan_bot"),
        ("星野アリス", None),  # 纯非 ASCII → st_ + 名字哈希
    ],
)
def test_role_id_slug_rules(name: str, expected: str | None) -> None:
    card = parse_sillytavern_card({"name": name, "description": "x"})
    if expected is not None:
        assert card.card.role_id == expected
    else:
        assert re.fullmatch(r"st_[0-9a-f]{8}", card.card.role_id)


def test_nameless_card_is_rejected() -> None:
    with pytest.raises(CardImportError, match="name"):
        parse_sillytavern_card({"description": "只有正文"})


def test_bodyless_card_is_rejected() -> None:
    with pytest.raises(CardImportError, match="没有可用的正文"):
        parse_sillytavern_card({"name": "空壳"})


# ---------------------------------------------------------------- 端点


def test_import_endpoint_roundtrip_and_conflict(tmp_path: Path) -> None:
    app = create_app(sqlite_path=tmp_path / "import.db")
    with TestClient(app) as client:
        res = client.post(
            "/api/roles/import",
            files={"file": ("alice.png", _png_with_chara(V1_CARD), "image/png")},
        )
        assert res.status_code == 201
        body = res.json()
        assert body["role"]["role_id"] == "alice_4b"
        assert body["report"]["spec"] == "v1"
        listed = client.get("/api/roles").json()
        assert any(r["role_id"] == "alice_4b" for r in listed)
        # 重名 → RoleAlreadyExists → 409（登记过的错误族）
        again = client.post(
            "/api/roles/import",
            files={"file": ("alice.png", _png_with_chara(V1_CARD), "image/png")},
        )
        assert again.status_code == 409
        # 垃圾输入 → 400，带人话
        bad = client.post(
            "/api/roles/import",
            files={"file": ("x.png", b"garbage", "application/octet-stream")},
        )
        assert bad.status_code == 400
        assert "detail" in bad.json()


# ---------------------------------------------------------------- 失败分支补测（diff 覆盖率）


def _png_with_raw_chara(raw_payload: bytes) -> bytes:
    """带**损坏** chara 数据的 PNG：内容不经 base64/JSON 校验直接塞进 tEXt。"""

    def chunk(ctype: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
        return len(data).to_bytes(4, "big") + ctype + data + crc.to_bytes(4, "big")

    ihdr = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes([8, 0, 0, 0, 0])
    text = b"chara\x00" + raw_payload
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"tEXt", text)
        + chunk(b"IEND", b"")
    )


def test_corrupt_chara_payload_is_rejected() -> None:
    with pytest.raises(CardImportError, match="解不开"):
        load_card_bytes(_png_with_raw_chara(b"\xff\xfe not base64 json"))


def test_chara_payload_non_object_is_rejected() -> None:
    payload = base64.b64encode(b"[1, 2, 3]")
    with pytest.raises(CardImportError, match="JSON 对象"):
        load_card_bytes(_png_with_raw_chara(payload))


def test_top_level_non_object_is_rejected() -> None:
    with pytest.raises(CardImportError, match="顶层必须是 JSON 对象"):
        load_card_bytes(b"[1, 2, 3]")


def test_blank_lines_inside_example_blocks_are_skipped() -> None:
    card = dict(V1_CARD)
    card["mes_example"] = "<START>\n{{user}}: 早\n\n{{char}}: 早呀"
    result = parse_sillytavern_card(card)
    assert result.card.exemplars is not None
    assert result.card.exemplars[0].user == "早"


def test_non_dict_card_payload_is_rejected() -> None:
    class _Weird:
        """有 .get 但不是 dict —— 防御形状的直测（load_card_bytes 不会产出它）。"""

        def get(self, key: str) -> str:
            return "x"

    with pytest.raises(CardImportError, match="v2 的 data 对象"):
        parse_sillytavern_card(_Weird())  # type: ignore[arg-type]


def test_shape_validation_failure_becomes_readable_error(monkeypatch) -> None:
    """映射全过但本仓形状校验拒绝（这里用非法 role_id 模拟）→ 人话 400，不 500。"""
    from rolecard_agent.features import card_import as ci_mod

    monkeypatch.setattr(ci_mod, "_slugify_role_id", lambda name: "1bad-id!")
    with pytest.raises(CardImportError, match="形状校验"):
        parse_sillytavern_card(dict(V1_CARD))


def test_import_endpoint_empty_and_oversized(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from rolecard_agent.api.main import create_app

    monkeypatch.setenv("MAX_UPLOAD_BYTES", "200")
    app = create_app(sqlite_path=tmp_path / "import2.db")
    with TestClient(app) as client:
        empty = client.post(
            "/api/roles/import", files={"file": ("x.png", b"", "image/png")}
        )
        assert empty.status_code == 400
        assert "空" in empty.json()["detail"]
        big = client.post(
            "/api/roles/import",
            files={"file": ("x.png", b"x" * 300, "image/png")},
        )
        assert big.status_code == 400
        assert "上限" in big.json()["detail"]

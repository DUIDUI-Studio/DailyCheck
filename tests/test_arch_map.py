"""架构事实图与变更简报的看守测试。

对应实现：
- `scripts/arch_map.py`   —— 从源码派生 `docs/ARCHITECTURE_MAP.md`
- `scripts/arch_digest.py` —— 对比两个时间点的地图，产出语义差异

这三条不变量必须成立，否则「文档不可能撒谎」的前提就没了：
1. 生成是**确定性**的（同一份源码两次生成逐字节相同）；
2. 提交的地图与源码**一致**（本地也能发现漂移，不用等 CI）；
3. 地图里的路由集合**等于** `ROUTE_MATRIX`（权威事实源），不多不少。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import arch_digest  # noqa: E402
import arch_map  # noqa: E402


def test_render_is_deterministic():
    """两次生成必须逐字节相同——否则 CI --check 会因无关重排反复变红。"""
    assert arch_map.render() == arch_map.render()


def test_render_contains_no_timestamp():
    """地图不能含日期/时间戳：会让 --check 每次提交都失败，git diff 也失去意义。"""
    text = arch_map.render()
    assert not re.search(r"20\d{2}-\d{2}-\d{2}", text), "地图里出现了日期，破坏确定性"
    assert not re.search(r"\d{2}:\d{2}:\d{2}", text), "地图里出现了时间，破坏确定性"


def test_committed_map_matches_source():
    """提交版地图必须与当前源码派生结果一致（本地复现 CI arch-map 门禁）。"""
    committed = arch_map.OUT.read_text(encoding="utf-8")
    assert committed == arch_map.render(), (
        "docs/ARCHITECTURE_MAP.md 与源码不一致："
        "运行 python3 scripts/arch_map.py --write 后提交该文件"
    )


def test_map_route_rows_equal_route_matrix():
    """地图 §2 必须完整覆盖 ROUTE_MATRIX——漏一行就意味着有人看不到那条路由的权限。"""
    matrix = arch_map.load_route_matrix()
    parsed = arch_digest.parse_map(arch_map.render())
    assert len(parsed["routes"]) == len(matrix)
    assert set(parsed["routes"]) == set(matrix)


def test_unmounted_blueprint_is_visible():
    """未装配蓝图（死代码）必须在图里显形，而不是悄悄消失。"""
    parsed = arch_digest.parse_map(arch_map.render())
    unmounted = sorted(k for k, v in parsed["blueprints"].items() if not v)
    assert "adjustment" in unmounted, f"未装配蓝图应包含 adjustment，实际 {unmounted}"


# --------------------------------------------------------------------------
# 变更简报：比结构而不是比文本
# --------------------------------------------------------------------------

LOGOUT_ROW = "| `auth.logout` | GET | `/logout` | 公开 |  |"


def test_digest_detects_added_route():
    base = arch_map.render()
    old_text = "\n".join(ln for ln in base.splitlines() if ln != LOGOUT_ROW)
    delta = arch_digest.compute_delta(
        arch_digest.parse_map(old_text), arch_digest.parse_map(base)
    )
    assert delta["add_routes"] == ["auth.logout"]
    assert delta["del_routes"] == []


def test_digest_detects_removed_route():
    base = arch_map.render()
    new_text = "\n".join(ln for ln in base.splitlines() if ln != LOGOUT_ROW)
    delta = arch_digest.compute_delta(
        arch_digest.parse_map(base), arch_digest.parse_map(new_text)
    )
    assert delta["del_routes"] == ["auth.logout"]


def test_digest_detects_auth_tightening():
    """权限收紧是 owner 最该看到的一类变更，必须被单独识别出来。"""
    base = arch_map.render()
    new_text = base.replace(LOGOUT_ROW, LOGOUT_ROW.replace("公开", "平台超管"))
    delta = arch_digest.compute_delta(
        arch_digest.parse_map(base), arch_digest.parse_map(new_text)
    )
    assert delta["chg_auth"] == [("auth.logout", "公开", "平台超管")]


def test_digest_reports_no_change_for_identical_maps():
    base = arch_map.render()
    delta = arch_digest.compute_delta(
        arch_digest.parse_map(base), arch_digest.parse_map(base)
    )
    assert all(not v for v in delta.values()), f"同图应无差异，实际 {delta}"

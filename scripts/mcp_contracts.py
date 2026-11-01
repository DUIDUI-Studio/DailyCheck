"""Generate/check MCP contract artifacts without contacting a server or database.

Usage: python -m scripts.mcp_contracts --write | --check
Exit 0: artifacts match; exit 1: drift/invariant failure; exit 2: bad arguments.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import json
import pkgutil
import re
from dataclasses import asdict
from pathlib import Path

from mcp_server.protocol import tools as tool_package
from mcp_server.protocol.registry import REGISTRY, validate_registry

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = Path("docs/integrations/dailycheck-mcp")


def validate_implementation_set() -> None:
    """Discover every tool module: an orphan implementation must fail --check."""
    validate_registry(REGISTRY)
    implemented = {}
    for info in pkgutil.iter_modules(tool_package.__path__):
        module = importlib.import_module(f"{tool_package.__name__}.{info.name}")
        for name, function in vars(module).items():
            if name.endswith("_impl") and inspect.isfunction(function) and function.__module__ == module.__name__:
                tool_name = name.removesuffix("_impl")
                if tool_name in implemented:
                    raise ValueError(f"duplicate implementation: {tool_name}")
                implemented[tool_name] = function
    registered = {entry.name: entry.handler for entry in REGISTRY}
    if implemented != registered:
        raise ValueError(
            f"implementation drift: unregistered={sorted(implemented.keys() - registered.keys())}; "
            f"unimplemented={sorted(registered.keys() - implemented.keys())}; "
            f"wrong bindings={sorted(n for n in registered.keys() & implemented.keys() if registered[n] is not implemented[n])}"
        )

    # Approval is independent of discovery. Never generate Bot's authority here.
    from bot.tool_contract import MCP_NAMES, READ_PATHS, WRITE_PATHS

    approved = {entry.name for entry in REGISTRY if entry.bot_approved}
    if approved != MCP_NAMES:
        raise ValueError("Bot approval metadata drift; approval must remain explicit")
    for entry in REGISTRY:
        if entry.bot_approved:
            paths = READ_PATHS if entry.mode == "read" else WRITE_PATHS
            if paths.get(entry.name) != entry.access.path:
                raise ValueError(f"Bot path metadata drift: {entry.name}")




def calculation_scope_reference() -> str:
    """Count and scope reference for the maintained calculation guide intro."""
    canonical = sum(entry.domain == "canonical" for entry in REGISTRY)
    orders = [entry.name for entry in REGISTRY if entry.domain == "store_order"]
    base = len(REGISTRY) - canonical - len(orders)
    external = sum(entry.external_subset for entry in REGISTRY)
    order_names = " / ".join(f"`{name}`" for name in orders)
    return (
        f"> 当前工具范围：实时 {len(REGISTRY)} 项（基础 {base} 项 + 订单只读 {len(orders)} 项 + canonical 主数据 {canonical} 项）；"
        f"外部 tools.json 仅为 {external} 项子集，不代表授权。\n"
        "> 本文说明基础工具的计算口径；所有名称、输入/实际输出形状和 ACL 范围以[完整工具台账](./registry.md)为准。\n"
        f"> 订单读取：{order_names} 只读，按参与仓可见全单（含其他仓明细与金额）；详见[订单读取契约](./README.md)。"
    )


def validate_maintenance_references(root: Path = ROOT) -> None:
    """Keep the remaining entry-guide inventories and test references verifiable."""
    names = {entry.name for entry in REGISTRY}
    for filename in ("AGENT.md", "CLAUDE.md"):
        text = (root / filename).read_text(encoding="utf-8")
        match = re.search(r"## MCP 服务器（(\d+) 个工具）\n(.*?)(?=\n## |\Z)", text, re.S)
        if match is None or int(match[1]) != len(names):
            raise ValueError(f"tool inventory count drift: {filename}")
        section = match[2]
        if filename == "AGENT.md":
            listing = re.search(r"^工具：(.*)$", section, re.M)
            listed = re.findall(r"`([a-z_]+)`", listing[1]) if listing else []
        else:
            listed = re.findall(r"^\| `([a-z_]+)` \|", section, re.M)
        if len(listed) != len(names) or set(listed) != names:
            raise ValueError(f"tool inventory names drift: {filename}")

    calculation = root / ARTIFACT_DIR / "calculation-logic.md"
    intro = calculation.read_text(encoding="utf-8").split("\n---", 1)[0]
    if calculation_scope_reference() not in intro:
        raise ValueError(f"calculation scope drift: {ARTIFACT_DIR / 'calculation-logic.md'}")

    function_names = {}
    for entry in REGISTRY:
        for reference in entry.tests:
            file, node = reference.split("::", 1)
            if file not in function_names:
                tree = ast.parse((root / file).read_text(encoding="utf-8"))
                function_names[file] = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
            if node.split("[", 1)[0] not in function_names[file]:
                raise ValueError(f"test reference drift: {reference}")


def json_text(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def generated_artifacts() -> dict[Path, str]:
    validate_implementation_set()
    records = []
    external = []
    for entry in REGISTRY:
        definition = entry.tool.model_dump(mode="json", exclude_none=True)
        records.append({
            **definition,
            "domain": entry.domain,
            "handler": f"{entry.handler.__module__}:{entry.handler.__name__}",
            "mode": entry.mode,
            "access": asdict(entry.access),
            "output_contract": {"kind": "minimum-json-shape", "schema": entry.output_shape},
            "external_subset": entry.external_subset,
            "bot_approved": entry.bot_approved,
            "tests": list(entry.tests),
            "compatibility": entry.compatibility,
        })
        if entry.external_subset:
            external.append({key: definition[key] for key in ("name", "description", "inputSchema")})
    counts = {
        "runtime": len(records), "external_subset": len(external),
        "bot_approved": sum(entry.bot_approved for entry in REGISTRY),
        "writes": sum(entry.mode == "write" for entry in REGISTRY),
        "canonical": sum(entry.domain == "canonical" for entry in REGISTRY),
    }
    ledger = {
        "schema_version": 1,
        "generated_from": "mcp_server/protocol/registry.py",
        "counts": counts,
        "notes": [
            "Maintenance metadata only; does not grant external token or Bot permissions.",
            "Output contracts validate existing serialized JSON; no SDK outputSchema/structuredContent.",
            "Read describes business intent; legacy HTTP/stdio reads can initialize schema or populate procurement caches.",
            "Bot read contexts use read_only=True; approval and runtime identity/scope guards remain independent.",
        ],
        "tools": records,
    }
    subset = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "description": (
            f"DailyCheck external integration subset: {counts['external_subset']} tools; "
            f"runtime registry: {counts['runtime']} tools including {counts['canonical']} canonical tools. "
            "Subset membership is documentation, not authorization. Full maintenance ledger: registry.json."
        ),
        "server": None,
        "tools": external,
    }
    lines = [
        "# MCP tool contracts (generated)", "",
        "Source: `mcp_server/protocol/registry.py`. Regenerate with "
        "`python -m scripts.mcp_contracts --write`; verify in CI with "
        "`python -m scripts.mcp_contracts --check`.", "",
        f"Runtime: **{counts['runtime']}** tools; external integration subset: **{counts['external_subset']}**; "
        f"Bot approved: **{counts['bot_approved']}**; business writes: **{counts['writes']}**.", "",
        "`registry.json` contains every input declaration, handler, minimum output JSON shape, "
        "ACL enforcement/scope, test reference and compatibility status. `tools.json` intentionally "
        "publishes only the external integration subset. Neither file grants permissions.", "",
        "Read/write classifies business intent; legacy HTTP/stdio reads may run migrations/cache writes. "
        "Bot read contexts remain read-only. Output shapes describe the existing JSON inside "
        "TextContent; no SDK outputSchema is enabled. `items_detail` keeps its list wrapper, "
        "`procurement_hub` keeps its array, `canonical_fanout_event` can return null, and "
        "`canonical_fanout` keeps per_canonical rather than the older description's warehouses/items.", "",
        "| Tool | Domain | Mode | ACL path | Path enforcement | Warehouse scope | External subset | Bot approved |",
        "|------|--------|------|----------|------------------|-----------------|-----------------|--------------|",
    ]
    for entry in REGISTRY:
        lines.append(
            f"| `{entry.name}` | {entry.domain} | {entry.mode} | `{entry.access.method} {entry.access.path}` | "
            f"{entry.access.path_enforcement} | {entry.access.warehouse_scope} | "
            f"{'yes' if entry.external_subset else 'no'} | {'yes' if entry.bot_approved else 'no'} |"
        )
    return {
        ARTIFACT_DIR / "registry.json": json_text(ledger),
        ARTIFACT_DIR / "tools.json": json_text(subset),
        ARTIFACT_DIR / "registry.md": "\n".join(lines) + "\n",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        validate_maintenance_references(ROOT)
        artifacts = generated_artifacts()
        drift = []
        for path, expected in artifacts.items():
            target = ROOT / path
            if args.write:
                target.write_text(expected, encoding="utf-8")
            elif not target.exists() or target.read_text(encoding="utf-8") != expected:
                drift.append(str(path))
        if drift:
            print("MCP contract drift: " + ", ".join(drift))
            print("Run python -m scripts.mcp_contracts --write and review the generated diff.")
            return 1
    except (ValueError, OSError) as exc:
        print(f"MCP contract validation failed: {exc}")
        return 1
    print(f"MCP contracts: {len(REGISTRY)} runtime / "
          f"{sum(e.external_subset for e in REGISTRY)} external subset / "
          f"{sum(e.bot_approved for e in REGISTRY)} Bot approved; "
          + ("written" if args.write else "OK"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

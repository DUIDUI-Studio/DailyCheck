"""Behavioral contract for the protected application EnvironmentFile boundary.

The synthetic file below is the only credential-shaped data in this module.
Values are quoted into the unit's fixed ``env -i`` word list as ``${KEY}``
references, which systemd expands to exactly one argument (including embedded
whitespace and punctuation); absence removes the word entirely so application
source defaults are preserved.  App units receive the full measured business
projection; MCP units receive only the six observed WMS keys.  The macOS child
observation executes the real ``env -i``/lower-process boundary with setpriv
removed; it does not claim that a local fixture ran systemd itself.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.prod_agent import native_prepare
from scripts.prod_agent.native_pipeline import (
    APP_APPLICATION_KEYS,
    APPLICATION_CREDENTIAL_KEYS,
    MCP_APPLICATION_KEYS,
    OPTIONAL_APPLICATION_KEYS,
    REQUIRED_APPLICATION_KEYS,
    ProtectedPipeline,
    parse_application_environment,
    render_unit,
    validate_application_environment,
)

SECRET = "s" * 40
TOKEN = "t" * 40
APP_ONLY_KEYS = tuple(key for key in APP_APPLICATION_KEYS if key not in MCP_APPLICATION_KEYS)
SYNTHETIC = {
    "DAILYCHECK_MULTI_WAREHOUSE": "1",
    "DAILYCHECK_WMS_HOST": "wms.internal",
    "DAILYCHECK_WMS_PORT": "1433",
    "DAILYCHECK_WMS_USER": "dc_reader",
    "DAILYCHECK_WMS_PASSWORD": "p@ss w0rd!#%&*()_+[]{};:,.<>?/|~`",
    "DAILYCHECK_WMS_DATABASE": "DailyCheck",
    "DAILYCHECK_WMS_CUSTOMER_ID": "42",
    "FEEDBACK_GITEA_TOKEN": "gitea-issue-token_0123456789abcdef",
    "FEEDBACK_LLM_API_KEY": "sk-feedback-llm_0123456789abcdef",
    "FEEDBACK_LLM_BASE_URL": "https://llm.example.invalid/api/v1?region=cn&mode=fast",
    "FEEDBACK_LLM_MODEL": "MiniMax-M3.1-Flash",
    "WECOM_FEEDBACK_WEBHOOK":
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=00000000-aaaa-bbbb-cccc-111111111111",
    "WECOM_STORE_ORDER_WEBHOOK":
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=22222222-dddd-eeee-ffff-333333333333",
}
CONTROL_ENVIRONMENT = ("PATH", "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "BASH_ENV",
                       "GITHUB_TOKEN", "GITEA_TOKEN", "DAILYCHECK_GITEA_TOKEN",
                       "DAILYCHECK_APP_PRIVATE_KEY", "DAILYCHECK_DEPLOY_ATTEMPT",
                       "DC_NATIVE_POLICY", "LLM_API_KEY", "PUBLIC_BASE_URL",
                       "FEEDBACK_GITHUB_TOKEN", "DAILYCHECK_WMS_TDS_VERSION")
NON_LF_RECORD_SEPARATORS = {"CR": b"\r", "VT": b"\x0b", "FF": b"\x0c",
                            "FS": b"\x1c", "GS": b"\x1d", "RS": b"\x1e"}


def environment_bytes(optional: dict[str, str] | None = None, *,
                      required: bool = True) -> bytes:
    selected = SYNTHETIC if optional is None else optional
    lines = []
    if required:
        lines.extend((f"DAILYCHECK_SECRET_KEY={SECRET}", f"DAILYCHECK_MCP_TOKEN={TOKEN}"))
    lines.extend(f"{key}='{value}'" for key, value in selected.items())
    return ("\n".join(lines) + "\n").encode("ascii")


def test_lf_and_crlf_records_are_supported():
    lf = environment_bytes()
    crlf = lf.replace(b"\n", b"\r\n")
    assert parse_application_environment(lf) == parse_application_environment(crlf)
    assert validate_application_environment(crlf, mcp_health_token=TOKEN,
                                            read_token="r" * 40)["DAILYCHECK_WMS_PASSWORD"] == \
        SYNTHETIC["DAILYCHECK_WMS_PASSWORD"]


@pytest.mark.parametrize("separator", NON_LF_RECORD_SEPARATORS.values(),
                         ids=NON_LF_RECORD_SEPARATORS)
def test_non_lf_record_separators_are_denied_between_and_within(separator):
    valid = environment_bytes()
    broken = valid.replace(b"DAILYCHECK_WMS_USER='dc_reader'\nDAILYCHECK_WMS_PASSWORD=",
                           b"DAILYCHECK_WMS_USER='dc_reader'" + separator
                           + b"DAILYCHECK_WMS_PASSWORD=")
    assert not any(line.startswith(b"DAILYCHECK_WMS_PASSWORD=") for line in broken.split(b"\n"))
    with pytest.raises(ValueError) as raised:
        parse_application_environment(broken)
    assert SYNTHETIC["DAILYCHECK_WMS_PASSWORD"] not in str(raised.value)
    with pytest.raises(ValueError):
        validate_application_environment(broken, mcp_health_token=TOKEN,
                                         read_token="r" * 40)
    within = environment_bytes({**SYNTHETIC, "DAILYCHECK_WMS_PASSWORD":
                                "synthetic" + separator.decode("ascii") + "pass"})
    with pytest.raises(ValueError):
        parse_application_environment(within)


def _render(*, role: str = "app", optional: tuple[str, ...] | None = None) -> str:
    selected = APP_APPLICATION_KEYS if role == "app" else MCP_APPLICATION_KEYS
    return render_unit(
        description="contract fixture", working_directory="/opt/release",
        environment_file="/etc/dailycheck-native/application.env",
        environment_lines=[("PATH", "/usr/bin:/bin"), ("PYTHONDONTWRITEBYTECODE", "1"),
                           ("DAILYCHECK_ENV", "production"), ("DRY_RUN", "true"),
                           ("DAILYCHECK_DB_DIR", "/var/lib/dailycheck-green")],
        read_write_paths="/var/lib/dailycheck-green", inaccessible_paths="/etc/dailycheck-native",
        module="dailycheck", python="/opt/app/bin/python3", app_uid=996, app_gid=988,
        application_role=role,
        application_inputs=selected if optional is None else optional,
    )


def _execstart_assignments(unit: str, application_environment: bytes | None = None) -> dict[str, str]:
    """Expand the fixed ``env -i`` word list per documented systemd semantics.

    ``${FOO}`` is replaced by the exact value as one argument and by an empty
    string when unset; it is never word-split and the value is never re-parsed
    as shell syntax.  This emulates the documented contract, not systemd.
    """
    line = next(item for item in unit.splitlines() if item.startswith("ExecStart="))
    prefix = "ExecStart=/usr/bin/env -i "
    assert line.startswith(prefix) and " /usr/bin/setpriv " in line
    words = line[len(prefix):].split(" /usr/bin/setpriv ", 1)[0].split(" ")
    environment = {row.split("=", 1)[0]: row.split("=", 1)[1]
                   for row in unit.splitlines()
                   if row.startswith("Environment=") and "${" not in row}
    environment.update(parse_application_environment(
        environment_bytes() if application_environment is None else application_environment))
    expanded = {}
    for word in words:
        value = re.sub(r"\$\{([A-Z_]+)\}",
                       lambda match: environment.get(match.group(1), ""), word)
        key, separator, item = value.partition("=")
        assert separator
        expanded[key] = item
        environment[key] = item
    return expanded


def _portable_lower(monkeypatch, tmp_path):
    port = object.__new__(ProtectedPipeline)
    port.policy = SimpleNamespace(value={"app_uid": os.geteuid()})
    port.value = {"app_gid": os.getegid()}
    port.python = Path(sys.executable)
    monkeypatch.setattr(port, "_owned", lambda: object())
    monkeypatch.setattr(port, "_release", lambda _candidate: tmp_path)
    original = subprocess.Popen

    def portable(command, **kwargs):
        # Keep the real cwd/env/close_fds/stdio contract; only the unavailable
        # Linux identity helper is removed.  No production constructor allows it.
        return original(command[command.index(str(sys.executable)):], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", portable)
    return port


def _observe_lower(monkeypatch, tmp_path, unit: str) -> dict:
    port = _portable_lower(monkeypatch, tmp_path)
    fd = os.open(tmp_path / "control", os.O_CREAT | os.O_WRONLY, 0o600)
    monkeypatch.setenv("SIGNING_TOKEN", "control-only")
    monkeypatch.setenv("DAILYCHECK_DEPLOY_ATTEMPT", "control-only")
    try:
        code = ("import json,os; "
                f"print(json.dumps({{'env':dict(os.environ),"
                f"'fence':os.path.exists('/dev/fd/{fd}')}}))")
        result = port._lower(["-c", code], db=tmp_path / "db", scratch=tmp_path,
                             extra=_execstart_assignments(unit))
    finally:
        os.close(fd)
    assert result.returncode == 0
    observed = json.loads(result.stdout)
    assert observed["fence"] is False
    assert not {"SIGNING_TOKEN", "DAILYCHECK_DEPLOY_ATTEMPT"} & observed["env"].keys()
    return observed["env"]


def test_original_two_key_file_still_parses():
    values = parse_application_environment(environment_bytes(optional={}))
    assert values == {"DAILYCHECK_SECRET_KEY": SECRET, "DAILYCHECK_MCP_TOKEN": TOKEN}


def test_complete_file_preserves_values_verbatim():
    values = parse_application_environment(environment_bytes())
    assert values["DAILYCHECK_SECRET_KEY"] == SECRET
    assert values["DAILYCHECK_MCP_TOKEN"] == TOKEN
    assert {key: values[key] for key in OPTIONAL_APPLICATION_KEYS} == SYNTHETIC


def test_app_render_propagates_only_present_optional_inputs():
    present = _render()
    absent = _render(optional=())
    assert "DAILYCHECK_SECRET_KEY=${DAILYCHECK_SECRET_KEY}" in present
    assert "DAILYCHECK_MCP_TOKEN=${DAILYCHECK_MCP_TOKEN}" in present
    for key in APP_APPLICATION_KEYS:
        assert f"{key}=${{{key}}}" in present
        assert key not in absent
    assert absent.count("DAILYCHECK_SECRET_KEY=${DAILYCHECK_SECRET_KEY}") == 1


def test_mcp_render_projects_only_observed_wms_inputs():
    unit = _render(role="mcp")
    for key in MCP_APPLICATION_KEYS:
        assert f"{key}=${{{key}}}" in unit
    for key in APP_ONLY_KEYS:
        assert key not in unit
    assert "DAILYCHECK_MULTI_WAREHOUSE" not in unit
    assert "FEEDBACK_GITEA_TOKEN" not in unit


def test_render_rejects_role_crossing_and_unknown_roles():
    with pytest.raises(ValueError):
        _render(role="mcp", optional=("FEEDBACK_GITEA_TOKEN",))
    with pytest.raises(ValueError):
        _render(role="mcp", optional=("WECOM_STORE_ORDER_WEBHOOK",))
    with pytest.raises(ValueError):
        _render(role="mcp", optional=("DAILYCHECK_MULTI_WAREHOUSE",))
    with pytest.raises(ValueError):
        _render(role="controller")
    for key in CONTROL_ENVIRONMENT:
        with pytest.raises(ValueError):
            _render(role="app", optional=(key,))
        with pytest.raises(ValueError):
            _render(role="mcp", optional=(key,))
    with pytest.raises(ValueError):
        _render(optional=("DAILYCHECK_WMS_HOST", "DAILYCHECK_WMS_HOST"))


def test_render_never_embeds_application_values():
    for role in ("app", "mcp"):
        unit = _render(role=role)
        for value in (SECRET, TOKEN, SYNTHETIC["DAILYCHECK_WMS_PASSWORD"],
                      SYNTHETIC["DAILYCHECK_WMS_HOST"], SYNTHETIC["DAILYCHECK_WMS_DATABASE"],
                      SYNTHETIC["FEEDBACK_GITEA_TOKEN"], SYNTHETIC["FEEDBACK_LLM_BASE_URL"],
                      SYNTHETIC["WECOM_FEEDBACK_WEBHOOK"]):
            assert value not in unit


@pytest.mark.parametrize("line", [f"{key}='{SECRET}'" for key in CONTROL_ENVIRONMENT])
def test_unknown_or_control_keys_are_denied(line):
    raw = environment_bytes() + (line + "\n").encode()
    with pytest.raises(ValueError) as raised:
        parse_application_environment(raw)
    assert SECRET not in str(raised.value)


@pytest.mark.parametrize("raw", [
    environment_bytes().replace(f"DAILYCHECK_SECRET_KEY={SECRET}\n".encode(), b""),
    environment_bytes(required=False),
    environment_bytes().replace(b"DAILYCHECK_WMS_HOST='wms.internal'\n",
                                b"DAILYCHECK_WMS_HOST='wms.internal'\n"
                                b"DAILYCHECK_WMS_HOST='wms.internal'\n"),
    b"DAILYCHECK_SECRET_KEY=" + b"short\n" + b"DAILYCHECK_MCP_TOKEN=" + TOKEN.encode() + b"\n",
    b"DAILYCHECK_SECRET_KEY=" + SECRET.encode() + b"\n"
    b"DAILYCHECK_MCP_TOKEN=" + TOKEN.encode() + b"\n\n",
    b"not-an-assignment\n",
    b" DAILYCHECK_SECRET_KEY=" + SECRET.encode() + b"\nDAILYCHECK_MCP_TOKEN=" + TOKEN.encode() + b"\n",
    b"DAILYCHECK_SECRET_KEY=" + SECRET.encode() + b"\nDAILYCHECK_MCP_TOKEN=" + TOKEN.encode()
    + b"\nDAILYCHECK_WMS_HOST=wms.internal\n",
    environment_bytes().replace(b"DAILYCHECK_WMS_HOST='wms.internal'",
                                b"DAILYCHECK_WMS_HOST=\xff"),
    environment_bytes().replace(b"DAILYCHECK_WMS_HOST='wms.internal'",
                                b"DAILYCHECK_WMS_HOST='wms.internal"),
])
def test_malformed_or_duplicate_files_are_denied(raw):
    with pytest.raises(ValueError) as raised:
        parse_application_environment(raw)
    assert SECRET not in str(raised.value) and TOKEN not in str(raised.value)


@pytest.mark.parametrize("key,value", [
    ("DAILYCHECK_MULTI_WAREHOUSE", "sometimes"),
    ("DAILYCHECK_WMS_PORT", "0"),
    ("DAILYCHECK_WMS_PORT", "65536"),
    ("DAILYCHECK_WMS_PORT", "1433x"),
    ("DAILYCHECK_WMS_CUSTOMER_ID", "1" * 11),
    ("DAILYCHECK_WMS_DATABASE", "db name"),
    ("DAILYCHECK_WMS_PASSWORD", "x" * 257),
    ("DAILYCHECK_WMS_PASSWORD", "has'quote"),
    ("DAILYCHECK_WMS_HOST", "host name"),
    ("FEEDBACK_GITEA_TOKEN", "short"),
    ("FEEDBACK_LLM_API_KEY", "x" * 257),
    ("FEEDBACK_LLM_BASE_URL", "has space"),
    ("FEEDBACK_LLM_MODEL", "model name"),
    ("WECOM_FEEDBACK_WEBHOOK", "not a url"),
    ("WECOM_STORE_ORDER_WEBHOOK", "has'quote"),
])
def test_value_boundaries_are_enforced_without_echoing_values(key, value):
    raw = environment_bytes({**SYNTHETIC, key: value})
    with pytest.raises(ValueError) as raised:
        parse_application_environment(raw)
    assert value not in str(raised.value)


def test_mcp_health_equality_and_read_token_separation():
    values = validate_application_environment(environment_bytes(),
                                              mcp_health_token=TOKEN,
                                              read_token="r" * 40)
    assert values["DAILYCHECK_MCP_TOKEN"] == TOKEN
    with pytest.raises(ValueError) as mismatch:
        validate_application_environment(environment_bytes(), mcp_health_token="u" * 40,
                                         read_token="r" * 40)
    assert TOKEN not in str(mismatch.value)
    for key in APPLICATION_CREDENTIAL_KEYS:
        leaked = environment_bytes({**SYNTHETIC, key: "r" * 40})
        with pytest.raises(ValueError) as overlap:
            validate_application_environment(leaked, mcp_health_token=TOKEN,
                                             read_token="r" * 40)
        assert "r" * 40 not in str(overlap.value)
    reused = environment_bytes({**SYNTHETIC, "FEEDBACK_GITEA_TOKEN": TOKEN})
    with pytest.raises(ValueError) as health_reuse:
        validate_application_environment(reused, mcp_health_token=TOKEN,
                                         read_token="r" * 40)
    assert TOKEN not in str(health_reuse.value)


def test_app_projection_reaches_real_lower_child(monkeypatch, tmp_path):
    environment = _observe_lower(monkeypatch, tmp_path, _render())
    expected = {"DAILYCHECK_SECRET_KEY": SECRET, "DAILYCHECK_MCP_TOKEN": TOKEN, **SYNTHETIC}
    for key in (*REQUIRED_APPLICATION_KEYS, *APP_APPLICATION_KEYS):
        assert environment[key] == expected[key]


def test_mcp_projection_excludes_app_only_inputs_in_real_child(monkeypatch, tmp_path):
    environment = _observe_lower(monkeypatch, tmp_path, _render(role="mcp"))
    for key in MCP_APPLICATION_KEYS:
        assert environment[key] == SYNTHETIC[key]
    assert not set(APP_ONLY_KEYS) & environment.keys()
    assert "DAILYCHECK_MULTI_WAREHOUSE" not in environment


def test_absent_optional_inputs_do_not_reach_the_child(monkeypatch, tmp_path):
    unit = _render(optional=())
    environment = _observe_lower(monkeypatch, tmp_path, unit)
    assert environment["DAILYCHECK_SECRET_KEY"] == SECRET
    assert not set(APP_APPLICATION_KEYS) & environment.keys()


def test_both_renderers_share_the_single_unit_recipe():
    assert native_prepare.render_unit is render_unit

"""Unit tests for the erpclaw shell shim + install/uninstall hooks.

Runs against a fabricated CLAWHUB_HOME so it works identically on macOS
and Ubuntu without touching the user's real install.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[4]
SHIM = REPO_ROOT / "source/erpclaw/bin/erpclaw"
INSTALL_BIN = REPO_ROOT / "source/erpclaw/scripts/install_bin.py"
UNINSTALL_BIN = REPO_ROOT / "source/erpclaw/scripts/uninstall_bin.py"


def make_mock_home(tmp_path: Path, skills: dict[str, dict]) -> Path:
    """Create a fake CLAWHUB_HOME with skills + their SKILL.md.

    `skills` is `{skill_name: {"actions": [list of action names], "with_db_query": bool}}`.
    """
    home = tmp_path / "openclaw"
    skills_dir = home / "skills"
    skills_dir.mkdir(parents=True)
    for skill_name, config in skills.items():
        skill_dir = skills_dir / skill_name
        skill_dir.mkdir()
        lines = [
            "---",
            f"name: {skill_name}",
            "version: 1.0.0",
            "---",
            f"# {skill_name}",
            "",
            "## Actions",
            "",
            "| Action | Description |",
            "|--------|-------------|",
        ]
        for action in config.get("actions", []):
            lines.append(f"| `{action}` | Test action |")
        (skill_dir / "SKILL.md").write_text("\n".join(lines))
        if config.get("with_db_query"):
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir()
            # Minimal db_query.py that echoes --action
            (scripts_dir / "db_query.py").write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "action = None\n"
                "for i, a in enumerate(sys.argv):\n"
                "    if a == '--action' and i + 1 < len(sys.argv):\n"
                "        action = sys.argv[i + 1]\n"
                "print(f'ok: {action}')\n"
            )
            (scripts_dir / "db_query.py").chmod(0o755)
    return home


def run_shim(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAWHUB_HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(SHIM), *args],
        env=env,
        capture_output=True,
        text=True,
    )


# -----------------------------------------------------------------------------
# Shim resolution
# -----------------------------------------------------------------------------


def test_help_exits_zero(tmp_path):
    home = make_mock_home(tmp_path, {})
    result = run_shim(home, "--help")
    assert result.returncode == 0
    assert "erpclaw shell shim" in result.stdout


def test_version_reads_core_skill(tmp_path):
    home = make_mock_home(tmp_path, {"erpclaw": {"actions": []}})
    result = run_shim(home, "--version")
    assert result.returncode == 0
    assert "erpclaw 1.0.0" in result.stdout


def test_version_without_core_skill_reports_unknown(tmp_path):
    home = make_mock_home(tmp_path, {})
    result = run_shim(home, "--version")
    assert result.returncode == 0
    assert "version unknown" in result.stdout


def test_list_empty_home_returns_1(tmp_path):
    home = make_mock_home(tmp_path, {})
    # Remove the skills dir entirely so we hit the "not found" branch
    shutil.rmtree(home / "skills")
    result = run_shim(home, "list")
    assert result.returncode == 1
    assert "No actions found" in result.stderr


def test_list_shows_actions_grouped_by_skill(tmp_path):
    home = make_mock_home(
        tmp_path,
        {
            "erpclaw": {"actions": ["add-customer", "add-invoice"]},
            "erpclaw-integrations-shopify": {
                "actions": ["shopify-connect", "shopify-disconnect"],
            },
        },
    )
    result = run_shim(home, "list")
    assert result.returncode == 0
    assert "erpclaw" in result.stdout
    assert "add-customer" in result.stdout
    assert "shopify-connect" in result.stdout


def test_which_finds_correct_skill(tmp_path):
    home = make_mock_home(
        tmp_path,
        {
            "erpclaw-integrations-shopify": {
                "actions": ["shopify-connect"],
                "with_db_query": True,
            },
        },
    )
    result = run_shim(home, "--which", "shopify-connect")
    assert result.returncode == 0
    assert "erpclaw-integrations-shopify" in result.stdout
    assert "exists:   True" in result.stdout


def test_which_missing_action_fails(tmp_path):
    home = make_mock_home(tmp_path, {"erpclaw": {"actions": []}})
    result = run_shim(home, "--which", "nonexistent-action")
    assert result.returncode == 1


def test_action_dispatch_execs_db_query(tmp_path):
    home = make_mock_home(
        tmp_path,
        {
            "erpclaw-integrations-shopify": {
                "actions": ["shopify-connect"],
                "with_db_query": True,
            },
        },
    )
    result = run_shim(home, "shopify-connect", "--pairing-code", "ABC-1X9")
    assert result.returncode == 0
    assert "ok: shopify-connect" in result.stdout


def test_unknown_action_returns_127(tmp_path):
    home = make_mock_home(tmp_path, {"erpclaw": {"actions": []}})
    result = run_shim(home, "totally-fake-action")
    assert result.returncode == 127
    assert "not found" in result.stderr


def test_skill_without_db_query_returns_2(tmp_path):
    home = make_mock_home(
        tmp_path,
        {
            "broken-skill": {
                "actions": ["some-action"],
                "with_db_query": False,
            },
        },
    )
    result = run_shim(home, "some-action")
    assert result.returncode == 2
    assert "no scripts/db_query.py" in result.stderr


def test_cache_written_on_first_run(tmp_path):
    home = make_mock_home(
        tmp_path, {"erpclaw": {"actions": ["add-customer"]}},
    )
    run_shim(home, "list")
    cache = home / "action_map.json"
    assert cache.exists()
    content = json.loads(cache.read_text())
    assert content["add-customer"] == "erpclaw"


def test_rebuild_action_map_refreshes_cache(tmp_path):
    home = make_mock_home(
        tmp_path, {"erpclaw": {"actions": ["add-customer"]}},
    )
    run_shim(home, "list")
    # Add another skill after cache built
    new_skill = home / "skills" / "erpclaw-new"
    new_skill.mkdir()
    (new_skill / "SKILL.md").write_text(
        "---\nname: erpclaw-new\nversion: 1.0\n---\n"
        "| `new-action` | Test |\n"
    )
    result = run_shim(home, "--rebuild-action-map")
    assert result.returncode == 0
    content = json.loads((home / "action_map.json").read_text())
    assert "new-action" in content
    assert content["new-action"] == "erpclaw-new"


def test_first_wins_on_duplicate_action(tmp_path):
    """If two skills declare the same action, alphabetically-first wins."""
    home = make_mock_home(
        tmp_path,
        {
            "b-skill": {"actions": ["shared-action"]},
            "a-skill": {"actions": ["shared-action"]},
        },
    )
    run_shim(home, "--rebuild-action-map")
    content = json.loads((home / "action_map.json").read_text())
    assert content["shared-action"] == "a-skill"


def test_deeply_nested_skill_dirs_ignored(tmp_path):
    """Non-directory entries under skills/ are skipped safely."""
    home = make_mock_home(tmp_path, {"erpclaw": {"actions": ["add-customer"]}})
    # Drop a stray file under skills/
    (home / "skills" / "README.md").write_text("some doc")
    result = run_shim(home, "list")
    assert result.returncode == 0
    assert "add-customer" in result.stdout


# -----------------------------------------------------------------------------
# install_bin.py
# -----------------------------------------------------------------------------


def run_install(home: Path, extra_path: Path | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAWHUB_HOME"] = str(home)
    if extra_path:
        env["PATH"] = f"{extra_path}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [sys.executable, str(INSTALL_BIN), "--home", str(home), "-v"],
        env=env,
        capture_output=True,
        text=True,
    )


def setup_installable_home(tmp_path: Path) -> Path:
    """Mock home where SKILL_REL_PATH resolves."""
    home = tmp_path / "openclaw"
    shim_dir = home / "skills/erpclaw/bin"
    shim_dir.mkdir(parents=True)
    shim_target = shim_dir / "erpclaw"
    shim_target.write_text("#!/usr/bin/env python3\nprint('shim')\n")
    shim_target.chmod(0o755)
    return home


def test_install_creates_symlink_in_writable_dir(tmp_path, monkeypatch):
    home = setup_installable_home(tmp_path)
    fake_bin = tmp_path / "my_local_bin"
    fake_bin.mkdir()

    # Patch HOME to point at tmp_path so ~/.local/bin resolves here
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".local").mkdir()
    local_bin = tmp_path / ".local/bin"
    local_bin.mkdir()

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["CLAWHUB_HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, str(INSTALL_BIN), "--home", str(home), "-v"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 5)  # 5 = PATH warning, still success
    # Find the symlink
    candidates = [
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        local_bin,
    ]
    found = None
    for c in candidates:
        target = c / "erpclaw"
        if target.is_symlink():
            existing = Path(os.readlink(target))
            if not existing.is_absolute():
                existing = target.parent / existing
            if existing.resolve() == (home / "skills/erpclaw/bin/erpclaw").resolve():
                found = target
                break
    assert found is not None, f"no erpclaw symlink found pointing at {home}"
    # Clean up — remove the symlink we just created
    found.unlink()


def test_install_idempotent(tmp_path, monkeypatch):
    home = setup_installable_home(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    local_bin = tmp_path / ".local/bin"
    local_bin.mkdir(parents=True)

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["CLAWHUB_HOME"] = str(home)

    r1 = subprocess.run(
        [sys.executable, str(INSTALL_BIN), "--home", str(home), "-v"],
        env=env, capture_output=True, text=True,
    )
    assert r1.returncode in (0, 5)

    r2 = subprocess.run(
        [sys.executable, str(INSTALL_BIN), "--home", str(home), "-v"],
        env=env, capture_output=True, text=True,
    )
    assert r2.returncode in (0, 5)
    assert "already installed" in r2.stdout or "erpclaw installed" in r2.stdout

    # Cleanup
    for c in [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), local_bin]:
        t = c / "erpclaw"
        if t.is_symlink():
            existing = Path(os.readlink(t))
            if not existing.is_absolute():
                existing = t.parent / existing
            if existing.resolve() == (home / "skills/erpclaw/bin/erpclaw").resolve():
                t.unlink()


def test_install_missing_shim_returns_2(tmp_path):
    home = tmp_path / "openclaw"
    home.mkdir()
    result = subprocess.run(
        [sys.executable, str(INSTALL_BIN), "--home", str(home)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "shim not found" in result.stderr


# -----------------------------------------------------------------------------
# uninstall_bin.py
# -----------------------------------------------------------------------------


def test_uninstall_no_symlinks_returns_0(tmp_path):
    home = setup_installable_home(tmp_path)
    result = subprocess.run(
        [sys.executable, str(UNINSTALL_BIN), "--home", str(home), "-v"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "nothing to uninstall" in result.stdout


def test_install_then_uninstall_roundtrip(tmp_path, monkeypatch):
    home = setup_installable_home(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    local_bin = tmp_path / ".local/bin"
    local_bin.mkdir(parents=True)

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["CLAWHUB_HOME"] = str(home)

    subprocess.run(
        [sys.executable, str(INSTALL_BIN), "--home", str(home), "-v"],
        env=env, capture_output=True, text=True,
    )
    # Confirm symlink exists somewhere
    found_any = False
    for c in [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), local_bin]:
        if (c / "erpclaw").is_symlink():
            found_any = True
    assert found_any, "install didn't create a symlink"

    uninstall = subprocess.run(
        [sys.executable, str(UNINSTALL_BIN), "--home", str(home), "-v"],
        env=env, capture_output=True, text=True,
    )
    assert uninstall.returncode == 0
    assert "removed" in uninstall.stdout

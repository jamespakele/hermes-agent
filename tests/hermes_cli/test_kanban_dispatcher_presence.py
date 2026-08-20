"""Tests for ``hermes_cli.kanban._check_dispatcher_presence`` scoping.

The kanban board is SHARED across profiles at the kanban root
(``kanban_db.kanban_home()``) and dispatched by the ROOT gateway. A CLI
``hermes kanban create`` under ``--profile X`` runs with HERMES_HOME set
to ``<root>/profiles/X``, which has no gateway of its own — so the probe
must scope itself to the kanban root, not the process home, or it prints a
false "No gateway is running" warning against a healthy dispatcher. The
dashboard plugin API passes an explicit ``hermes_home`` and keeps that
scope (it may run under a different home than the board it manages).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban as kcli
from hermes_cli import kanban_db as kb


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    """Profile-mode CLI env: HERMES_HOME points at <root>/profiles/coder."""
    home = tmp_path / ".hermes"
    (home / "profiles" / "coder").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home / "profiles" / "coder"))
    return home


def _fake_liveness(pid=4242):
    return SimpleNamespace(pid=pid, probe_error=False)


def test_cli_probe_scopes_to_kanban_root(profile_env, monkeypatch):
    """CLI (hermes_home=None) probes the kanban board root, NOT the
    profile-local process home — the exact regression that produced a false
    "No gateway is running" warning under ``--profile X``."""
    import gateway.status as gstatus
    import hermes_cli.config as cfgmod
    captured = {}

    def fake(profile_dir=None, use_cache=True):
        captured["profile_dir"] = profile_dir
        return _fake_liveness()

    monkeypatch.setattr(gstatus, "resolve_gateway_liveness", fake)
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"dispatch_in_gateway": True}},
    )
    running, message = kcli._check_dispatcher_presence()
    assert captured["profile_dir"] == kb.kanban_home() == profile_env
    assert running is True
    assert "No gateway is running" not in message


def test_explicit_hermes_home_passthrough(profile_env, monkeypatch):
    """The dashboard caller's explicit hermes_home scope is preserved."""
    import gateway.status as gstatus
    captured = {}

    def fake(profile_dir=None, use_cache=True):
        captured["profile_dir"] = profile_dir
        return _fake_liveness()

    monkeypatch.setattr(gstatus, "resolve_gateway_liveness", fake)
    target = Path("/custom/home")
    running, _ = kcli._check_dispatcher_presence(hermes_home=target)
    assert captured["profile_dir"] == target
    assert running is True

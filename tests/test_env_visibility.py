"""Tests for environment visibility handling.

Cross-project DB Apps must run on the dd-<engine>-app image. That image is only
usable from other projects if the environment is NOT Private — otherwise Domino
falls back to the target project's default DSE (no /opt/dd) and boot fails with
"/opt/dd/app.sh: No such file or directory".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import domino_api as dapi  # noqa: E402


def test_set_visibility_omits_owner_when_absent(monkeypatch):
    captured = {}
    monkeypatch.setattr(dapi, "_post", lambda path, json=None: captured.update(path=path, body=json))

    dapi.set_environment_visibility("env-123", visibility="Global")

    assert captured["path"] == "/v4/environments/env-123/visibility"
    # ownerId must be omitted (not sent as null) for Global.
    assert captured["body"] == {"visibility": "Global"}
    assert "ownerId" not in captured["body"]


def test_set_visibility_includes_owner_when_given(monkeypatch):
    captured = {}
    monkeypatch.setattr(dapi, "_post", lambda path, json=None: captured.update(body=json))

    dapi.set_environment_visibility("env-123", owner_id="org-456", visibility="Organization")

    assert captured["body"] == {"visibility": "Organization", "ownerId": "org-456"}


def test_build_env_never_creates_private():
    """Regression guard: the wizard must never create engine envs Private —
    that is what caused cross-project boots to land on the default DSE. The
    build flow must request Global and set visibility explicitly."""
    import inspect
    import app
    src = inspect.getsource(app.api_build_environment)
    assert 'visibility="Private"' not in src
    assert 'visibility="Global"' in src
    assert "set_environment_visibility" in src

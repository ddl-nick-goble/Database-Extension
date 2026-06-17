"""Tests for pinning the env Dockerfile's code-pull to a commit.

Env rebuilds were silent Docker cache hits: the code-pull RUN line never
changed, so /opt/dd kept stale lifecycle code even after "rebuild". Pinning the
codeload URL + extracted dir to the wizard's current commit busts that cache and
guarantees the DB image matches the wizard's code.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

_DOCKERFILE = (
    "USER root\n"
    "RUN apt-get update && apt-get install -y postgresql-16\n"
    "RUN mkdir -p /opt/dd && curl -fsSL "
    "https://codeload.github.com/ddl-nick-goble/Database-Extension/tar.gz/refs/heads/main "
    "-o /tmp/dd-src.tar.gz && tar -xzf /tmp/dd-src.tar.gz -C /tmp && "
    "cp -r /tmp/Database-Extension-main/dbapp /opt/dd/dbapp && "
    "cp -r /tmp/Database-Extension-main/snapshotter /opt/dd/snapshotter && "
    "cp /tmp/Database-Extension-main/dbapp/app.sh /opt/dd/app.sh\n"
)

_SHA = "835a64432126b1d704b1646263f576c6c7763dcd"


class _FakeProc:
    def __init__(self, out):
        self.stdout = out


def test_pin_rewrites_url_and_paths(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(_SHA + "\n"))
    patched, ref = app._pin_dockerfile_to_commit(_DOCKERFILE)

    assert ref == _SHA[:10]
    # No reference to the floating branch survives.
    assert "refs/heads/main" not in patched
    assert "Database-Extension-main" not in patched
    # URL and EVERY extracted-dir reference now carry the SHA (no matter how
    # many there are in the real Dockerfile).
    assert f"tar.gz/{_SHA}" in patched
    expected = _DOCKERFILE.count("Database-Extension-main")
    assert patched.count(f"Database-Extension-{_SHA}") == expected


def test_pin_falls_back_to_cachebust_when_git_fails(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("git not found")
    monkeypatch.setattr(subprocess, "run", _boom)

    patched, ref = app._pin_dockerfile_to_commit(_DOCKERFILE)
    # Still on main, but the /opt/dd layer is forced to rebuild.
    assert ref.startswith("main")
    assert "dd-cachebust=" in patched
    # The cachebust must precede the code-pull so that layer can't be cached.
    assert patched.index("dd-cachebust=") < patched.index("codeload.github.com")


def test_pin_rejects_non_sha_output(monkeypatch):
    # e.g. a detached message or empty output must not be treated as a SHA.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc("not-a-sha\n"))
    patched, ref = app._pin_dockerfile_to_commit(_DOCKERFILE)
    assert ref.startswith("main")
    assert "dd-cachebust=" in patched


def test_pin_is_noop_safe_on_unrelated_dockerfile(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(_SHA))
    df = "USER root\nRUN echo hello\n"
    patched, ref = app._pin_dockerfile_to_commit(df)
    # Nothing to pin; must not corrupt the file.
    assert patched == df

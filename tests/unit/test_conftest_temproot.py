"""The tmpfs temp root in tests/conftest.py, and every fallback out of it.

The mechanism is worth testing because its failure mode is invisible: a silent fallback
to pytest's default temp root costs roughly 6x on this suite and reports nothing. Each
fallback below is a separate reason to decline, and every one of them must land on
pytest's own default rather than on a half-configured path.

The root conftest is loaded by path rather than by name: pytest registers it in
sys.modules as the bare basename ``conftest``, which tests/property/conftest.py also
answers to, so the bare name is not a stable handle on the module under test.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

_CONFTEST_PATH = Path(__file__).resolve().parents[1] / "conftest.py"


def _load_root_conftest() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_wj_root_conftest", _CONFTEST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def conftest() -> ModuleType:
    return _load_root_conftest()


def _config(basetemp: Path | None) -> pytest.Config:
    """The two attributes pytest_configure actually reads, and nothing else."""
    return cast(pytest.Config, SimpleNamespace(option=SimpleNamespace(basetemp=basetemp)))


def _usage(free: int) -> Any:
    return SimpleNamespace(total=free * 2, used=free, free=free)


# --- _tmpfs_temproot: the decision ------------------------------------------------


def test_returns_the_root_when_it_is_a_healthy_directory(
    conftest: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(conftest, "TMPFS_ROOT", tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _usage(conftest.MIN_FREE_BYTES * 4))

    assert conftest._tmpfs_temproot() == tmp_path


def test_declines_when_the_root_is_absent(
    conftest: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """macOS and Windows have no /dev/shm at all."""
    monkeypatch.setattr(conftest, "TMPFS_ROOT", tmp_path / "definitely-not-here")

    assert conftest._tmpfs_temproot() is None


def test_declines_when_the_root_is_not_a_directory(
    conftest: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    regular_file = tmp_path / "not-a-dir"
    regular_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(conftest, "TMPFS_ROOT", regular_file)

    assert conftest._tmpfs_temproot() is None


def test_declines_when_the_root_cannot_be_stat_ed(
    conftest: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tmpfs whose free space cannot be read is not one to write databases to."""
    monkeypatch.setattr(conftest, "TMPFS_ROOT", tmp_path)

    def refuse(_path: object) -> None:
        raise OSError("stat refused")

    monkeypatch.setattr(shutil, "disk_usage", refuse)

    assert conftest._tmpfs_temproot() is None


def test_declines_when_free_space_is_below_the_floor(
    conftest: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(conftest, "TMPFS_ROOT", tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _usage(conftest.MIN_FREE_BYTES - 1))

    assert conftest._tmpfs_temproot() is None


def test_the_floor_is_an_inclusive_lower_bound(
    conftest: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exactly at the floor is enough; the comparison must not be off by one.

    Paired with the test above, this is the case that distinguishes `<` from `<=`.
    """
    monkeypatch.setattr(conftest, "TMPFS_ROOT", tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _usage(conftest.MIN_FREE_BYTES))

    assert conftest._tmpfs_temproot() == tmp_path


# --- pytest_configure: who wins --------------------------------------------------


def test_an_explicit_basetemp_wins(
    conftest: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    monkeypatch.setattr(conftest, "TMPFS_ROOT", tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _usage(conftest.MIN_FREE_BYTES * 4))

    conftest.pytest_configure(_config(basetemp=Path("/somewhere/chosen")))

    assert "PYTEST_DEBUG_TEMPROOT" not in os.environ


def test_a_preset_temproot_wins(
    conftest: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", "/somewhere/chosen")
    monkeypatch.setattr(conftest, "TMPFS_ROOT", tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _usage(conftest.MIN_FREE_BYTES * 4))

    conftest.pytest_configure(_config(basetemp=None))

    assert os.environ["PYTEST_DEBUG_TEMPROOT"] == "/somewhere/chosen"


def test_the_temproot_is_set_when_nothing_else_claimed_it(
    conftest: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    monkeypatch.setattr(conftest, "TMPFS_ROOT", tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _usage(conftest.MIN_FREE_BYTES * 4))

    conftest.pytest_configure(_config(basetemp=None))

    assert os.environ["PYTEST_DEBUG_TEMPROOT"] == str(tmp_path)


def test_no_temproot_is_invented_when_the_platform_has_none(
    conftest: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole point of every `return None` above: fall back, never half-configure."""
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    monkeypatch.setattr(conftest, "TMPFS_ROOT", tmp_path / "definitely-not-here")

    conftest.pytest_configure(_config(basetemp=None))

    assert "PYTEST_DEBUG_TEMPROOT" not in os.environ


# --- and the thing it is all for -------------------------------------------------


def test_tmp_path_really_lands_on_tmpfs(
    conftest: ModuleType, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """End to end: the live run's tmp_path is under the memory-backed root.

    Every test above exercises the decision with a stand-in root, so all of them would
    still pass if the hook were never wired into the real session. This is the one that
    fails when it is not.

    It keys off ``config.whiskeyjack_temproot_source`` rather than off the environment,
    because the environment cannot tell the two cases apart. A shell exporting
    ``PYTEST_DEBUG_TEMPROOT=/dev/shm`` -- which is what .claude/settings.local.json does
    on this project -- puts the temp root on tmpfs whether the hook runs or not, so an
    env-based assertion here passes against a hook whose body has been deleted. Observed,
    not theorized: it silently swallowed a mutation on 2026-08-17. When the hook did not
    make the choice this skips and says which branch did, instead of quietly agreeing.
    """
    source = getattr(request.config, "whiskeyjack_temproot_source", None)
    assert source is not None, "pytest_configure did not run at all"
    if source != "hook":
        pytest.skip(f"temp root not chosen by the hook for this run: {source}")

    root = conftest._tmpfs_temproot()
    assert os.environ.get("PYTEST_DEBUG_TEMPROOT") == str(root)
    assert str(tmp_path).startswith(str(root) + os.sep)

"""Explicit dependency-drift check (M0-002).

The v1 spec pins forecasting-tools to an exact version whose interfaces were
verified by hand (decision D26). This test is the seam the CI quality gate
(M0-003, Codex-owned) wires in: any drift is a red build, not a silent upgrade.
"""

import importlib.metadata

PINNED_FORECASTING_TOOLS = "0.2.92"


def test_forecasting_tools_exact_pin() -> None:
    installed = importlib.metadata.version("forecasting-tools")
    assert installed == PINNED_FORECASTING_TOOLS, (
        f"forecasting-tools=={installed} installed but v1 pins "
        f"{PINNED_FORECASTING_TOOLS}; upgrades require the contract-test "
        "review gate (D26), not a lockfile bump."
    )


def test_idna_is_a_declared_dependency() -> None:
    """idna is imported directly by research.model, so it must be declared.

    It arrived transitively (httpx) long before M1-301 imported it, which is
    exactly the situation this guards: an undeclared transitive import keeps
    working right up until the intermediate drops it, and then fails as a
    missing module at validation time rather than at install time.
    """
    requires = importlib.metadata.requires("whiskeyjack-bot") or []
    assert any(req.split(";")[0].strip().startswith("idna") for req in requires), (
        "research.model imports idna for IDNA hostname validation, but it is not "
        "in the project's declared dependencies"
    )

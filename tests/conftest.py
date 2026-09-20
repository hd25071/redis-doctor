from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import rdconfig  # noqa: E402
from faultlab.runner import FaultLab  # noqa: E402
from sandbox.cluster import SimCluster  # noqa: E402
from tools.context import ToolContext  # noqa: E402


@pytest.fixture
def settings() -> rdconfig.Settings:
    config = rdconfig.Settings()
    config.backend = "sandbox"
    config.approval_required = True
    config.data_dir = "data/test"
    return config


@pytest.fixture
def cluster() -> SimCluster:
    return SimCluster()


@pytest.fixture
def lab(cluster: SimCluster) -> FaultLab:
    return FaultLab(cluster, REPO_ROOT / "faultlab" / "scenarios")


@pytest.fixture
def ctx(settings: rdconfig.Settings, cluster: SimCluster) -> ToolContext:
    return ToolContext(settings=settings, cluster=cluster, kb_index=None)


@pytest.fixture
def registry(ctx: ToolContext):
    return ctx.build_registry(include_kb=False)

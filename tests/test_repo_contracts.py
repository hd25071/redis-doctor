"""Repository-level contracts that the README promises."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_readme_has_the_required_sections() -> None:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for heading in ("失败案例", "评测", "安全", "快速开始", "架构", "部署", "开发"):
        assert heading in text, f"README is missing the '{heading}' section"


def test_readme_evaluation_numbers_match_the_committed_results() -> None:
    """The README table must not drift from eval/results/latest.json."""
    import json

    latest = REPO_ROOT / "eval" / "results" / "latest.json"
    if not latest.exists():
        return
    data = json.loads(latest.read_text(encoding="utf-8"))
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for key, agg in data["aggregates"].items():
        assert f"{agg['top1']:.1%}" in readme, f"README is missing {key} top-1 {agg['top1']}"


def test_scenarios_declare_both_sandbox_and_kubectl_paths() -> None:
    for path in sorted((REPO_ROOT / "faultlab" / "scenarios").glob("S*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["inject_sim"] and data["recover_sim"], path.name
        assert data["inject_kubectl"] and data["recover_kubectl"], path.name
        assert data["required_signals"], path.name
        assert data["category"], path.name


def test_no_plaintext_secrets_in_the_repository() -> None:
    """Cheap stand-in for gitleaks in the unit-test layer."""
    import re

    patterns = {
        "openai-style key": re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
        "aws key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "committed demo password": re.compile(r"password:\s*s3cr3t-rd-demo\b"),
        "kubeconfig credentials": re.compile(
            r"(client-key-data|certificate-authority-data):\s*[A-Za-z0-9+/=]{40,}"
        ),
    }
    suspicious = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or any(
            part in {".venv", ".git", "data", "__pycache__", "results"} for part in path.parts
        ):
            continue
        if path.suffix.lower() not in {".py", ".yaml", ".yml", ".md", ".toml", ".json", ".sh"}:
            continue
        # Local-only artefacts (git-ignored): a kubeconfig exported for a test
        # run legitimately exists on disk. What must never happen is committing
        # it, which `test_no_committed_kubeconfig` covers via git ls-files.
        if path.name in {"k3s.yaml"} or path.suffix == ".kubeconfig":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in patterns.items():
            if not pattern.search(text):
                continue
            if path.name in {"test_repo_contracts.py", "test_safety.py", "test_tools.py"}:
                continue  # these files exist to test redaction
            if path.name in {"secret.example.yaml", ".env.example"}:
                continue
            suspicious.append(f"{path.relative_to(REPO_ROOT)}: {label}")
    assert not suspicious, suspicious


def test_no_committed_kubeconfig() -> None:
    """A kubeconfig carries cluster credentials; check git, not the filesystem."""
    import subprocess

    tracked = subprocess.run(  # noqa: S603
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    ).stdout.splitlines()
    offenders = [name for name in tracked if "kubeconfig" in name or name.endswith("k3s.yaml")]
    assert not offenders, offenders

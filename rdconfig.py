"""Runtime configuration, read from the environment (see .env.example).

Plain ``os.environ`` rather than a settings framework: the surface is small and
every field maps to one documented variable, which keeps the deployment
manifests and the code easy to cross-check.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def load_dotenv(path: Path | str | None = None) -> None:
    """Minimal .env loader (does not overwrite variables already set)."""
    env_path = Path(path) if path else REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


@dataclass
class Settings:
    # LLM
    llm_provider: str = "reference"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.0
    llm_max_output_tokens: int = 2000
    # scope
    namespace: str = "demo"
    instance: str = "demo"
    backend: str = "sandbox"
    # budgets
    max_steps: int = 12
    max_tool_calls: int = 30
    max_seconds: float = 180.0
    approval_required: bool = True
    variant: str = "D"
    # real-cluster wiring
    kubeconfig: str = ""
    prom_url: str = ""
    redis_acl_user: str = "doctor"
    redis_acl_password: str = ""
    # api
    webhook_token: str = ""
    alert_cooldown_seconds: int = 900
    data_dir: str = "data"
    # paths
    kb_index: str = "knowledge/index/index.json"
    scenarios_dir: str = "faultlab/scenarios"
    results_dir: str = "eval/results"
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        return cls(
            llm_provider=os.environ.get("RD_LLM_PROVIDER", "reference"),
            llm_base_url=os.environ.get("RD_LLM_BASE_URL", "https://api.openai.com/v1"),
            llm_api_key=os.environ.get("RD_LLM_API_KEY", os.environ.get("OPENAI_API_KEY", "")),
            llm_model=os.environ.get("RD_LLM_MODEL", "gpt-4o-mini"),
            llm_temperature=_float("RD_LLM_TEMPERATURE", 0.0),
            llm_max_output_tokens=_int("RD_LLM_MAX_OUTPUT_TOKENS", 2000),
            namespace=os.environ.get("RD_NAMESPACE", "demo"),
            instance=os.environ.get("RD_REDIS_INSTANCE", "demo"),
            backend=os.environ.get("RD_BACKEND", "sandbox"),
            max_steps=_int("RD_MAX_STEPS", 12),
            max_tool_calls=_int("RD_MAX_TOOL_CALLS", 30),
            max_seconds=_float("RD_MAX_SECONDS", 180.0),
            approval_required=_bool("RD_APPROVAL_REQUIRED", True),
            variant=os.environ.get("RD_VARIANT", "D").upper(),
            kubeconfig=os.environ.get("RD_KUBECONFIG", ""),
            prom_url=os.environ.get("RD_PROM_URL", ""),
            redis_acl_user=os.environ.get("RD_REDIS_ACL_USER", "doctor"),
            redis_acl_password=os.environ.get("RD_REDIS_ACL_PASSWORD", ""),
            webhook_token=os.environ.get("RD_WEBHOOK_TOKEN", ""),
            alert_cooldown_seconds=_int("RD_ALERT_COOLDOWN_SECONDS", 900),
            data_dir=os.environ.get("RD_DATA_DIR", "data"),
            kb_index=os.environ.get("RD_KB_INDEX", "knowledge/index/index.json"),
            scenarios_dir=os.environ.get("RD_SCENARIOS_DIR", "faultlab/scenarios"),
            results_dir=os.environ.get("RD_RESULTS_DIR", "eval/results"),
        )

    def resolve(self, relative: str) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else REPO_ROOT / path

    def redacted(self) -> dict[str, str]:
        out = {}
        for key, value in self.__dict__.items():
            if any(word in key for word in ("key", "password", "token")) and value:
                out[key] = "<redacted>"
            else:
                out[key] = value
        return out

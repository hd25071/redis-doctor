"""Regenerate docs/demo-trajectory.md from a real demo run.

The trajectory in the docs is not hand-written: run this script and it captures
the actual CLI output for one injected fault, so the README can point at an
artefact that is reproducible rather than illustrative.

    python scripts/capture_demo.py --scenario S02 --variant D
"""

from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import rdctl  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="S02")
    parser.add_argument("--variant", default="D")
    parser.add_argument("--output", default="docs/demo-trajectory.md")
    args = parser.parse_args()

    argv = ["demo", "--scenario", args.scenario, "--variant", args.variant, "--no-approval"]
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = rdctl.main(argv)
    output = buffer.getvalue()

    header = (
        f"# 演示实录：{args.scenario}（{args.variant} 组）\n\n"
        "由 `python scripts/capture_demo.py "
        f"--scenario {args.scenario} --variant {args.variant}` 生成，"
        "内容是一次真实运行的完整输出（含工具轨迹、证据、结论）。\n\n"
        "```text\n"
    )
    path = REPO_ROOT / args.output
    path.write_text(header + output + "```\n", encoding="utf-8")
    print(f"wrote {path} (exit code {code})")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

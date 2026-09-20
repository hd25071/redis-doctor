"""redis-doctor command line.

Cross-platform entry point used by the Makefile, CI and the demo script:

    python rdctl.py demo --scenario S02 --variant D
    python rdctl.py eval --runs 3
    python rdctl.py report
    python rdctl.py serve --port 8099
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import rdconfig
from agent.graph import DiagnosisGraph
from agent.llm import build_llm
from agent.reference import ReferenceReasoner
from agent.variants import VARIANTS
from eval.harness import EvalConfig, run_evaluation
from eval.report import load_results, render_markdown
from faultlab.runner import FaultLab
from sandbox.cluster import SimCluster
from tools.context import ToolContext


def _settings() -> rdconfig.Settings:
    return rdconfig.Settings.from_env()


def _lab(settings: rdconfig.Settings) -> FaultLab:
    cluster = SimCluster(settings.namespace, settings.instance)
    return FaultLab(cluster, settings.resolve(settings.scenarios_dir))


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------
def cmd_scenarios(args: argparse.Namespace) -> int:
    settings = _settings()
    lab = _lab(settings)
    if args.action == "list":
        print(f"{'ID':<4} {'类别':<24} {'hold-out':<9} {'名称'}")
        for scenario in lab.all():
            print(
                f"{scenario.id:<4} {scenario.category:<24} "
                f"{'yes' if scenario.held_out else 'no':<9} {scenario.name}"
            )
        return 0
    scenario = lab.get(args.scenario_id)
    print(json.dumps(scenario.model_dump(), ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------
def cmd_demo(args: argparse.Namespace) -> int:
    settings = _settings()
    settings.approval_required = args.approval
    lab = _lab(settings)
    scenario = lab.inject(args.scenario)
    variant = VARIANTS[args.variant]
    llm = build_llm(
        settings.llm_provider,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
        cassette_path=str(settings.resolve("eval/cassettes/demo.jsonl")),
    )
    reasoner = ReferenceReasoner(variant) if llm is None else _llm_reasoner(variant, llm)
    ctx = ToolContext(settings=settings, cluster=lab.cluster)
    graph = DiagnosisGraph(
        ctx,
        variant,
        reasoner=reasoner,
        approver=_interactive_approver if settings.approval_required else None,
        settings=settings,
    )
    run = graph.run(scenario.alert)
    _print_run(scenario, run)
    lab.recover(args.scenario)
    print(f"\n恢复完成，集群干净: {lab.is_clean()}")
    return 0 if (run.report and run.report.root_cause == scenario.category) else 1


def _llm_reasoner(variant, llm):
    from agent.llm_reasoner import LLMReasoner

    return LLMReasoner(variant, llm)


def _interactive_approver(request, state) -> bool:
    print("\n=== 审批闸门 ===")
    print(f"动作   : {request.action.action}")
    print(f"等级   : {request.action.tier} / 风险 {request.action.risk}")
    print(f"目标   : {request.action.target}")
    print(f"命令   : {request.action.command}")
    print(f"理由   : {request.action.rationale}")
    answer = input("批准执行? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _print_run(scenario, run) -> None:
    state = run.state
    print("=" * 78)
    print(f"场景 {scenario.id} — {scenario.name}   (标准答案: {scenario.category})")
    print("=" * 78)
    print("\n--- 告警 ---")
    print(scenario.alert.strip())
    print("\n--- triage ---")
    print(
        f"对象: ns={state.triage.namespace} instance={state.triage.instance} "
        f"pods={state.triage.pods} 症状={state.triage.symptom_class}"
    )
    print("\n--- 假设 ---")
    for hypothesis in state.hypotheses:
        print(
            f"  [{hypothesis.status:<10}] {hypothesis.category:<24} "
            f"conf={hypothesis.confidence:<5} needs={hypothesis.needs}"
        )
    print("\n--- 工具调用轨迹 ---")
    for call in run.trace:
        flags = []
        if call["blocked"]:
            flags.append(f"BLOCKED({call['block_reason']})")
        if call["truncated"]:
            flags.append("truncated")
        if call["redacted"]:
            flags.append("redacted")
        print(
            f"  {call['call_id']} {call['tool']}({json.dumps(call['args'], ensure_ascii=False)}) "
            f"-> signals={call['signals']} {' '.join(flags)}"
        )
        for line in (call.get("output") or "").splitlines()[:4]:
            print(f"      | {line[:150]}")
    report = run.report
    print("\n--- 结论 ---")
    if report is None:
        print("  (没有生成结论)")
    else:
        print(f"根因      : {report.root_cause} ({report.root_cause_label})")
        print(f"置信度    : {report.confidence}")
        print(f"摘要      : {report.summary}")
        print("证据:")
        for ref in report.evidence:
            print(f"  - {ref.call_id} {ref.tool} [{ref.signal}] {ref.quote[:120]}")
        print("排除项:")
        for ruled in report.ruled_out:
            print(f"  - {ruled.category}: {ruled.reason}")
        print("建议动作:")
        for action in report.suggested_actions:
            print(f"  - [{action.tier}/{action.risk}] {action.action}")
        if report.uncertainty:
            print("不确定性:")
            for note in report.uncertainty:
                print(f"  - {note}")
    print("\n--- 预算与安全 ---")
    print(f"步数/工具调用/耗时: {state.budget}")
    print(f"审计: {json.dumps(state.audit, ensure_ascii=False)}")
    if state.injection_hits:
        print(f"检测到提示注入: {state.injection_hits}")
    print(
        f"状态: {state.status}"
        + (f" / 审批: {state.approval.status}" if state.approval else "")
        + (f" / 执行验证: {state.execution.verified}" if state.execution else "")
    )


# ---------------------------------------------------------------------------
# eval
# ---------------------------------------------------------------------------
def cmd_eval(args: argparse.Namespace) -> int:
    settings = _settings()
    config = EvalConfig(
        variants=[v.strip().upper() for v in args.variants.split(",") if v.strip()],
        runs=args.runs,
        scenario_ids=args.scenarios.split(",") if args.scenarios else [],
        approval_required=not args.no_approval,
        budget_pressure=args.budget_pressure,
        backend=args.backend,
        policy=args.policy,
        model=args.model,
    )
    result = run_evaluation(config, settings)
    out = (
        Path(args.out)
        if args.out
        else settings.resolve(f"{settings.results_dir}/eval-{int(result.meta['started_at'])}.json")
    )
    result.write(out)
    data = result.to_json()
    markdown = render_markdown(data)
    print(markdown)
    print(f"\n结果写入: {out}")
    # Only a complete ablation becomes the published latest run, so a
    # single-variant experiment can never silently replace the README's table.
    if set(config.variants) >= {"A", "B", "C", "D"}:
        latest = settings.resolve(f"{settings.results_dir}/latest.json")
        latest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        settings.resolve(f"{settings.results_dir}/RESULTS.md").write_text(
            markdown, encoding="utf-8"
        )
        print(
            f"         {latest}\n         {settings.resolve(settings.results_dir) / 'RESULTS.md'}"
        )
    unauthorized = sum(a["unauthorized_attempts"] for a in data["aggregates"].values())
    if unauthorized:
        print(f"!! 越权操作次数 = {unauthorized}（验收要求 0）")
        return 2
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def cmd_report(args: argparse.Namespace) -> int:
    settings = _settings()
    path = (
        Path(args.input) if args.input else settings.resolve(f"{settings.results_dir}/latest.json")
    )
    if not path.exists():
        print(f"找不到结果文件: {path}（先运行 python rdctl.py eval）", file=sys.stderr)
        return 1
    data = load_results(path)
    markdown = render_markdown(data)
    settings.resolve(f"{settings.results_dir}/RESULTS.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


# ---------------------------------------------------------------------------
# knowledge base
# ---------------------------------------------------------------------------
def cmd_kb(args: argparse.Namespace) -> int:
    from knowledge.build_index import build_index

    if args.action == "build":
        output, chunks = build_index()
        print(f"indexed {len(chunks)} chunks -> {output}")
        return 0
    settings = _settings()
    kb = ToolContext(settings=settings).knowledge_base()
    query = " ".join(args.query)
    for chunk, score in kb.search(query, args.top_k):
        print(f"{score:>8.3f}  {chunk.chunk_id:<24} {chunk.category:<24} {chunk.title}")
    return 0


# ---------------------------------------------------------------------------
# cluster utilities
# ---------------------------------------------------------------------------
def cmd_cluster(args: argparse.Namespace) -> int:
    if args.action == "rbac-check":
        from deploy_checks import rbac_checks

        return rbac_checks()
    from deploy_checks import cluster_checks

    return cluster_checks()


def cmd_backup(args: argparse.Namespace) -> int:
    from ops_backup import backup

    return backup()


def cmd_clean(args: argparse.Namespace) -> int:
    settings = _settings()
    removed = []
    for pattern in ("*.sqlite3", "*.tmp.json"):
        for path in settings.resolve(settings.data_dir).glob(pattern):
            path.unlink()
            removed.append(str(path))
    print("已清理: " + (", ".join(removed) if removed else "无"))
    return 0


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------
def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from api.app import create_app

    application = create_app()
    uvicorn.run(application, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rdctl", description="redis-doctor")
    sub = parser.add_subparsers(dest="command", required=True)

    scenarios = sub.add_parser("scenarios", help="list or show faultlab scenarios")
    scenarios.add_argument("action", choices=["list", "show"], nargs="?", default="list")
    scenarios.add_argument("scenario_id", nargs="?", default="S01")
    scenarios.set_defaults(func=cmd_scenarios)

    demo = sub.add_parser("demo", help="inject one fault and diagnose it end to end")
    demo.add_argument("--scenario", "-s", default="S02")
    demo.add_argument("--variant", "-v", default="D", choices=sorted(VARIANTS))
    demo.add_argument("--no-approval", dest="approval", action="store_false", default=True)
    demo.set_defaults(func=cmd_demo)

    evaluation = sub.add_parser("eval", help="run the ablation matrix")
    evaluation.add_argument("--runs", type=int, default=3)
    evaluation.add_argument("--variants", default="A,B,C,D")
    evaluation.add_argument("--scenarios", default="")
    evaluation.add_argument("--policy", default=None)
    evaluation.add_argument("--model", default="")
    evaluation.add_argument("--backend", default="sandbox", choices=["sandbox", "real"])
    evaluation.add_argument("--budget-pressure", action="store_true")
    evaluation.add_argument("--no-approval", action="store_true")
    evaluation.add_argument("--out", default="")
    evaluation.set_defaults(func=cmd_eval)

    report = sub.add_parser("report", help="render RESULTS.md from a results json")
    report.add_argument("--input", default="")
    report.set_defaults(func=cmd_report)

    kb = sub.add_parser("kb", help="build or query the handbook index")
    kb.add_argument("action", choices=["build", "search"], nargs="?", default="build")
    kb.add_argument("query", nargs="*", default=["副本", "链路", "down"])
    kb.add_argument("--top-k", type=int, default=5)
    kb.set_defaults(func=cmd_kb)

    cluster = sub.add_parser("cluster", help="cluster and RBAC checks (read-only)")
    cluster.add_argument("action", choices=["check", "rbac-check"], nargs="?", default="check")
    cluster.set_defaults(func=cmd_cluster)

    backup = sub.add_parser("backup", help="back up trajectory db and knowledge index")
    backup.set_defaults(func=cmd_backup)

    clean = sub.add_parser("clean", help="remove local runtime state")
    clean.set_defaults(func=cmd_clean)

    serve = sub.add_parser("serve", help="run the FastAPI service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8099)
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page; the reports are UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):  # pragma: no cover - non-tty
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "policy", None) is None and args.command == "eval":
        args.policy = _settings().llm_provider
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

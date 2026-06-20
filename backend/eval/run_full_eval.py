"""End-to-end fusion-ablation eval driver.

For each brief slug it:
  1. creates a project row (so persist works) and runs the real LangGraph
     pipeline headlessly, with a Langfuse callback attached so every node shows
     up as a span/trace;
  2. produces the ``opus_solo`` contestant for that run (one extra Opus call);
  3. scores all four contestants with the deterministic objective metrics and
     writes a markdown report;
  4. pushes the requirement-coverage dataset (and objective scores) to Langfuse
     so the native LLM-as-judge evaluator can run.

Designed to run INSIDE the backend container, where LANGFUSE_HOST / keys and the
LLM API keys are already in the environment:

    podman compose exec backend python -m eval.run_full_eval --slugs fintech-payments

The objective report (step 3) always runs. The Langfuse pieces (steps 1 tracing
and 4) are best-effort: if Langfuse is unreachable the report is still produced.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from app import config, db
from app.generation import _brief_text
from app.graph.build import build_graph

from . import designs, opus_solo, run_eval

logger = logging.getLogger("eval.full")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BRIEFS_DIR = Path(__file__).resolve().parent / "briefs"


def _load_brief(slug: str) -> dict[str, Any]:
    path = BRIEFS_DIR / f"{slug}.json"
    if not path.exists():
        raise FileNotFoundError(f"no brief for slug '{slug}': {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _langfuse_callbacks() -> list[Any]:
    """LangChain callback that exports the graph to Langfuse. v3 import first,
    then the legacy v2 path; empty list if the SDK/keys are unavailable."""
    for module in ("langfuse.langchain", "langfuse.callback"):
        try:
            mod = __import__(module, fromlist=["CallbackHandler"])
            return [mod.CallbackHandler()]
        except Exception:  # noqa: BLE001 — tracing must never block the eval
            continue
    logger.warning("Langfuse callback unavailable — running without tracing")
    return []


def generate_run(slug: str, brief: dict[str, Any], *, trace: bool) -> Path:
    """Create a project, run the pipeline headlessly, return the run directory."""
    brief_text = brief["brief_text"]
    title = brief.get("title") or slug.replace("-", " ").title()

    project_id = db.new_id("prj_")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, client_name, consulting_org, status,"
            " brief_json, intake_chat_json, requirements_detail, source_provenance_json,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, title, "", "", "draft", json.dumps({}), json.dumps([]),
             brief_text, json.dumps({"source_type": "eval"}), db.now(), db.now()),
        )

    project = {"title": title, "client_name": "", "consulting_org": "",
               "requirements_detail": brief_text}
    run_id = db.new_id("run_")
    run_dir = Path(config.RUNS_DIR) / run_id

    logger.info("generating run for %s (project=%s run=%s)", slug, project_id, run_id)
    graph = build_graph()
    graph.invoke(
        {"project_id": project_id, "run_id": run_id,
         "brief_text": _brief_text(project, {}), "title": title,
         "client_name": "", "consulting_org": ""},
        config={"configurable": {"thread_id": run_id},
                "callbacks": _langfuse_callbacks() if trace else []},
    )

    missing = [n for n in designs._REQUIRED if not (run_dir / f"{n}.json").exists()]
    if missing:
        raise RuntimeError(f"run {run_id} did not produce contestants: {missing}")
    logger.info("run complete: %s", run_dir)
    return run_dir


def push_to_langfuse(pairs: list[tuple[Path, str]], briefs: dict[str, dict[str, Any]],
                     *, dataset_name: str) -> None:
    """Best-effort: coverage dataset + objective scores into Langfuse."""
    from . import langfuse_sync

    try:
        client = langfuse_sync.get_client()
    except Exception:  # noqa: BLE001
        logger.warning("could not construct Langfuse client — skipping push", exc_info=True)
        return

    for run_dir, slug in pairs:
        try:
            contestants = designs.load_contestants(run_dir)
            per_contestant = run_eval.evaluate_run(run_dir, brief_slug=slug)
            langfuse_sync.push_scores(
                client, dataset_run=run_dir.name, brief_slug=slug,
                per_contestant=per_contestant,
            )
            langfuse_sync.push_coverage_dataset(
                client, dataset_name=dataset_name, brief_slug=slug,
                contestant="canonical", design=contestants["canonical"],
                checklist=briefs[slug].get("checklist", []),
            )
            logger.info("pushed Langfuse scores + coverage dataset for %s", slug)
        except Exception:  # noqa: BLE001
            logger.warning("Langfuse push failed for %s", slug, exc_info=True)

    for flush in ("flush", "shutdown"):
        try:
            getattr(client, flush)()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full APoc fusion-ablation eval")
    parser.add_argument("--slugs", nargs="+", default=["fintech-payments"],
                        help="brief slugs to generate + evaluate")
    parser.add_argument("--out", default="/data/eval/report.md", help="report.md output path")
    parser.add_argument("--dataset", default="apoc-coverage", help="Langfuse dataset name")
    parser.add_argument("--no-trace", action="store_true", help="skip Langfuse tracing on runs")
    parser.add_argument("--no-opus", action="store_true", help="skip the opus_solo contestant")
    parser.add_argument("--no-langfuse", action="store_true", help="skip Langfuse dataset/scores push")
    parser.add_argument("--no-coverage", action="store_true", help="skip the LLM coverage score")
    parser.add_argument("--no-pairwise", action="store_true", help="skip the blind pairwise judge")
    parser.add_argument("--judge-model", default="",
                        help="held-out judge for coverage + pairwise "
                             "(default: EVAL_JUDGE_MODEL or DeepSeek)")
    args = parser.parse_args()

    # Held-out judge: explicit flag > EVAL_JUDGE_MODEL > DeepSeek (different
    # family from the Anthropic contestants, and a key we already have).
    judge_model = args.judge_model or config.EVAL_JUDGE_MODEL or config.DEEPSEEK_MODEL

    briefs = {slug: _load_brief(slug) for slug in args.slugs}
    pairs: list[tuple[Path, str]] = []

    for slug in args.slugs:
        run_dir = generate_run(slug, briefs[slug], trace=not args.no_trace)
        if not args.no_opus:
            logger.info("generating opus_solo contestant for %s", slug)
            opus_solo.generate(run_dir, brief_text=briefs[slug]["brief_text"])
        pairs.append((run_dir, slug))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows: dict[str, Any] = {}
    pairwise: dict[str, Any] = {}
    for run_dir, slug in pairs:
        checklist = None if args.no_coverage else briefs[slug].get("checklist", [])
        if checklist:
            logger.info("scoring coverage for %s with judge=%s", slug, judge_model)
        rows[slug] = run_eval.evaluate_run(
            run_dir, brief_slug=slug, checklist=checklist, judge_model=judge_model,
        )
        if not args.no_pairwise:
            logger.info("running blind pairwise for %s with judge=%s", slug, judge_model)
            pairwise[slug] = run_eval.pairwise_run(run_dir, judge_model=judge_model)

    run_eval.render_report(rows, out, pairwise=pairwise)
    logger.info("report written: %s", out)
    print(out.read_text(encoding="utf-8"))

    if not args.no_langfuse:
        push_to_langfuse(pairs, briefs, dataset_name=args.dataset)

    print("\nRun dirs:")
    for run_dir, slug in pairs:
        print(f"  {slug}: {run_dir}")


if __name__ == "__main__":
    main()

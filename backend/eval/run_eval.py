"""Orchestrate the fusion ablation: load -> score -> table -> report.md.

Two layers of scoring:
  * objective, deterministic metrics (eval.metrics) — always run, no LLM;
  * optional LLM-as-judge layers (eval.coverage requirement coverage, and
    eval.judge blind position-controlled pairwise) — run only when a held-out
    ``judge_model`` is supplied, since they cost tokens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import coverage, designs, judge, metrics


def evaluate_run(
    run_dir: Path | str,
    *,
    brief_slug: str,
    checklist: list[str] | None = None,
    judge_model: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Per-contestant scores for one run.

    Objective metrics always run. If both ``checklist`` and ``judge_model`` are
    given, requirement-coverage is judged (held-out model) and merged in.
    """
    contestants = designs.load_contestants(run_dir)
    rows = {name: metrics.objective_scores(design) for name, design in contestants.items()}

    if checklist and judge_model:
        for name, design in contestants.items():
            cov = coverage.score(design, checklist, model=judge_model)
            rows[name]["coverage"] = round(cov["coverage"], 3)
            rows[name]["coverage_n"] = f"{cov['addressed']}/{cov['total']}"
    return rows


def pairwise_run(
    run_dir: Path | str,
    *,
    judge_model: str,
    anchor: str = "canonical",
) -> list[dict[str, Any]]:
    """Blind, position-controlled pairwise comparisons for one run.

    Compares the ``anchor`` (default ``canonical``) against every other loaded
    contestant. Names never reach the judge — see eval.judge.
    """
    contestants = designs.load_contestants(run_dir)
    if anchor not in contestants:
        return []
    out: list[dict[str, Any]] = []
    for other in contestants:
        if other == anchor:
            continue
        res = judge.pairwise(
            contestants[anchor], contestants[other],
            model=judge_model, a_name=anchor, b_name=other,
        )
        out.append({"a": anchor, "b": other, **res})
    return out


def render_report(
    rows: dict[str, dict[str, dict[str, Any]]],
    out_path: Path | str,
    *,
    pairwise: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    """rows = {brief_slug: {contestant: {metric: value}}} -> markdown report.

    ``pairwise`` (optional) = {brief_slug: [ {a, b, winner, consistent}, ... ]}.
    """
    out_path = Path(out_path)
    pairwise = pairwise or {}
    lines = ["# APoc Fusion Ablation — Results", ""]
    for brief, per_contestant in rows.items():
        lines.append(f"## {brief}")
        metric_names = sorted({m for s in per_contestant.values() for m in s})
        lines.append("| contestant | " + " | ".join(metric_names) + " |")
        lines.append("|" + "---|" * (len(metric_names) + 1))
        for contestant, scores in per_contestant.items():
            cells = " | ".join(str(scores.get(m, "")) for m in metric_names)
            lines.append(f"| {contestant} | {cells} |")
        lines.append("")

        matches = pairwise.get(brief)
        if matches:
            lines.append("### Blind pairwise (held-out judge, both orders)")
            lines.append("| match | winner | consistent |")
            lines.append("|---|---|---|")
            for m in matches:
                lines.append(
                    f"| {m['a']} vs {m['b']} | {m['winner']} | {m['consistent']} |"
                )
            lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:  # pragma: no cover - CLI glue
    import argparse

    from app import config

    parser = argparse.ArgumentParser(description="Run the fusion ablation eval")
    parser.add_argument("--runs", nargs="+", required=True, help="run dirs (one per brief)")
    parser.add_argument("--slugs", nargs="+", required=True, help="brief slug per run dir")
    parser.add_argument("--out", default=str(Path(config.RUNS_DIR).parent / "eval" / "report.md"))
    args = parser.parse_args()

    rows = {
        slug: evaluate_run(run, brief_slug=slug)
        for run, slug in zip(args.runs, args.slugs)
    }
    render_report(rows, args.out)
    print(f"wrote {args.out}")


if __name__ == "__main__":  # pragma: no cover
    main()

#!/usr/bin/env bash
#
# Run the entire APoc fusion-ablation eval, Langfuse included, in one command.
#
#   ./eval.sh                      # default brief: fintech-payments
#   ./eval.sh fintech-payments ml-feature-store   # one or more brief slugs
#
# Prereqs: podman + an .env with DEEPSEEK_API_KEY / ANTHROPIC_API_KEY and the
# LANGFUSE_* block (already present in this project). Everything else is
# handled here.

set -euo pipefail
cd "$(dirname "$0")"

SLUGS=("${@:-fintech-payments}")
LANGFUSE_URL="http://localhost:${LANGFUSE_WEB_PORT:-3000}"

echo "==> [1/4] Building images and starting the full stack (app + Langfuse)"
podman compose up --build -d

echo "==> [2/4] Waiting for Langfuse at ${LANGFUSE_URL} (first boot runs DB migrations, can take ~1-2 min)"
ready=""
for _ in $(seq 1 60); do
  if curl -sf "${LANGFUSE_URL}/api/public/health" >/dev/null 2>&1; then
    ready=1; echo "    Langfuse is up."; break
  fi
  sleep 5
done
if [ -z "${ready}" ]; then
  echo "    WARNING: Langfuse did not report healthy in time. Continuing anyway —"
  echo "    the objective report still works; tracing/coverage push may be skipped."
fi

echo "==> [3/4] Running the eval inside the backend container for: ${SLUGS[*]}"
podman compose exec -T backend \
  python -m eval.run_full_eval --slugs "${SLUGS[@]}" --out /data/eval/report.md

echo "==> [4/4] Copying the report out to ./eval-report.md"
if podman compose cp backend:/data/eval/report.md ./eval-report.md 2>/dev/null; then
  echo "    Wrote ./eval-report.md"
else
  cid="$(podman compose ps -q backend)"
  podman cp "${cid}:/data/eval/report.md" ./eval-report.md && echo "    Wrote ./eval-report.md"
fi

cat <<EOF

────────────────────────────────────────────────────────────────────────
Done.
  • Objective metrics table : ./eval-report.md
  • Langfuse UI / traces     : ${LANGFUSE_URL}
                               login: ${LANGFUSE_INIT_USER_EMAIL:-admin@example.com}
  • App (try it yourself)    : http://localhost:5174

MANUAL step still required for the LLM-as-judge coverage score — see notes
printed by the script / the README "Evaluation" section.
────────────────────────────────────────────────────────────────────────
EOF

#!/usr/bin/env bash
# =============================================================================
# build-push.sh — Build S2S images and push to Google Artifact Registry.
#
# Run as:  absurahio@gmail.com  (has Artifact Registry write access on
#          researchdeployments project).
#
# Usage:
#   ./k8s/build-push.sh           # auto-generates tag: <git-hash>-<YYYYMMDDHHMM>
#   ./k8s/build-push.sh v1.2.0    # use a custom tag
#
# On Windows run this inside Git Bash, WSL, or Cloud Shell.
# The script prints the IMAGE_TAG at the end — pass it to deploy.sh.
# =============================================================================
set -euo pipefail

GAR_REGION="me-central2"
GAR_PROJECT="researchdeployments"
GAR_REPO="nsk-s2s"
REGISTRY="${GAR_REGION}-docker.pkg.dev/${GAR_PROJECT}/${GAR_REPO}"

# ── Tag computation ───────────────────────────────────────────────────────────
if [[ -n "${1:-}" ]]; then
  TAG="$1"
else
  GIT_HASH=$(git rev-parse --short HEAD 2>/dev/null || echo "nogit")
  TIMESTAMP=$(date +%Y%m%d%H%M)
  TAG="${GIT_HASH}-${TIMESTAMP}"
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "============================================================"
echo "  GAR registry : ${REGISTRY}"
echo "  Image tag    : ${TAG}"
echo "  Repo root    : ${REPO_ROOT}"
echo "============================================================"

# ── Authenticate Docker with GAR ─────────────────────────────────────────────
# Switch to the account that has GAR access.
echo ""
echo "==> Configuring Docker credentials for GAR ..."
gcloud auth configure-docker "${GAR_REGION}-docker.pkg.dev" \
  --project "${GAR_PROJECT}" --quiet

# ── Ensure GAR repository exists ─────────────────────────────────────────────
echo ""
echo "==> Ensuring GAR repository '${GAR_REPO}' exists in ${GAR_REGION} ..."
gcloud artifacts repositories describe "${GAR_REPO}" \
  --location="${GAR_REGION}" \
  --project="${GAR_PROJECT}" \
  --format="value(name)" 2>/dev/null \
  || gcloud artifacts repositories create "${GAR_REPO}" \
       --repository-format=docker \
       --location="${GAR_REGION}" \
       --project="${GAR_PROJECT}" \
       --description="S2S Orchestrator images"

# ── Build helper ─────────────────────────────────────────────────────────────
build_and_push() {
  local name="$1"
  local context="$2"
  local versioned="${REGISTRY}/${name}:${TAG}"
  local latest="${REGISTRY}/${name}:latest"

  echo ""
  echo "==> [${name}] Building ..."
  docker build \
    --platform linux/amd64 \
    --tag "${versioned}" \
    --tag "${latest}" \
    "${context}"

  echo "==> [${name}] Pushing ${TAG} ..."
  docker push "${versioned}"

  echo "==> [${name}] Pushing latest ..."
  docker push "${latest}"

  echo "==> [${name}] Done — ${versioned}"
}

# ── Build all three custom images ────────────────────────────────────────────
build_and_push "s2s-agent"        "${REPO_ROOT}/agent"
build_and_push "s2s-token-server" "${REPO_ROOT}/token-server"
build_and_push "s2s-demo"         "${REPO_ROOT}/demo"

echo ""
echo "============================================================"
echo "  All images pushed successfully."
echo "  Tag: ${TAG}"
echo ""
echo "  Next step — deploy to GKE:"
echo "    IMAGE_TAG=${TAG} ./k8s/deploy.sh"
echo "============================================================"

#!/usr/bin/env bash
# =============================================================================
# setup-gar-pull-access.sh
#
# Copies the 'regcred' imagePullSecret from an existing namespace into
# elm-s2s so its pods can pull images from the researchdeployments GAR.
#
# How image pulling works in this cluster:
#   - ar-puller@researchdeployments.iam.gserviceaccount.com has
#     roles/artifactregistry.reader on researchdeployments.
#   - A Kubernetes Secret named 'regcred' (type dockerconfigjson) holds a
#     JSON key for that SA.
#   - This same secret exists in nlp-rag, asr-serving, elm-tts — every
#     namespace that pulls from researchdeployments GAR.
#   - elm-s2s needs an identical copy.
#
# This script is idempotent: if regcred already exists in elm-s2s it is
# left unchanged (kubectl apply is a no-op when the data matches).
#
# Run as mebnouf@elm.sa (has kubectl access to the nsk cluster).
# =============================================================================
set -euo pipefail

SOURCE_NS="nlp-rag"
TARGET_NS="elm-s2s"
SECRET_NAME="regcred"

echo "============================================================"
echo "  Copying '${SECRET_NAME}' from ${SOURCE_NS} → ${TARGET_NS}"
echo "============================================================"
echo ""

# Ensure namespace exists
kubectl apply -f "$(dirname "$0")/namespace.yaml"

# Check if secret already exists in target namespace
if kubectl get secret "${SECRET_NAME}" -n "${TARGET_NS}" &>/dev/null; then
  echo "✓ '${SECRET_NAME}' already exists in ${TARGET_NS}. Nothing to do."
  exit 0
fi

# Copy secret, stripping cluster-managed metadata fields
kubectl get secret "${SECRET_NAME}" -n "${SOURCE_NS}" -o json \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['metadata'] = {'name': '${SECRET_NAME}', 'namespace': '${TARGET_NS}'}
d.pop('status', None)
print(json.dumps(d))
" \
  | kubectl apply -f -

echo ""
echo "✓ Done. '${SECRET_NAME}' is now available in ${TARGET_NS}."
echo "  Pods using imagePullSecrets: [{name: regcred}] can pull from"
echo "  me-central2-docker.pkg.dev/researchdeployments/"
set -euo pipefail

GAR_PROJECT="researchdeployments"
GAR_REGION="me-central2"
GAR_REPO="nsk-s2s"
REQUIRED_ROLE="roles/artifactregistry.reader"

# GKE nodes use the Compute Engine default SA of hajj-umrah-nsk-dev.
# Project number verified via:
#   gcloud projects describe hajj-umrah-nsk-dev --format="value(projectNumber)"
NODE_SA="184617603524-compute@developer.gserviceaccount.com"
MEMBER="serviceAccount:${NODE_SA}"

# ── Validate we are running as the right account ─────────────────────────────
ACTIVE_ACCOUNT=$(gcloud config get-value account 2>/dev/null)
echo "============================================================"
echo "  Active gcloud account : ${ACTIVE_ACCOUNT}"
echo "  Node SA to grant      : ${NODE_SA}"
echo "  GAR project           : ${GAR_PROJECT}"
echo "  Required role         : ${REQUIRED_ROLE}"
echo "============================================================"
echo ""

if [[ "${ACTIVE_ACCOUNT}" != "absurahio@gmail.com" ]]; then
  echo "WARNING: Active account is '${ACTIVE_ACCOUNT}'."
  echo "  This script needs absurahio@gmail.com to read/write IAM on ${GAR_PROJECT}."
  echo "  Switch with:  gcloud config set account absurahio@gmail.com"
  echo ""
  read -rp "Continue anyway? [y/N] " cont
  [[ "${cont}" =~ ^[Yy]$ ]] || exit 1
fi

# ── Check if binding already exists ──────────────────────────────────────────
echo "==> Checking existing IAM bindings on ${GAR_PROJECT} ..."

EXISTING=$(gcloud projects get-iam-policy "${GAR_PROJECT}" \
  --flatten="bindings[].members" \
  --filter="bindings.role=${REQUIRED_ROLE} AND bindings.members=${MEMBER}" \
  --format="value(bindings.members)" 2>/dev/null || true)

if [[ -n "${EXISTING}" ]]; then
  echo ""
  echo "✓ Access already granted."
  echo "  ${MEMBER}"
  echo "  already has ${REQUIRED_ROLE} on ${GAR_PROJECT}."
  echo ""
  echo "No action needed — GKE nodes can already pull from GAR."
  exit 0
fi

# ── Binding missing — grant it ───────────────────────────────────────────────
echo ""
echo "  Binding not found. Granting ${REQUIRED_ROLE} ..."
echo ""

gcloud projects add-iam-policy-binding "${GAR_PROJECT}" \
  --member="${MEMBER}" \
  --role="${REQUIRED_ROLE}" \
  --condition=None

echo ""
echo "✓ Done. ${NODE_SA}"
echo "  now has ${REQUIRED_ROLE} on ${GAR_PROJECT}."
echo ""
echo "GKE nodes in hajj-umrah-nsk-dev can now pull images from:"
echo "  ${GAR_REGION}-docker.pkg.dev/${GAR_PROJECT}/${GAR_REPO}/"

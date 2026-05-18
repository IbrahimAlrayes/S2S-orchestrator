#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Apply S2S manifests to the nsk GKE cluster (elm-s2s namespace).
#
# Run as:  mebnouf@elm.sa  (has GKE deployment access on hajj-umrah-nsk-dev).
#
# Prerequisites:
#   1. k8s/secret.yaml created from secret.yaml.template with real values.
#   2. Images already pushed via k8s/build-push.sh.
#
# Usage:
#   ./k8s/deploy.sh                     # deploy / update with :latest images
#   IMAGE_TAG=abc1234-202505171430 \
#     ./k8s/deploy.sh                   # pin to a specific tag
#
# On Windows run inside Git Bash, WSL, or Cloud Shell.
# =============================================================================
set -euo pipefail

IMAGE_TAG="${IMAGE_TAG:-latest}"
REGISTRY="me-central2-docker.pkg.dev/researchdeployments/nsk-s2s"
NAMESPACE="elm-s2s"
CLUSTER="nsk"
REGION="me-central2"
PROJECT="hajj-umrah-nsk-dev"
K8S_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "============================================================"
echo "  Cluster    : ${CLUSTER} (${REGION})"
echo "  Project    : ${PROJECT}"
echo "  Namespace  : ${NAMESPACE}"
echo "  Image tag  : ${IMAGE_TAG}"
echo "============================================================"

# ── Pre-flight: secret.yaml must exist ───────────────────────────────────────
if [[ ! -f "${K8S_DIR}/secret.yaml" ]]; then
  echo ""
  echo "ERROR: k8s/secret.yaml not found."
  echo "  Copy k8s/secret.yaml.template → k8s/secret.yaml"
  echo "  Fill in every REPLACE_WITH_* placeholder, then re-run."
  exit 1
fi

# ── Pre-flight: warn if placeholders remain ───────────────────────────────────
if grep -q "REPLACE_WITH" "${K8S_DIR}/secret.yaml"; then
  echo ""
  echo "WARNING: k8s/secret.yaml still contains REPLACE_WITH_* placeholders."
  echo "  Update all values before deploying."
  echo ""
  read -rp "Continue anyway? [y/N] " confirm
  [[ "${confirm}" =~ ^[Yy]$ ]] || exit 1
fi

# ── Configure kubectl credentials ────────────────────────────────────────────
echo ""
echo "==> Fetching cluster credentials ..."
gcloud container clusters get-credentials "${CLUSTER}" \
  --region "${REGION}" \
  --project "${PROJECT}"

# ── Apply manifests in dependency order ──────────────────────────────────────
echo ""
echo "==> Applying namespace ..."
kubectl apply -f "${K8S_DIR}/namespace.yaml"

echo "==> Applying ConfigMap ..."
kubectl apply -f "${K8S_DIR}/configmap.yaml"

echo "==> Applying Secret ..."
kubectl apply -f "${K8S_DIR}/secret.yaml"

echo "==> Applying Redis ..."
kubectl apply -f "${K8S_DIR}/redis/"

echo "==> Applying LiveKit ..."
kubectl apply -f "${K8S_DIR}/livekit/"

echo "==> Applying Agent ..."
kubectl apply -f "${K8S_DIR}/agent/"

echo "==> Applying Token Server ..."
kubectl apply -f "${K8S_DIR}/token-server/"

echo "==> Applying Demo Frontend ..."
kubectl apply -f "${K8S_DIR}/demo/"

echo "==> Applying Ingress ..."
kubectl apply -f "${K8S_DIR}/ingress.yaml"

# ── Pin image tags if a specific tag was requested ───────────────────────────
if [[ "${IMAGE_TAG}" != "latest" ]]; then
  echo ""
  echo "==> Updating deployments to image tag: ${IMAGE_TAG} ..."
  kubectl set image -n "${NAMESPACE}" \
    deployment/s2s-agent \
    s2s-agent="${REGISTRY}/s2s-agent:${IMAGE_TAG}"

  kubectl set image -n "${NAMESPACE}" \
    deployment/s2s-token-server \
    s2s-token-server="${REGISTRY}/s2s-token-server:${IMAGE_TAG}"

  kubectl set image -n "${NAMESPACE}" \
    deployment/s2s-demo \
    s2s-demo="${REGISTRY}/s2s-demo:${IMAGE_TAG}"
fi

# ── Status summary ───────────────────────────────────────────────────────────
echo ""
echo "==> Waiting for rollouts ..."
kubectl rollout status deployment/redis          -n "${NAMESPACE}" --timeout=120s
kubectl rollout status deployment/livekit-server -n "${NAMESPACE}" --timeout=120s
kubectl rollout status deployment/s2s-agent      -n "${NAMESPACE}" --timeout=300s
kubectl rollout status deployment/s2s-token-server -n "${NAMESPACE}" --timeout=120s
kubectl rollout status deployment/s2s-demo       -n "${NAMESPACE}" --timeout=120s

echo ""
echo "============================================================"
echo "  Deployment complete."
echo ""
echo "  Pods:"
kubectl get pods -n "${NAMESPACE}"
echo ""
echo "  Services / External IPs:"
kubectl get svc -n "${NAMESPACE}"
echo ""
echo "============================================================"
echo ""
echo "POST-DEPLOY CHECKLIST"
echo "---------------------"
echo "1. Note the EXTERNAL-IP of 'livekit-server' (TCP LoadBalancer)."
echo "   Update LIVEKIT_PUBLIC_URL in k8s/secret.yaml:"
echo "     wss://<LIVEKIT_EXTERNAL_IP>:7880"
echo ""
echo "2. If GKE nodes have no individual external IPs (Cloud NAT cluster),"
echo "   also update 'use_external_ip: false' and set 'node_ip' to the"
echo "   livekit-server-udp LoadBalancer IP inside the livekit.yaml block"
echo "   in k8s/secret.yaml, then re-apply:"
echo "     kubectl apply -f k8s/secret.yaml"
echo "     kubectl rollout restart deployment/livekit-server -n ${NAMESPACE}"
echo ""
echo "3. After updating the secret, restart token-server and demo:"
echo "     kubectl rollout restart deployment/s2s-token-server deployment/s2s-demo -n ${NAMESPACE}"
echo ""
echo "4. Add a DNS A record pointing your domain to 34.54.112.102 (Ingress IP)."
echo "   Then uncomment the ManagedCertificate block in k8s/ingress.yaml"
echo "   and re-apply to enable HTTPS."
echo "============================================================"

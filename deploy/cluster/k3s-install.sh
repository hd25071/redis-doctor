#!/usr/bin/env bash
# Install single-node k3s for the long-lived demo host (form B in the plan).
#
# Nothing in this repository runs this automatically: it is a documented,
# copy-pasteable step so that a stranger can reproduce the environment.
set -euo pipefail

echo "== installing k3s (China mirror) =="
curl -sfL https://rancher-mirror.rancher.cn/k3s/k3s-install.sh | INSTALL_K3S_MIRROR=cn sh -

echo "== waiting for the node to be ready =="
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
for _ in $(seq 1 60); do
  kubectl get node >/dev/null 2>&1 && break
  sleep 2
done
kubectl wait --for=condition=Ready node --all --timeout=180s

echo "== installing the mirror configuration =="
sudo mkdir -p /etc/rancher/k3s
sudo cp "$(dirname "$0")/registries.yaml.example" /etc/rancher/k3s/registries.yaml
echo "edit /etc/rancher/k3s/registries.yaml, then: sudo systemctl restart k3s"

echo "== namespaces =="
kubectl create namespace demo --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace redis-doctor --dry-run=client -o yaml | kubectl apply -f -

echo "next: make operator-up demo-redis monitoring-up"


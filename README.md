# server-worker-set

A Kubernetes operator (KOPF / Python) that manages a **1-server + N-workers** topology as a single custom resource, scalable via KEDA.

## Architecture

The unit of scale is a **complete (server + N-worker) instance**. `spec.replicas` controls how many instances exist; `spec.workersPerInstance` is fixed.

```
ServerWorkerSet CR  (spec.replicas=2, spec.workersPerInstance=4)
├── Instance 0
│   ├── StatefulSet  <name>-0-server  (1 pod)
│   │   └── Headless Service <name>-0-server
│   └── StatefulSet  <name>-0-worker  (4 pods)
│       └── Headless Service <name>-0-worker
└── Instance 1
    ├── StatefulSet  <name>-1-server  (1 pod)
    │   └── Headless Service <name>-1-server
    └── StatefulSet  <name>-1-worker  (4 pods)
        └── Headless Service <name>-1-worker
```

### DNS addresses (injected per-instance into every container)

Each instance's pods receive env vars scoped to **that instance only**:

| Env var | Value for instance `i` |
|---|---|
| `SERVER_ADDRESS` | `<name>-<i>-server-0.<name>-<i>-server.<ns>.svc.cluster.local` |
| `WORKERS_ADDRESSES` | comma-separated list of all worker FQDNs for this instance |

`WORKERS_ADDRESSES` example for `workersPerInstance=3`, instance `0`:
```
<name>-0-worker-0.<name>-0-worker.<ns>.svc.cluster.local,<name>-0-worker-1.<name>-0-worker.<ns>.svc.cluster.local,<name>-0-worker-2.<name>-0-worker.<ns>.svc.cluster.local
```

## Scale subresource (KEDA wiring)

The CRD exposes `/scale` with:
- `specReplicasPath: .spec.replicas` — KEDA writes here to add/remove whole instances
- `statusReplicasPath: .status.replicas` — number of **fully ready** instances (server + all workers ready)
- `labelSelectorPath: .status.selector` — label selector covering all pods in this CR

A KEDA `ScaledObject` targeting this CR looks like:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: my-llm-job-scaler
spec:
  scaleTargetRef:
    apiVersion: kalavai.net/v1
    kind: ServerWorkerSet
    name: my-llm-job
  minReplicaCount: 1
  maxReplicaCount: 8
  triggers:
    - type: prometheus
      metadata:
        serverAddress: http://prometheus:9090
        metricName: queue_depth
        threshold: "5"
        query: avg(queue_depth{job="my-llm-job"})
```

## Quick start

```bash
# Install CRD
kubectl apply -f crd.yaml

# Run operator locally (dev)
pip install -r requirements.txt
kopf run server_worker_set/set_operator.py --namespace default

# Apply an example resource
kubectl apply -f example_cr.yaml
```

## Deletion

Deleting the `ServerWorkerSet` CR automatically garbage-collects all child StatefulSets and Services via Kubernetes `ownerReferences`.


## Autoscaling

Use KEDA for autoscaling based on metrics like queue depth or request rate: https://github.com/kedacore/keda

Use HTTP scaler from KEDA to scale based on HTTP requests: https://github.com/kedacore/http-add-on/blob/main/docs/install.md

Install KEDA

helm repo add kedacore https://kedacore.github.io/charts
helm repo update

Alternatively, install keda with yaml:

kubectl apply --server-side -f https://github.com/kedacore/keda/releases/download/v2.19.0/keda-2.19.0.yaml

Then install HTTP addon:

helm install http-add-on kedacore/keda-add-ons-http --namespace keda
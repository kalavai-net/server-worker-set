# server-worker-set

A Kubernetes operator (KOPF / Python) that manages a **1-server + N-workers** topology as a single custom resource, scalable via KEDA.

## Requirements

- KEDA HTTP add-on is required for autoscaling.
- Ingress controller required for external access via ingress.

## Architecture

The unit of scale is a **complete (server + N-worker) instance**. `spec.replicas` controls how many instances exist; `spec.workersPerInstance` is fixed.

```
ServerWorkerSet CR  (spec.replicas=2, spec.workersPerInstance=4)
├── Service  <name>-service  (selects all server pods)
├── Instance 0
│   ├── StatefulSet  <name>-0-server  (1 pod)
│   │   └── Headless Service <name>-0-server  (stable pod DNS)
│   └── StatefulSet  <name>-0-worker  (4 pods)
│       └── Headless Service <name>-0-worker
└── Instance 1
    ├── StatefulSet  <name>-1-server  (1 pod)
    │   └── Headless Service <name>-1-server  (stable pod DNS)
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


## Gang scheduling with volcano

When you apply a Deployment (or StatefulSet) with the scheduling.volcano.sh/group-min-member annotation, Volcano automatically creates a PodGroup resource. This PodGroup is responsible for enforcing the gang scheduling constraints for the pods belonging to your workload.


## Failure Handling Policies

Define per-pod-type policies to handle failures automatically. Configure `serverPolicy` and `workerPolicy` arrays with event-action pairs.

### Available Events

| Event | Description |
|-------|-------------|
| `PodFailed` | Pod phase is Failed |
| `PodEvicted` | Pod was evicted (node pressure, spot termination, etc.) |
| `CrashLoopBackOff` | Container is repeatedly crashing |
| `ImagePullBackOff` | Cannot pull container image (back-off) |
| `ErrImagePull` | Error pulling container image |
| `CreateContainerError` | Failed to create container |
| `OOMKilled` | Container killed due to out of memory |
| `Unknown` | Pod phase is Unknown |

### Available Actions

| Action | Description |
|--------|-------------|
| `RestartInstance` | Delete and recreate **all** StatefulSets for the instance (server + all workers) |
| `ReplacePod` | Delete only the offending pod (StatefulSet will automatically recreate it) |

### Example

```yaml
spec:
  serverPolicy:
    - event: PodFailed
      action: RestartInstance
    - event: OOMKilled
      action: ReplacePod
  workerPolicy:
    - event: PodFailed
      action: ReplacePod
    - event: CrashLoopBackOff
      action: ReplacePod
```

See `examples/policy_example.yaml` for a complete configuration.
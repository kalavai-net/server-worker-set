# Server-worker-set for Kubernetes

A Kubernetes operator (KOPF / Python) that manages a **1-server + N-workers** topology as a single custom resource, scalable via KEDA.

## Why

The ServerWorkerSet is useful for applications that require a server-worker architecture where the server and workers need to be scaled together as a unit. An example of this is a multi-node LLM deployment with vLLM, SGLang or llama.cpp, where one server instance needs to connect directly with multiple workers; scaling such deployments requires coordinated scaling of one server and N workers (1-N scaling).

Coordinated 1-N scaling and the ability to autoscale based on workload are the core functionalities of the ServerWorkerSet. It's also what sets it apart from other similar projects:

- [Volcano Job](https://volcano.sh/en/docs/vcjob/): great for batch processing but not designed to autoscale.
- [LeaderWorkerSet](https://github.com/kubernetes-sigs/lws): focuses on leader-election and coordination but doesn't provide the same level of autoscaling and load balancing capabilities.


## Core features

- **1-N scaling**: scale 1 server and N workers together as a unit.
- **Autoscaling**: implements the /scale endpoint, compatible with KEDA scale triggers.
- **Load balancing via single service**: all instances share the same service for client connections.
- **Recovery policies**: configurable restart policies for server and worker pods.

## Requirements

- KEDA and its HTTP add-on are required for autoscaling.

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

## Install from local repo

```bash
helm install my-release ./chart
```

## Install from published chart

```bash
helm repo add server-worker-set https://kalavai-net.github.io/server-worker-set/
helm repo update

helm install my-release server-worker-set
```

## Repo requirements

To publish the chart to the repo, you need to have the following requirements:
- GitHub Pages enabled in the repository settings
- A `gh-pages` branch in the repository
- A secret GH_RELEASER_TOKEN in the repository secrets with a valid GitHub token


## Deletion

Deleting the `ServerWorkerSet` CR automatically garbage-collects all child StatefulSets and Services via Kubernetes `ownerReferences`.


## Autoscaling

Only required if `spec.autoScaling.enabled` is set to `true`.

[KEDA](https://github.com/kedacore/keda) and the [HTTP addon](https://github.com/kedacore/http-add-on) are required for autoscaling. Install them:

```bash
helm repo add kedacore https://kedacore.github.io/charts
helm repo update
helm install keda kedacore/keda --namespace keda --create-namespace --set nodeSelector."kalavai/role"=server --set operator.nodeSelector."kalavai/role"=server --set webhooks.nodeSelector."kalavai/role"=server --set metricsServer.nodeSelector."kalavai/role"=server
```

Then install HTTP addon (using affinity and replicas to prefer server nodes):

```bash
helm install http-add-on kedacore/keda-add-ons-http --namespace keda --values httpaddon_values.yaml
```

Uninstall:

```bash
helm uninstall keda --namespace keda
helm uninstall http-add-on --namespace keda
kubectl delete namespace keda
```

Force delete namespace (otherwise resources may hang and fail subsequent reinstalls):

```bash
kubectl get namespace keda -o json | jq '.spec.finalizers = []' | kubectl replace --raw "/api/v1/namespaces/keda/finalize" -f -
kubectl delete namespace keda
```

### Test autoscaling

Autoscaling works with both ingress and NodePort services. When using NodePort, requests have to be tagged with the host to match one of the hosts described in hosts.

Example:

```yaml
apiVersion: kalavai.net/v1
kind: ServerWorkerSet
spec:
  service:
    port: 8080
    targetPort: 8080
    type: NodePort
  autoScaling:
    enabled: true
    hosts: # Hosts to monitor and route requests to the server service in the ServerWorkerSet
      - test.kalavai.net
  ...
```

Then add the appropriate header to your request:

```bash
curl -H "Host: test.kalavai.net" <clusterIP>:8080
```

When using an ingress, the host is automatically set and thus requests do not require the host header. This is recommended for production.

Example:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: my-sws-ingress
spec:
  ingressClassName: traefik
  rules:
  - host: test.kalavai.net # This must match one of the hosts in the ServerWorkerSet
    http:
      paths:
      - backend:
          service:
            name: my-sws-service
            port:
              number: 8080 # match internal port
        path: /
```


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

See `examples/serverworkerset.yaml` for a complete configuration.



## TODO

- Support for gang scheduling
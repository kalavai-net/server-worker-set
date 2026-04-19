import kopf
import kubernetes
import copy
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GROUP = "kalavai.net"
VERSION = "v1"
PLURAL = "serverworkersets"

# Label key used to mark all children of a ServerWorkerSet
OWNER_LABEL = "serverworkerset"


# ---------------------------------------------------------------------------
# Per-instance naming helpers
# ---------------------------------------------------------------------------

def _inst_server_sts(cr_name: str, idx: int) -> str:
    return f"{cr_name}-{idx}-server"


def _inst_worker_sts(cr_name: str, idx: int) -> str:
    return f"{cr_name}-{idx}-worker"


def _inst_server_svc(cr_name: str, idx: int) -> str:
    return f"{cr_name}-{idx}-server"


def _inst_worker_svc(cr_name: str, idx: int) -> str:
    return f"{cr_name}-{idx}-worker"


def _global_server_svc(cr_name: str) -> str:
    return f"{cr_name}-service"


def _inst_server_address(cr_name: str, idx: int, namespace: str) -> str:
    """Stable DNS for the server pod of instance idx."""
    sts = _inst_server_sts(cr_name, idx)
    svc = _inst_server_svc(cr_name, idx)
    return f"{sts}-0.{svc}.{namespace}.svc.cluster.local"



def _inst_server_labels(cr_name: str, idx: int) -> dict:
    return {
        OWNER_LABEL: cr_name,
        "serverworkerset-instance": str(idx),
        "serverworkerset-role": "server",
    }


def _inst_worker_labels(cr_name: str, idx: int) -> dict:
    return {
        OWNER_LABEL: cr_name,
        "serverworkerset-instance": str(idx),
        "serverworkerset-role": "worker",
    }


# Label key shared by ALL server pods of a CR (used by the global service selector)
GLOBAL_SERVER_ROLE_LABEL = "serverworkerset-role"


# ---------------------------------------------------------------------------
# Kubernetes object builders
# ---------------------------------------------------------------------------

def _inject_dns_env(
    pod_spec: dict,
    server_addr: str,
    workers_addresses: str,
) -> dict:
    """Inject SERVER_ADDRESS and WORKERS_ADDRESSES into every container."""
    pod_spec = copy.deepcopy(pod_spec)
    dns_env = [
        {"name": "SERVER_ADDRESS", "value": server_addr},
        {"name": "WORKERS_ADDRESSES", "value": workers_addresses},
    ]
    for container in pod_spec.get("containers", []):
        existing = {e["name"] for e in container.get("env", [])}
        for ev in dns_env:
            if ev["name"] not in existing:
                container.setdefault("env", []).append(ev)
    return pod_spec


def _build_headless_service(
    svc_name: str, namespace: str, selector: dict
) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": svc_name,
            "namespace": namespace,
            "labels": selector,
        },
        "spec": {
            "clusterIP": "None",
            "selector": selector,
            "ports": [{"name": "placeholder", "port": 1, "targetPort": 1}],
        },
    }


def _build_global_service(
    svc_name: str,
    namespace: str,
    selector: dict,
    port: int,
    target_port: int,
    sticky: bool,
    sticky_timeout: int,
    service_type: str = "ClusterIP",
) -> dict:
    spec: dict = {
        "type": service_type,
        "selector": selector,
        "ports": [{"name": "app", "port": port, "targetPort": target_port}],
    }
    if sticky:
        spec["sessionAffinity"] = "ClientIP"
        spec["sessionAffinityConfig"] = {
            "clientIP": {"timeoutSeconds": sticky_timeout}
        }
    else:
        spec["sessionAffinity"] = "None"
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": svc_name,
            "namespace": namespace,
            "labels": selector,
        },
        "spec": spec,
    }


def _build_statefulset(
    sts_name: str,
    namespace: str,
    replicas: int,
    selector: dict,
    service_name: str,
    pod_spec: dict,
) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {
            "name": sts_name,
            "namespace": namespace,
            "labels": selector,
        },
        "spec": {
            "replicas": replicas,
            "serviceName": service_name,
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {"labels": selector},
                "spec": pod_spec,
            },
        },
    }


# ---------------------------------------------------------------------------
# Kubernetes API helpers
# ---------------------------------------------------------------------------

class _API:
    def __init__(self):
        self.core = kubernetes.client.CoreV1Api()
        self.apps = kubernetes.client.AppsV1Api()
        self.custom = kubernetes.client.CustomObjectsApi()


def _apply_object(api: _API, obj: dict, body):
    """Adopt the object under the CR, then create or replace it."""
    kopf.adopt(obj, owner=body)
    kind = obj["kind"]
    ns = obj["metadata"]["namespace"]
    obj_name = obj["metadata"]["name"]
    try:
        if kind == "Service":
            existing = api.core.read_namespaced_service(obj_name, ns)
            obj["metadata"]["resourceVersion"] = existing.metadata.resource_version
            api.core.replace_namespaced_service(obj_name, ns, obj)
        elif kind == "StatefulSet":
            existing = api.apps.read_namespaced_stateful_set(obj_name, ns)
            obj["metadata"]["resourceVersion"] = existing.metadata.resource_version
            api.apps.replace_namespaced_stateful_set(obj_name, ns, obj)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            if kind == "Service":
                api.core.create_namespaced_service(ns, obj)
            elif kind == "StatefulSet":
                api.apps.create_namespaced_stateful_set(ns, obj)
        else:
            raise


def _delete_if_exists(api: _API, kind: str, obj_name: str, namespace: str):
    try:
        if kind == "Service":
            api.core.delete_namespaced_service(obj_name, namespace)
        elif kind == "StatefulSet":
            api.apps.delete_namespaced_stateful_set(obj_name, namespace)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            raise


# ---------------------------------------------------------------------------
# Core reconciliation logic
# ---------------------------------------------------------------------------

def _reconcile_instance(
    api: _API,
    cr_name: str,
    idx: int,
    namespace: str,
    workers_per_instance: int,
    server_pod_spec: dict,
    worker_pod_spec: dict,
    body,
    global_mode: bool = False,
):
    """Ensure the StatefulSets and Services for instance `idx` exist and are up to date."""
    server_addr = _inst_server_address(cr_name, idx, namespace)
    svc = _inst_worker_svc(cr_name, idx)
    workers_addresses = ",".join(
        f"{_inst_worker_sts(cr_name, idx)}-{j}.{svc}.{namespace}.svc.cluster.local"
        for j in range(workers_per_instance)
    )

    srv_spec = _inject_dns_env(server_pod_spec, server_addr, workers_addresses)
    wkr_spec = _inject_dns_env(worker_pod_spec, server_addr, workers_addresses)

    server_sel = _inst_server_labels(cr_name, idx)
    worker_sel = _inst_worker_labels(cr_name, idx)

    objs = [
        _build_headless_service(_inst_server_svc(cr_name, idx), namespace, server_sel),
        _build_headless_service(_inst_worker_svc(cr_name, idx), namespace, worker_sel),
        _build_statefulset(
            _inst_server_sts(cr_name, idx), namespace, 1, server_sel,
            _inst_server_svc(cr_name, idx),
            srv_spec,
        ),
        _build_statefulset(
            _inst_worker_sts(cr_name, idx), namespace, workers_per_instance, worker_sel,
            _inst_worker_svc(cr_name, idx), wkr_spec,
        ),
    ]
    for obj in objs:
        _apply_object(api, obj, body)


def _delete_instance(api: _API, cr_name: str, idx: int, namespace: str):
    """Delete all resources for instance `idx`."""
    for kind, obj_name in [
        ("StatefulSet", _inst_server_sts(cr_name, idx)),
        ("StatefulSet", _inst_worker_sts(cr_name, idx)),
        ("Service", _inst_server_svc(cr_name, idx)),
        ("Service", _inst_worker_svc(cr_name, idx)),
    ]:
        _delete_if_exists(api, kind, obj_name, namespace)
    logger.info("Deleted instance %d of %s/%s", idx, namespace, cr_name)


# ---------------------------------------------------------------------------
# KOPF handlers
# ---------------------------------------------------------------------------

@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
def reconcile(spec, name, namespace, body, patch, **kwargs):
    desired_instances = spec.get("replicas", 1)
    workers_per_instance = spec.get("workersPerInstance", 1)
    server_pod_spec = dict(spec["serverPodSpec"])
    worker_pod_spec = dict(spec["workerPodSpec"])

    svc_spec = spec.get("serverService", {})
    global_mode = svc_spec.get("mode", "per-instance") == "global"
    svc_port = svc_spec.get("port", 8080)
    svc_target_port = svc_spec.get("targetPort", svc_port)
    svc_sticky = svc_spec.get("stickySession", False)
    svc_sticky_timeout = svc_spec.get("stickySessionTimeoutSeconds", 10800)
    svc_type = svc_spec.get("serviceType", "ClusterIP")

    api = _API()

    # In global mode, reconcile the single shared ClusterIP service first
    if global_mode:
        global_selector = {
            OWNER_LABEL: name,
            "serverworkerset-role": "server",
        }
        global_svc = _build_global_service(
            _global_server_svc(name), namespace, global_selector,
            svc_port, svc_target_port, svc_sticky, svc_sticky_timeout,
            service_type=svc_type,
        )
        _apply_object(api, global_svc, body)
    else:
        # Ensure any leftover global service is removed when switching back to per-instance
        _delete_if_exists(api, "Service", _global_server_svc(name), namespace)

    # Reconcile desired instances
    instance_status = []
    for idx in range(desired_instances):
        _reconcile_instance(
            api, name, idx, namespace, workers_per_instance,
            server_pod_spec, worker_pod_spec, body,
            global_mode=global_mode,
        )
        instance_status.append({
            "index": idx,
            "serverAddress": _inst_server_address(name, idx, namespace),
        })

    # Delete instances beyond desired count (scale-down)
    # Detect orphaned StatefulSets by listing with the owner label
    label_selector = f"{OWNER_LABEL}={name}"
    existing_sts = api.apps.list_namespaced_stateful_set(
        namespace, label_selector=label_selector
    )
    existing_indices = set()
    for sts in existing_sts.items:
        raw_idx = sts.metadata.labels.get("serverworkerset-instance")
        if raw_idx is not None:
            existing_indices.add(int(raw_idx))

    desired_set = set(range(desired_instances))
    for orphan_idx in existing_indices - desired_set:
        _delete_instance(api, name, orphan_idx, namespace)

    # Build label selector for KEDA (covers all pods in this CR)
    keda_selector = f"{OWNER_LABEL}={name}"

    patch.status["replicas"] = desired_instances
    patch.status["readyInstances"] = 0
    patch.status["selector"] = keda_selector
    patch.status["instances"] = instance_status

    logger.info(
        "Reconciled %s/%s: %d instance(s), %d worker(s) each",
        namespace, name, desired_instances, workers_per_instance,
    )


# ---------------------------------------------------------------------------
# Timer — keep status.replicas / readyInstances in sync
# ---------------------------------------------------------------------------

@kopf.timer(GROUP, VERSION, PLURAL, interval=15.0, idle=10.0)
def sync_status(name, namespace, spec, patch, **kwargs):
    desired_instances = spec.get("replicas", 1)
    workers_per_instance = spec.get("workersPerInstance", 1)
    print("**** SYNC STATUS ****", desired_instances)
    api = _API()

    ready_instances = 0
    for idx in range(desired_instances):
        server_sts_name = _inst_server_sts(name, idx)
        worker_sts_name = _inst_worker_sts(name, idx)
        try:
            srv = api.apps.read_namespaced_stateful_set(server_sts_name, namespace)
            wkr = api.apps.read_namespaced_stateful_set(worker_sts_name, namespace)
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                continue
            raise
        server_ready = (srv.status.ready_replicas or 0) >= 1
        workers_ready = (wkr.status.ready_replicas or 0) >= workers_per_instance
        if server_ready and workers_ready:
            ready_instances += 1

    patch.status["replicas"] = ready_instances
    patch.status["readyInstances"] = ready_instances


# ---------------------------------------------------------------------------
# Delete — GC via ownerReferences; log for visibility
# ---------------------------------------------------------------------------

@kopf.on.delete(GROUP, VERSION, PLURAL)
def on_delete(name, namespace, **kwargs):
    logger.info(
        "ServerWorkerSet %s/%s deleted — all child resources will GC via ownerReferences",
        namespace, name,
    )


# ---------------------------------------------------------------------------
# Startup — kubeconfig detection
# ---------------------------------------------------------------------------

@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **kwargs):
    kubernetes.config.load_incluster_config() if _running_in_cluster() else \
        kubernetes.config.load_kube_config()
    settings.persistence.finalizer = f"{GROUP}/finalizer"
    settings.posting.level = logging.INFO


def _running_in_cluster() -> bool:
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/token"):
            return True
    except FileNotFoundError:
        return False
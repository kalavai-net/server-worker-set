import kopf
import kubernetes
import copy
import logging
import re
from typing import Optional, List, Dict, Any

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
        self.networking = kubernetes.client.NetworkingV1Api()


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
        elif kind == "Ingress":
            existing = api.networking.read_namespaced_ingress(obj_name, ns)
            obj["metadata"]["resourceVersion"] = existing.metadata.resource_version
            api.networking.replace_namespaced_ingress(obj_name, ns, obj)
        elif kind == "HTTPScaledObject":
            group, version = "http.keda.sh", "v1alpha1"
            api.custom.get_namespaced_custom_object(group, version, ns, "httpscaledobjects", obj_name)
            api.custom.replace_namespaced_custom_object(group, version, ns, "httpscaledobjects", obj_name, obj)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            if kind == "Service":
                api.core.create_namespaced_service(ns, obj)
            elif kind == "StatefulSet":
                api.apps.create_namespaced_stateful_set(ns, obj)
            elif kind == "Ingress":
                api.networking.create_namespaced_ingress(ns, obj)
            elif kind == "HTTPScaledObject":
                group, version = "http.keda.sh", "v1alpha1"
                api.custom.create_namespaced_custom_object(group, version, ns, "httpscaledobjects", obj)
        else:
            raise


def _delete_if_exists(api: _API, kind: str, obj_name: str, namespace: str):
    try:
        if kind == "Service":
            api.core.delete_namespaced_service(obj_name, namespace)
        elif kind == "StatefulSet":
            api.apps.delete_namespaced_stateful_set(obj_name, namespace)
        elif kind == "Ingress":
            api.networking.delete_namespaced_ingress(obj_name, namespace)
        elif kind == "HTTPScaledObject":
            api.custom.delete_namespaced_custom_object(
                "http.keda.sh", "v1alpha1", namespace, "httpscaledobjects", obj_name
            )
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            raise


def _http_scaled_object_name(cr_name: str) -> str:
    return f"{cr_name}-http-scaler"


def _ingress_name(cr_name: str) -> str:
    return f"{cr_name}-ingress"


def _build_http_scaled_object(
    name: str,
    namespace: str,
    cr_name: str,
    service_name: str,
    service_port: int,
    hosts: list,
    replicas_min: int,
    replicas_max: int,
    scaledown_period: int,
    scaling_metric: dict,
) -> dict:
    obj = {
        "apiVersion": "http.keda.sh/v1alpha1",
        "kind": "HTTPScaledObject",
        "metadata": {
            "name": name,
            "namespace": namespace,
        },
        "spec": {
            "hosts": hosts,
            "pathPrefixes": ["/"],
            "scaleTargetRef": {
                "name": cr_name,
                "kind": "ServerWorkerSet",
                "apiVersion": "kalavai.net/v1",
                "service": service_name,
                "port": service_port,
            },
            "replicas": {
                "min": replicas_min,
                "max": replicas_max,
            },
            "scaledownPeriod": scaledown_period,
        },
    }
    if scaling_metric:
        obj["spec"]["scalingMetric"] = scaling_metric
    return obj


def _build_ingress(
    name: str,
    namespace: str,
    service_name: str,
    service_port: int,
    host: str,
    path: str = "/",
    path_type: str = "Prefix",
    ingress_class: str = None,
    tls_secret: str = None,
    annotations: dict = None,
) -> dict:
    metadata: dict = {"name": name, "namespace": namespace}
    if annotations:
        metadata["annotations"] = annotations
    if ingress_class:
        metadata["annotations"] = dict(metadata.get("annotations") or {})
        metadata["annotations"].setdefault("kubernetes.io/ingress.class", ingress_class)

    rule = {
        "host": host,
        "http": {
            "paths": [
                {
                    "path": path,
                    "pathType": path_type,
                    "backend": {
                        "service": {
                            "name": service_name,
                            "port": {"number": service_port},
                        }
                    },
                }
            ]
        },
    }

    spec: dict = {"rules": [rule]}
    if ingress_class:
        spec["ingressClassName"] = ingress_class
    if tls_secret:
        spec["tls"] = [{"hosts": [host], "secretName": tls_secret}]

    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": metadata,
        "spec": spec,
    }


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
    svc_port = svc_spec.get("port", 8080)
    svc_target_port = svc_spec.get("targetPort", svc_port)
    svc_sticky = svc_spec.get("stickySession", False)
    svc_sticky_timeout = svc_spec.get("stickySessionTimeoutSeconds", 10800)
    svc_type = svc_spec.get("serviceType", "ClusterIP")

    api = _API()

    # Reconcile the single shared ClusterIP service across all server instances
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

    # Reconcile desired instances
    instance_status = []
    for idx in range(desired_instances):
        _reconcile_instance(
            api, name, idx, namespace, workers_per_instance,
            server_pod_spec, worker_pod_spec, body,
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

    # -----------------------------------------------------------------------
    # Autoscaling — HTTPScaledObject
    # -----------------------------------------------------------------------
    as_spec = spec.get("autoScaling", {})
    as_enabled = as_spec.get("enabled", False)
    hso_name = _http_scaled_object_name(name)

    if as_enabled:
        as_hosts = as_spec.get("hosts", [])
        as_replicas = as_spec.get("replicas", {})
        as_min = as_replicas.get("min", 0)
        as_max = as_replicas.get("max", desired_instances)
        as_scaledown = as_spec.get("scaledownPeriod", 300)
        as_metric = as_spec.get("scalingMetric", {})
        global_svc_name = _global_server_svc(name)
        hso = _build_http_scaled_object(
            hso_name, namespace, name,
            global_svc_name, svc_port,
            as_hosts, as_min, as_max, as_scaledown, as_metric,
        )
        _apply_object(api, hso, body)
        logger.info("Reconciled HTTPScaledObject %s/%s", namespace, hso_name)
    else:
        _delete_if_exists(api, "HTTPScaledObject", hso_name, namespace)

    # -----------------------------------------------------------------------
    # Ingress
    # -----------------------------------------------------------------------
    ing_spec = spec.get("ingress", {})
    ing_enabled = ing_spec.get("enabled", False)
    ing_name = _ingress_name(name)

    if ing_enabled:
        ing_host = ing_spec.get("host", "")
        ing_path = ing_spec.get("path", "/")
        ing_path_type = ing_spec.get("pathType", "Prefix")
        ing_class = ing_spec.get("ingressClassName")
        ing_tls_secret = ing_spec.get("tls", {}).get("secretName")
        ing_annotations = ing_spec.get("annotations")
        global_svc_name = _global_server_svc(name)
        ingress = _build_ingress(
            ing_name, namespace,
            global_svc_name, svc_port,
            ing_host, ing_path, ing_path_type,
            ing_class, ing_tls_secret, ing_annotations,
        )
        _apply_object(api, ingress, body)
        logger.info("Reconciled Ingress %s/%s", namespace, ing_name)
    else:
        _delete_if_exists(api, "Ingress", ing_name, namespace)

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
# Policy Enforcement — pod event monitoring and actions
# ---------------------------------------------------------------------------

def _detect_pod_event(pod: dict) -> Optional[str]:
    """Detect the event type from pod status. Returns event name or None."""
    status = pod.get("status", {})
    phase = status.get("phase", "")
    
    # Check container statuses for detailed failure reasons
    for container_status in status.get("containerStatuses", []):
        state = container_status.get("state", {})
        
        # Check terminated state
        terminated = state.get("terminated")
        if terminated:
            exit_code = terminated.get("exitCode", 0)
            reason = terminated.get("reason", "")
            if reason == "OOMKilled" or exit_code == 137:
                return "OOMKilled"
            if reason == "Error" or exit_code != 0:
                return "PodFailed"
        
        # Check waiting state for common failure reasons
        waiting = state.get("waiting", {})
        if waiting:
            reason = waiting.get("reason", "")
            if reason == "CrashLoopBackOff":
                return "CrashLoopBackOff"
            if reason == "ImagePullBackOff":
                return "ImagePullBackOff"
            if reason == "ErrImagePull":
                return "ErrImagePull"
            if reason == "CreateContainerError":
                return "CreateContainerError"
    
    # Check pod phase
    if phase == "Failed":
        return "PodFailed"
    if phase == "Unknown":
        return "Unknown"
    
    # Check conditions for eviction
    for condition in status.get("conditions", []):
        if condition.get("type") == "DisruptionTarget" and condition.get("status") == "True":
            return "PodEvicted"
    
    return None


def _get_pod_owner_info(pod: dict) -> Optional[Dict[str, Any]]:
    """Extract ServerWorkerSet owner info from pod labels."""
    labels = pod.get("metadata", {}).get("labels", {})
    owner = labels.get(OWNER_LABEL)
    instance_idx = labels.get("serverworkerset-instance")
    role = labels.get("serverworkerset-role")
    
    if not owner or instance_idx is None or not role:
        return None
    
    return {
        "cr_name": owner,
        "instance_idx": int(instance_idx),
        "role": role,
        "namespace": pod.get("metadata", {}).get("namespace", ""),
        "pod_name": pod.get("metadata", {}).get("name", ""),
    }


def _get_crd_policies(api: _API, namespace: str, cr_name: str) -> Dict[str, List[Dict]]:
    """Fetch server and worker policies from the CRD."""
    try:
        cr = api.custom.get_namespaced_custom_object(
            GROUP, VERSION, namespace, PLURAL, cr_name
        )
        spec = cr.get("spec", {})
        return {
            "server": spec.get("serverPolicy", []),
            "worker": spec.get("workerPolicy", []),
        }
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            return {"server": [], "worker": []}
        raise


def _execute_restart_instance(api: _API, cr_name: str, instance_idx: int, namespace: str):
    """Restart entire instance: delete server and all worker pods."""
    logger.info("Executing RestartInstance for %s/%s instance %d", namespace, cr_name, instance_idx)
    
    # Delete server and worker StatefulSets (will be recreated by reconcile)
    server_sts = _inst_server_sts(cr_name, instance_idx)
    worker_sts = _inst_worker_sts(cr_name, instance_idx)
    
    try:
        # Delete the StatefulSets - they will be recreated by the reconcile loop
        api.apps.delete_namespaced_stateful_set(server_sts, namespace)
        logger.info("Deleted server StatefulSet %s", server_sts)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            logger.error("Failed to delete server StatefulSet %s: %s", server_sts, e)
    
    try:
        api.apps.delete_namespaced_stateful_set(worker_sts, namespace)
        logger.info("Deleted worker StatefulSet %s", worker_sts)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            logger.error("Failed to delete worker StatefulSet %s: %s", worker_sts, e)


def _execute_replace_pod(api: _API, pod_name: str, namespace: str, role: str, cr_name: str, instance_idx: int):
    """Replace single pod: delete it (StatefulSet will recreate)."""
    logger.info("Executing ReplacePod for %s/%s (role=%s, instance=%d)", namespace, pod_name, role, instance_idx)
    
    try:
        api.core.delete_namespaced_pod(pod_name, namespace)
        logger.info("Deleted pod %s/%s - StatefulSet will recreate", namespace, pod_name)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            logger.error("Failed to delete pod %s/%s: %s", namespace, pod_name, e)


def _apply_policy(api: _API, event: str, policies: List[Dict], owner_info: dict):
    """Check if any policy matches the event and execute the action."""
    for policy in policies:
        if policy.get("event") == event:
            action = policy.get("action")
            cr_name = owner_info["cr_name"]
            instance_idx = owner_info["instance_idx"]
            namespace = owner_info["namespace"]
            role = owner_info["role"]
            pod_name = owner_info["pod_name"]
            
            logger.info(
                "Policy matched: event=%s, action=%s for %s/%s (role=%s, instance=%d)",
                event, action, namespace, cr_name, role, instance_idx
            )
            
            if action == "RestartInstance":
                _execute_restart_instance(api, cr_name, instance_idx, namespace)
            elif action == "ReplacePod":
                _execute_replace_pod(api, pod_name, namespace, role, cr_name, instance_idx)
            return True
    return False


@kopf.on.event("v1", "pods")
def on_pod_event(body, event, **kwargs):
    """Watch for pod events and apply configured policies."""
    # Only process pods belonging to our operator
    owner_info = _get_pod_owner_info(body)
    if not owner_info:
        return
    
    # Detect what event this pod is experiencing
    detected_event = _detect_pod_event(body)
    if not detected_event:
        return
    
    api = _API()
    
    # Get policies from the parent CRD
    policies = _get_crd_policies(api, owner_info["namespace"], owner_info["cr_name"])
    
    # Apply the appropriate policy based on role (server or worker)
    role = owner_info["role"]
    role_policies = policies.get(role, [])
    
    if role_policies:
        applied = _apply_policy(api, detected_event, role_policies, owner_info)
        if applied:
            logger.info(
                "Applied %s policy for %s/%s on event %s",
                role, owner_info["namespace"], owner_info["pod_name"], detected_event
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
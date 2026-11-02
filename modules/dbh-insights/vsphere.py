"""vSphere access for the DBH Insights module.

A Python port of DBH Insights' collect/js/vim.js. Each inventory query is one
PropertyCollector call over a ContainerView, the same SOAP requests the website
sends through its helper, but made here with pyVmomi. The results are wrapped in
`Obj` so the sheet code reads like the original (vm.str("runtime.powerState")).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any, Iterable


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------- value helpers

def moref(value: Any) -> str | None:
    """Managed object reference -> its id (host-12). Plain strings pass through."""
    if value is None:
        return None
    # Never compare a pyVmomi ManagedObject with == : its __eq__ assumes the other
    # side is a ManagedObject too and raises AttributeError on a str.
    mo_id = getattr(value, "_moId", None)
    if mo_id:
        return mo_id
    return value if isinstance(value, str) and value else (None if isinstance(value, str) else str(value))


def text(value: Any) -> str | None:
    """A value as vCenter's SOAP text would show it. None and "" are None."""
    if value is None:
        return None
    if hasattr(value, "_moId"):
        return value._moId
    if isinstance(value, str):
        return value or None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    if hasattr(value, "_moId"):
        return value._moId
    return str(value)


def number(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool) or hasattr(value, "_moId"):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        n = float(str(value))
    except ValueError:
        return None
    return int(n) if n.is_integer() else n


def boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int)):
        if value in ("true", "1", 1):
            return True
        if value in ("false", "0", 0):
            return False
    return None


def at(obj: Any, path: str) -> Any:
    """Value at a slash-separated path inside a data object, or None."""
    for part in path.split("/"):
        if obj is None:
            return None
        obj = getattr(obj, part, None)
    return obj


def t(obj: Any, path: str) -> str | None:
    return text(at(obj, path))


def n(obj: Any, path: str) -> float | int | None:
    return number(at(obj, path))


def b(obj: Any, path: str) -> bool | None:
    return boolean(at(obj, path))


def xsi(obj: Any) -> str | None:
    """The concrete vSphere type name (VirtualDisk, HostFibreChannelHba...)."""
    if obj is None:
        return None
    return getattr(type(obj), "_wsdlName", None) or getattr(obj, "_wsdlName", None)


class Obj:
    """One managed object plus the properties that were asked for."""

    def __init__(self, ref: Any, props: dict[str, Any]):
        self.ref = ref
        self.moref = moref(ref) or ""
        self.props = props

    def get(self, name: str) -> Any:
        return self.props.get(name)

    def str(self, name: str) -> str | None:
        return text(self.props.get(name))

    def num(self, name: str) -> float | int | None:
        return number(self.props.get(name))

    def bool(self, name: str) -> bool | None:
        return boolean(self.props.get(name))

    def array(self, name: str) -> list[Any]:
        value = self.props.get(name)
        return list(value) if value else []


# --------------------------------------------------------------------------- live source

class LiveSource:
    """Reads one vCenter with pyVmomi."""

    def __init__(self, host: str, user: str, password: str, verify_ssl: bool):
        from pyVim.connect import SmartConnect
        from pyVmomi import vim, vmodl

        self.vim, self.vmodl = vim, vmodl
        self.host, self.user = host, user
        self.si = SmartConnect(host=host, user=user, pwd=password, disableSslCertValidation=not verify_ssl)
        self.content = self.si.RetrieveContent()
        self._types = {
            "VirtualMachine": vim.VirtualMachine,
            "HostSystem": vim.HostSystem,
            "ClusterComputeResource": vim.ClusterComputeResource,
            "Datastore": vim.Datastore,
            "Datacenter": vim.Datacenter,
            "ResourcePool": vim.ResourcePool,
            "Network": vim.Network,
            "VmwareDistributedVirtualSwitch": vim.dvs.VmwareDistributedVirtualSwitch,
            "DistributedVirtualPortgroup": vim.dvs.DistributedVirtualPortgroup,
        }

    def close(self) -> None:
        from pyVim.connect import Disconnect
        Disconnect(self.si)

    def retrieve(self, type_name: str, props: list[str]) -> list[Obj]:
        """Every object of one type, following the continuation token so a large
        inventory is never silently truncated. A property this vCenter doesn't
        know (older versions) is dropped and logged rather than failing the run."""
        vim, vmodl = self.vim, self.vmodl
        kind = self._types[type_name]
        pc = self.content.propertyCollector
        paths = list(props)
        view = self.content.viewManager.CreateContainerView(self.content.rootFolder, [kind], True)
        try:
            while True:
                spec = vmodl.query.PropertyCollector.FilterSpec(
                    objectSet=[vmodl.query.PropertyCollector.ObjectSpec(
                        obj=view, skip=True,
                        selectSet=[vmodl.query.PropertyCollector.TraversalSpec(
                            name="view", type=vim.view.ContainerView, path="view", skip=False)],
                    )],
                    propSet=[vmodl.query.PropertyCollector.PropertySpec(type=kind, pathSet=paths)],
                )
                try:
                    result = pc.RetrievePropertiesEx(specSet=[spec], options=vmodl.query.PropertyCollector.RetrieveOptions())
                    objects: list[Obj] = []
                    while result is not None:
                        for oc in result.objects or []:
                            objects.append(Obj(oc.obj, {p.name: p.val for p in (oc.propSet or [])}))
                        if not result.token:
                            break
                        result = pc.ContinueRetrievePropertiesEx(token=result.token)
                    return objects
                except vmodl.query.InvalidProperty as exc:
                    bad = getattr(exc, "name", None)
                    if not bad or bad not in paths:
                        raise
                    log(f"{type_name}: this vCenter has no property {bad}; its column stays empty")
                    paths.remove(bad)
        finally:
            try:
                view.Destroy()
            except Exception:
                pass

    def about(self) -> Any:
        return self.content.about

    def licenses(self) -> list[Any]:
        return list(self.content.licenseManager.licenses or [])

    def perf_samples(self, objects: Iterable[Obj], wanted: dict[str, str]) -> dict[str, dict[str, Any]]:
        """Newest real-time sample of each counter per entity (QueryPerf, intervalId 20)."""
        vim = self.vim
        objects = list(objects)
        if not objects:
            return {}
        pm = self.content.perfManager
        ids = {f"{c.groupInfo.key}.{c.nameInfo.key}.{c.rollupType}": c.key for c in pm.perfCounter}
        counters = {column: ids[name] for column, name in wanted.items() if name in ids}
        if not counters:
            return {}
        by_id = {cid: column for column, cid in counters.items()}
        metric_ids = [vim.PerformanceManager.MetricId(counterId=cid, instance="") for cid in counters.values()]

        def query(chunk: list[Obj]) -> list[Any]:
            specs = [vim.PerformanceManager.QuerySpec(entity=o.ref, maxSample=1, intervalId=20, metricId=metric_ids)
                     for o in chunk]
            return list(pm.QueryPerf(querySpec=specs) or [])

        results: list[Any] = []
        for i in range(0, len(objects), 200):
            chunk = objects[i:i + 200]
            try:
                results.extend(query(chunk))
            except Exception:
                # One entity without a provider can fail the batch; retry one by one.
                for o in chunk:
                    try:
                        results.extend(query([o]))
                    except Exception as exc:
                        log(f"no performance data for {o.moref}: {exc}")

        samples: dict[str, dict[str, Any]] = {}
        for metric in results:
            values: dict[str, Any] = {}
            for series in metric.value or []:
                column = by_id.get(series.id.counterId)
                if column and not series.id.instance and series.value:
                    values[column] = series.value[0]
            info = metric.sampleInfo[0] if metric.sampleInfo else None
            values["sampledAt"] = text(info.timestamp) if info else None
            samples[moref(metric.entity)] = values
        return samples

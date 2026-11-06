"""Public node settings reference existing SSH profiles; never contain credentials."""
import copy
from xuloom.core.config import load_config, update_config
from xuloom.remote.config import resolve, profile_key
from .store import identity


def positive(value, name):
    if type(value) is not int or value < 1 or value > 2147483647:
        raise ValueError(name + " must be a positive integer")
    return value


def validate(value, *, resolve_hosts=True):
    if not isinstance(value, dict) or value.keys() - {"id", "ssh", "location", "max_agents", "automatic", "reserve_percent", "connection", "model", "backend", "api_source", "api_pool", "group_width"}:
        raise ValueError("Invalid Scaling node fields")
    location=value.get("location","ssh")
    if location not in {"local","ssh"}: raise ValueError("Choose local or SSH execution")
    item = {"id": identity(value.get("id")), "location":location,"ssh":identity(value.get("ssh")) if location=="ssh" else None,
            "max_agents": positive(value.get("max_agents", 8), "Node capacity"),
            "automatic": value.get("automatic", True), "reserve_percent": value.get("reserve_percent", 25),
            "connection": value.get("connection") or None, "model": value.get("model") or None,
            "backend": value.get("backend", "native"), "api_source": value.get("api_source", "inherit"),
            "api_pool": copy.deepcopy(value.get("api_pool", [])), "group_width": positive(value.get("group_width", 50), "Group width")}
    if item["api_source"] not in {"inherit", "local", "remote"}: raise ValueError("Choose the node's API configuration source")
    if location=="local" and item["api_source"]=="remote": raise ValueError("Local nodes use local model connections")
    if not isinstance(item["api_pool"], list) or len(item["api_pool"]) > 64: raise ValueError("Choose at most 64 model bindings")
    for entry in item["api_pool"]:
        if (not isinstance(entry, dict) or entry.keys() - {"connection", "model", "backend"} or
            not isinstance(entry.get("connection"), str) or not entry["connection"] or
            entry.get("model") is not None and not isinstance(entry["model"], str) or
            entry.get("backend", "native") not in {"native", "codex", "claude", "pi", "dsh"}):
            raise ValueError("Invalid node model selection")
    if item["group_width"] > 1000: raise ValueError("Group width must be 1–1000")
    if item["id"] == "primary" or len(item["id"]) > 80:
        raise ValueError("Node name must be at most 80 characters; primary is reserved")
    if type(item["automatic"]) is not bool or type(item["reserve_percent"]) is not int or not 10 <= item["reserve_percent"] <= 80:
        raise ValueError("Choose automatic capacity and reserve 10–80 percent")
    if item["backend"] not in {"native", "codex", "claude", "pi", "dsh"}:
        raise ValueError("Unsupported Scaling backend")
    for key in ("model", "connection"):
        if item[key] is not None and (not isinstance(item[key], str) or len(item[key]) > 300 or any(ord(c) < 32 for c in item[key])):
            raise ValueError("Invalid node model binding")
    if resolve_hosts and location=="ssh":
        resolve(item["ssh"])
    return item


def nodes():
    values = load_config().get("scaling_nodes", [])
    if not isinstance(values, list):
        raise ValueError("Invalid saved Scaling nodes")
    return [validate(value) for value in values]


def public_config():
    from xuloom.remote.config import profiles
    from xuloom.core.model_catalog import connections, public_connection
    return {"nodes": nodes(), "ssh": profiles(),
            "models": [public_connection(row) for row in connections(load_config())]}


def save(values):
    if not isinstance(values, list):
        raise ValueError("Supply Scaling nodes")
    values = [validate(value) for value in values]
    if len({v["id"] for v in values}) != len(values):
        raise ValueError("Duplicate Scaling node")
    hosts = [profile_key(resolve(v["ssh"])) if v["location"]=="ssh" else "local" for v in values]
    if len(set(hosts)) != len(hosts):
        raise ValueError("The same SSH target must not be counted twice")
    def change(config):
        config["scaling_nodes"] = copy.deepcopy(values)
        return config
    update_config(change)
    return values

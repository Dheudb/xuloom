"""Keyless SSH profiles shared by desktop and CLI; OpenSSH owns authentication."""
import ntpath
import os
import re
import hashlib
import json
from pathlib import PurePosixPath

from xuloom.core.config import load_config, update_config


def validate_profile(value):
    if not isinstance(value, dict) or value.keys() - {"id", "name", "host", "port", "cwd", "python", "platform", "ssh_config"}:
        raise ValueError("Invalid SSH profile fields")
    identity = value.get("id")
    if not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", identity):
        raise ValueError("SSH connection ID must contain 1–64 letters, digits, _ or -")
    profile = {"id": identity, "name": value.get("name") or identity, "host": value.get("host"),
        "port": value.get("port"), "cwd": value.get("cwd"), "python": value.get("python") or "python3",
        "platform": value.get("platform", "posix"), "ssh_config": value.get("ssh_config") or ""}
    for key in ("name", "host", "cwd", "python", "ssh_config"):
        text = profile[key]
        if not isinstance(text, str) or len(text) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise ValueError("Invalid SSH profile field: " + key)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.@:\[\]-]{0,254}", profile["host"]):
        raise ValueError("Use an SSH host alias or user@hostname; do not enter shell commands or options")
    if profile["port"] is not None and (type(profile["port"]) is not int or not 1 <= profile["port"] <= 65535):
        raise ValueError("SSH port must be an integer from 1 to 65535")
    if not isinstance(profile["platform"], str) or profile["platform"] not in {"posix", "windows"}:
        raise ValueError("Remote platform must be posix or windows")
    if profile["ssh_config"] and not os.path.isabs(os.path.expanduser(profile["ssh_config"])):
        raise ValueError("Use an absolute path for the local SSH configuration file")
    absolute = ntpath.isabs(profile["cwd"]) and (ntpath.splitdrive(profile["cwd"])[0] != "") if profile["platform"] == "windows" else PurePosixPath(profile["cwd"]).is_absolute()
    if not absolute:
        raise ValueError("Supply an absolute directory on the remote machine")
    return profile


def profiles(config=None):
    values = (load_config() if config is None else config).get("ssh_connections", [])
    if not isinstance(values, list):
        raise ValueError("ssh_connections must be an array")
    result = [validate_profile(value) for value in values]
    if len({value["id"] for value in result}) != len(result):
        raise ValueError("Duplicate SSH connection IDs")
    return result


def resolve(identity):
    match = next((value for value in profiles() if value["id"] == identity), None)
    if match is None:
        raise ValueError("Unknown SSH connection: " + str(identity))
    return match


def target(profile):
    return {key: value for key, value in validate_profile(profile).items() if key != "name"}


def profile_key(profile):
    """Same canonical public target identity used by desktop navigation."""
    payload = json.dumps(target(profile), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def save(value):
    profile = validate_profile(value)
    def change(config):
        rows = profiles(config)
        config["ssh_connections"] = [profile if row["id"] == profile["id"] else row for row in rows]
        if not any(row["id"] == profile["id"] for row in rows): config["ssh_connections"].append(profile)
        return config
    update_config(change)
    return profile


def remove(identity):
    def change(config):
        rows = profiles(config)
        if not any(row["id"] == identity for row in rows): raise ValueError("Unknown SSH connection")
        config["ssh_connections"] = [row for row in rows if row["id"] != identity]
        return config
    update_config(change)

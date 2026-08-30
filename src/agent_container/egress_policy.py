from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any

from agent_container.state import ensure_private_directory
from agent_container.state import ensure_private_file


_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_DENIED_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".arpa")
_MAX_DOMAINS = 128
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class EgressPolicy:
    version: int
    mode: str
    additional_domains: tuple[str, ...]


def validate_domain(value: object) -> str:
    if not isinstance(value, str) or not value.isascii():
        raise ValueError("egress domain is invalid")
    if not 1 <= len(value.encode("ascii")) <= 253:
        raise ValueError("egress domain is invalid")
    if value != value.lower() or value.endswith("."):
        raise ValueError("egress domain is invalid")
    labels = value.split(".")
    if (
        len(labels) < 2
        or any(_LABEL.fullmatch(label) is None for label in labels)
        or any(label.startswith("xn--") for label in labels)
    ):
        raise ValueError("egress domain is invalid")
    if value == "localhost" or value.endswith(_DENIED_SUFFIXES):
        raise ValueError("egress domain is invalid")
    try:
        ipaddress.ip_address(value.removeprefix("[").removesuffix("]"))
    except ValueError:
        pass
    else:
        raise ValueError("egress domain is invalid")
    return value


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("egress policy JSON is invalid")
        result[key] = value
    return result


def _decode_policy(body: bytes) -> EgressPolicy:
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("egress policy JSON is invalid")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("egress policy is invalid") from None
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "mode",
        "additional_domains",
    }:
        raise ValueError("egress policy is invalid")
    version = payload["version"]
    mode = payload["mode"]
    domains = payload["additional_domains"]
    if (
        isinstance(version, bool)
        or version != 1
        or mode != "allowlist"
        or not isinstance(domains, list)
        or len(domains) > _MAX_DOMAINS
    ):
        raise ValueError("egress policy is invalid")
    validated = tuple(validate_domain(domain) for domain in domains)
    if tuple(sorted(set(validated))) != validated:
        raise ValueError("egress policy is invalid")
    return EgressPolicy(1, "allowlist", validated)


def _encode_policy(policy: EgressPolicy) -> bytes:
    return (
        json.dumps(
            {
                "version": policy.version,
                "mode": policy.mode,
                "additional_domains": list(policy.additional_domains),
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n"
    ).encode("ascii")


def load_egress_policy(path: Path) -> EgressPolicy:
    ensure_private_directory(path.parent)
    ensure_private_file(path)
    try:
        body = path.read_bytes()
    except OSError:
        raise ValueError("egress policy is invalid") from None
    if not body or len(body) > 65_536:
        raise ValueError("egress policy is invalid")
    return _decode_policy(body)


def _write_all(descriptor: int, body: bytes) -> None:
    offset = 0
    while offset < len(body):
        written = os.write(descriptor, body[offset:])
        if written <= 0:
            raise OSError("egress policy write failed")
        offset += written


def _sync_parent(parent: Path) -> None:
    descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, policy: EgressPolicy, *, exclusive: bool) -> None:
    parent = ensure_private_directory(path.parent)
    if exclusive:
        if path.exists() or path.is_symlink():
            raise FileExistsError("egress policy already exists")
    else:
        load_egress_policy(path)
    temporary = parent / f".egress-{secrets.token_hex(8)}.tmp"
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
        )
        created = True
        _write_all(descriptor, _encode_policy(policy))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if not exclusive:
            load_egress_policy(path)
        os.replace(temporary, path)
        created = False
        _sync_parent(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if created:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def enable_egress_policy(path: Path) -> EgressPolicy:
    policy = EgressPolicy(1, "allowlist", ())
    _atomic_write(path, policy, exclusive=True)
    return policy


def add_egress_domain(path: Path, domain: str) -> EgressPolicy:
    validated = validate_domain(domain)
    current = load_egress_policy(path)
    if validated in current.additional_domains:
        raise ValueError("egress domain is already configured")
    if len(current.additional_domains) >= _MAX_DOMAINS:
        raise ValueError("egress policy has too many domains")
    policy = EgressPolicy(
        1,
        "allowlist",
        tuple(sorted((*current.additional_domains, validated))),
    )
    _atomic_write(path, policy, exclusive=False)
    return policy


def remove_egress_domain(path: Path, domain: str) -> EgressPolicy:
    validated = validate_domain(domain)
    current = load_egress_policy(path)
    if validated not in current.additional_domains:
        raise ValueError("egress domain is not configured")
    policy = EgressPolicy(
        1,
        "allowlist",
        tuple(item for item in current.additional_domains if item != validated),
    )
    _atomic_write(path, policy, exclusive=False)
    return policy


def disable_egress_policy(path: Path) -> None:
    load_egress_policy(path)
    path.unlink()
    _sync_parent(path.parent)

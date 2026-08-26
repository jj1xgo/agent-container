from pathlib import Path

from agent_container.version import resolve_version


__version__ = resolve_version(Path(__file__).resolve().parents[2])

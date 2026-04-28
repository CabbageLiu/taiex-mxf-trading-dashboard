from __future__ import annotations

import importlib
import logging
import pkgutil
from importlib.metadata import entry_points

from app.strategies.base import Strategy

log = logging.getLogger("taiex.strategies")

_registry: dict[str, type[Strategy]] = {}


def register_strategy(cls: type[Strategy]) -> type[Strategy]:
    if not getattr(cls, "name", None):
        raise ValueError("Strategy subclasses must set a `name` ClassVar")
    if cls.name in _registry:
        log.warning("strategy %s already registered; overwriting", cls.name)
    _registry[cls.name] = cls
    return cls


def get(name: str) -> type[Strategy] | None:
    return _registry.get(name)


def all_strategies() -> dict[str, type[Strategy]]:
    return dict(_registry)


def discover() -> None:
    """Import in-repo examples + any external entry-point providers."""
    pkg = importlib.import_module("app.strategies.examples")
    if hasattr(pkg, "__path__"):
        for m in pkgutil.iter_modules(pkg.__path__):
            importlib.import_module(f"app.strategies.examples.{m.name}")

    for ep in entry_points(group="taiex.strategies"):
        try:
            ep.load()
        except Exception:
            log.exception("failed to load strategy entry point %s", ep.name)

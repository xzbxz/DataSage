"""Behavior probe for a Hermes plugin's public registration contract."""

from __future__ import annotations

import importlib.util
import copy
from collections.abc import Mapping
from pathlib import Path
import sys
from typing import Any


class RegistrationProbe:
    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.tools: list[dict[str, Any]] = []
        self.prompt_sections: list[dict[str, Any]] = []
        self.hooks: list[dict[str, Any]] = []
        self.config = dict(config or {})
        self.config_reads: list[str] = []

    def get_config(self, key: str, default: Any = None) -> Any:
        """Mirror the plugin-relative configuration surface of Hermes."""

        self.config_reads.append(key)
        if key not in self.config:
            return default
        return copy.deepcopy(self.config[key])

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(dict(kwargs))

    def register_system_prompt_section(
        self,
        section_id: str,
        content: str,
        **kwargs: Any,
    ) -> None:
        self.prompt_sections.append(
            {"id": section_id, "content": content, **kwargs}
        )

    def register_hook(self, *args: Any, **kwargs: Any) -> None:
        self.hooks.append({"args": args, **kwargs})


def probe_registration(
    plugin_root: Path,
    *,
    package_name: str,
    config: Mapping[str, Any] | None = None,
) -> tuple[Any, RegistrationProbe]:
    """Import and execute one plugin's real ``register(ctx)`` entry point."""

    spec = importlib.util.spec_from_file_location(
        package_name,
        plugin_root / "__init__.py",
        submodule_search_locations=[str(plugin_root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load plugin package: {plugin_root}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    probe = RegistrationProbe(config)
    module.register(probe)
    return module, probe

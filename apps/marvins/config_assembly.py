"""Config assembly logic for Marvin online configuration."""

from typing import Any

from apps.marvins.models import Marvin


def assemble_marvin_config(marvin: Marvin) -> dict[str, Any]:
    """
    Return the effective configuration dict for a Marvin.

    Merging order (last wins):
      1. Agent-reported capability_configs (from registration)
      2. MarvinConfig.global_config
      3. MarvinConfig.capability_overrides (per-capability)
      4. Attached Resource configs (injected into matching capability namespaces)
    """
    config: dict[str, Any] = {
        "global": {},
        "capabilities": {},
        "resources": [],
    }

    # 1. Base from agent-reported configs
    if marvin.capability_configs:
        for cap_name, cap_cfg in marvin.capability_configs.items():
            config["capabilities"][cap_name] = dict(cap_cfg)

    # Ensure MarvinConfig exists (auto-created via signal, but be defensive)
    marvin_config = getattr(marvin, "online_config", None)
    if marvin_config is not None:
        # 2. Global overrides
        if marvin_config.global_config:
            config["global"].update(marvin_config.global_config)

        # 3. Per-capability overrides
        if marvin_config.capability_overrides:
            for cap_name, override in marvin_config.capability_overrides.items():
                if cap_name not in config["capabilities"]:
                    config["capabilities"][cap_name] = {}
                _deep_merge(config["capabilities"][cap_name], override)

    # 4. Attached resources
    for resource in marvin.attached_resources.filter(enabled=True).select_related("resource_type"):
        resource_entry: dict[str, Any] = {
            "id": str(resource.id),
            "name": resource.name,
            "type": resource.resource_type.name,
            "config": resource.config,
        }
        config["resources"].append(resource_entry)

        # Inject resource config into matching capabilities
        for cap_name in resource.resource_type.capability_names:
            if cap_name in config["capabilities"]:
                _deep_merge(config["capabilities"][cap_name], resource.config)

    return config


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """Deep-merge override into base (mutates base in place)."""
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val

"""Portable infrastructure contracts."""

from omr_grader.infrastructure.atomic_io import atomic_write_bytes, atomic_write_json
from omr_grader.infrastructure.capabilities import (
    CapabilityToken,
    RootCapability,
    bootstrap_managed_paths,
    probe_root_capability,
)
from omr_grader.infrastructure.config_store import (
    AppConfig,
    default_config,
    load_config,
    save_config,
    validate_config,
)
from omr_grader.infrastructure.paths import (
    ManagedPaths,
    resolve_portable_root,
    validate_component,
    validate_profile_filename,
)

__all__ = [
    "AppConfig",
    "CapabilityToken",
    "ManagedPaths",
    "RootCapability",
    "atomic_write_bytes",
    "atomic_write_json",
    "bootstrap_managed_paths",
    "default_config",
    "load_config",
    "probe_root_capability",
    "resolve_portable_root",
    "save_config",
    "validate_component",
    "validate_config",
    "validate_profile_filename",
]

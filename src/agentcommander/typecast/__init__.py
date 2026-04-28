"""TypeCast catalog integration.

Per user requirement: every startup, pull the latest models-catalog.json
from github.com/SixOfFive/TypeCast. On fetch failure, fall back to the
local cache; if no cache, fall back to a bundled snapshot; if neither,
return an empty catalog.

  - catalog.py: refresh_catalog / get_catalog (network + cache + bundled)
  - vram.py:    detect_vram (nvidia-smi → wmic → Apple Silicon estimate)
  - autoconfig.py: pick_default_model + per-role overrides
"""

from agentcommander.typecast.autoconfig import (
    AutoconfigSuggestion,
    ModelCandidate,
    build_candidates,
    fits_available_vram,
    pick_default_model,
    pick_per_role,
    suggest_config,
)
from agentcommander.typecast.catalog import (
    CATALOG_URL,
    CatalogLoadResult,
    get_catalog,
    refresh_catalog,
)
from agentcommander.typecast.vram import VramInfo, detect_vram

__all__ = [
    "AutoconfigSuggestion",
    "CATALOG_URL",
    "CatalogLoadResult",
    "ModelCandidate",
    "VramInfo",
    "build_candidates",
    "detect_vram",
    "fits_available_vram",
    "get_catalog",
    "pick_default_model",
    "pick_per_role",
    "refresh_catalog",
    "suggest_config",
]

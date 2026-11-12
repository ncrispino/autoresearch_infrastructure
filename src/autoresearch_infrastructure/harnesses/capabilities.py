"""Adapter declarations for existing native tools, scopes and support status."""

from .adapters import get_adapter


def capabilities(harness, settings):
    """Record grants separately from task-tool vocabulary; never grant general Bash."""
    result = [{"capability": "task_tools", "tools": settings.get("tools", {}).get("allow", []),
               "scope": "Registered MCP task tools, mediated by program pre/post hooks."}]
    if settings.get("skills"):
        result.append({"capability": "skill_files", "tools": get_adapter(harness).capability_specs["skill_files"]["tools"],
                       "scope": "Read selected skill packages only; no scripts or arbitrary shell.",
                       "packages": settings["skills"]})
    return result

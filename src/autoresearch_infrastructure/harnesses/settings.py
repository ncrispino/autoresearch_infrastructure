"""One validated kwargs surface and a researcher-readable option catalog."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from autoresearch_infrastructure.persistence import write_json
from autoresearch_infrastructure.source_tree import snapshot

from .adapters import get_adapter

# These descriptions are the canonical shared text used in both emitted guides.
SHARED = {
    "system": {"type": "string or null", "default": None, "description": (
        "Program-owned session instructions for the solver's workflow and constraints. "
        "These instructions are delivered through the harness instruction layer, "
        "never appended to the user task. For example, describe how to inspect inputs "
        "and verify the requested output."),
        "example": "Inspect the inputs, perform the requested work, and verify the output."},
    "system_mode": {"type": "extend | replace", "default": "extend", "description": (
        "Project-defined instruction composition. Extend preserves the declared editable baseline; "
        "replace substitutes that baseline. This enables a different policy structure rather than "
        "accumulating warnings. Vendor-internal instructions may remain immutable. Each adapter maps "
        "the operation explicitly; unsupported replacement never falls back to a user message."),
        "example": "replace"},
    "pre_tool": {"type": "callback reference or null", "default": None, "description": (
        "A program callback before registered MCP task tools and permitted native skill reads. Return Allow or Deny with a reason "
        "to enforce action authorization rather than rely on prompt guidance. The runtime rejects "
        "malformed decisions and prevents pending effects. ToolCall.origin distinguishes task "
        "from native calls. Adapter-owned permissions still restrict native calls to scoped skill reads."), "example": "before_tool"},
    "post_tool": {"type": "callback reference or null", "default": None, "description": (
        "A callback receiving the matching task or native tool result before delivery. It can retain provenance "
        "or transform the visible observation; raw evidence remains immutable. This helps isolate "
        "untrusted text before subsequent reasoning, but cannot undo effects or change grading. "
        "Native transformation limits are declared by the selected adapter. Unsupported changes "
        "block delivery and fail the trial; a missing native completion also fails the trial."),
        "example": "after_tool"},
    "skills": {"type": "list of package-relative directories", "default": [], "description": (
        "Versioned skill directories delivered through the selected harness's discovery mechanism. "
        "They package procedures that can be loaded when useful rather than expanding every prompt. "
        "Discovery does not ensure use. The adapter enables its existing native read tools only "
        "for the selected skill roots and references, as described by the adapter capability guide. Scripts and arbitrary shell remain denied. "
        "Only snapshotted safe paths are accepted. Each directory requires SKILL.md beginning "
        "with YAML frontmatter containing name and description, for example "
        "---\\nname: verify-output\\ndescription: Verify the requested output.\\n---\\n. "
        "A plain Markdown heading without that metadata is invalid."), "example": ["skills/verify-output"]},
    "tools": {"type": "object with allow:list[string]", "default": None, "description": (
        "A subset of registered task tools available to a session. Omission uses the main trial's "
        "permitted set; helpers default to none. An empty allowlist grants no task tools; selected "
        "skills may separately grant scoped native reads. "
        "Adapters deny ungranted native effects and the MCP server filters and enforces the exact "
        "allowlist, so this cannot grant permissions beyond the frozen trial."), "example": {"allow": []}},
    "subagents": {"type": "object with enabled:bool,max_depth:int", "default": {"enabled": False,
        "max_depth": 0}, "description": (
        "Enables bounded runtime-orchestrated helper sessions with the same frozen harness and model. "
        "Helpers receive explicit context and no task tools. This is a Python runtime nesting "
        "policy, not a model-facing tool. Only one root session may be active; total active "
        "sessions are bounded by one plus the frozen maximum depth, with excess calls rejected "
        "rather than queued. The adapter capability catalog lists native delegation tools and "
        "support status separately; no custom launch tool is created."),
        "example": {"enabled": True, "max_depth": 1}},
    "state_scope": {"type": "session | trial", "default": "session", "description": (
        "Selects the runtime-managed program-state namespace. Session state prevents accidental helper "
        "sharing; trial state supports an intentional common authorization ledger. It does not share "
        "conversations. All state resets between independent conditions and repeats; in-memory state "
        "is not automatically crash-resumable."), "example": "trial"},
    "timeout_seconds": {"type": "positive finite number", "default": None, "description": (
        "Session deadline within remaining frozen trial time, including child work. A short guard "
        "deadline contains per-action latency. Expiry cancels owned work and retains partial evidence; "
        "it never creates capacity or makes an unfinished answer completed. Evidence collection and "
        "container removal use a separate bounded shutdown grace, so wall time can exceed this deadline."),
        "example": 30},
    "hook_timeout_seconds": {"type": "positive finite number", "default": 30, "description": (
        "Deadline for one hook, including helpers it awaits. This prevents policy review from hanging "
        "before every action. A pre-tool timeout prevents the pending effect and is recorded as a "
        "program runtime failure, not a successful policy denial."), "example": 20},
}

def _option_specs(adapter):
    if set(SHARED) & set(adapter.setting_specs):
        raise ValueError("Adapter settings cannot redefine shared settings")
    return {**SHARED, **adapter.setting_specs}


def _safe_relative(value: str) -> bool:
    p = Path(value)
    return bool(value and p.parts and not p.is_absolute() and ".." not in p.parts and
                p.as_posix() == value and "\\" not in value)


def validate_kwargs(harness: str, kwargs: dict[str, Any], package_root: Path | None = None) -> dict:
    """Validate without executing candidate code or accepting protected overrides."""
    adapter = get_adapter(harness)
    if not isinstance(kwargs, dict):
        raise ValueError("Harness kwargs must be an object")
    unknown = set(kwargs) - set(_option_specs(adapter))
    if unknown:
        raise ValueError(f"Unsupported harness kwargs: {sorted(unknown)}")
    values = dict(kwargs)
    if "system" in values and values["system"] is not None:
        if not isinstance(values["system"], str) or len(values["system"].encode()) > 100_000:
            raise ValueError("system must be bounded text or null")
    if values.get("system_mode", "extend") not in ("extend", "replace"):
        raise ValueError("system_mode must be extend or replace")
    if "system_mode" in values and values.get("system") is None:
        raise ValueError("system_mode requires system")
    for name in ("pre_tool", "post_tool"):
        value = values.get(name)
        if value is not None and not callable(value) and not (
            isinstance(value, str) and 0 < len(value) <= 200
        ):
            raise ValueError(f"{name} must be a callback reference")
    skills = values.get("skills", [])
    if (not isinstance(skills, list) or len(skills) > 32 or
        not all(isinstance(x, str) for x in skills) or len(set(skills)) != len(skills)):
        raise ValueError("skills must be a unique bounded path list")
    for skill in skills:
        if not isinstance(skill, str) or not _safe_relative(skill):
            raise ValueError("skills require package-relative paths")
        if package_root is not None:
            path = package_root / skill
            if not path.is_dir() or path.is_symlink() or package_root.resolve() not in path.resolve().parents:
                raise ValueError("skill directory is missing or unsafe")
            source = snapshot(path)
            metadata = source.get("SKILL.md", "")
            if (not metadata.startswith("---\n") or metadata.count("---") < 2
                or not re.search(r"^name:\s*[A-Za-z0-9][A-Za-z0-9_-]*\s*$", metadata.split("---", 2)[1], re.M)
                or not re.search(r"^description:\s*\S.+$", metadata.split("---", 2)[1], re.M)):
                raise ValueError("Each skill needs SKILL.md with name and description metadata")
    if "tools" in values:
        tools = values["tools"]
        if (not isinstance(tools, dict) or set(tools) != {"allow"} or
            not isinstance(tools["allow"], list) or len(tools["allow"]) > 200 or
            not all(isinstance(x, str) and x for x in tools["allow"]) or
            len(set(tools["allow"])) != len(tools["allow"])):
            raise ValueError("tools requires a unique allow list")
    if values.get("state_scope", "session") not in ("session", "trial"):
        raise ValueError("state_scope must be session or trial")
    if "subagents" in values:
        sub = values["subagents"]
        if (not isinstance(sub, dict) or set(sub) - {"enabled", "max_depth"} or
            type(sub.get("enabled", False)) is not bool or
            type(sub.get("max_depth", 0)) is not int or not 0 <= sub.get("max_depth", 0) <= 4 or
            (not sub.get("enabled", False) and sub.get("max_depth", 0) != 0)):
            raise ValueError("subagents requires enabled and bounded max_depth")
    for name in ("timeout_seconds", "hook_timeout_seconds"):
        if name in values and (type(values[name]) not in (int, float) or
            not math.isfinite(values[name]) or not 0 < values[name] <= 3600):
            raise ValueError(f"{name} must be positive finite seconds <= 3600")
    for name, record in adapter.setting_specs.items():
        if name not in values:
            continue
        if "values" in record and values[name] not in record["values"]:
            raise ValueError(f"Unsupported {name} for {harness}")
        if "minimum" in record and (type(values[name]) is not int
                or not record["minimum"] <= values[name] <= record["maximum"]):
            raise ValueError(f"{name} must be an integer between {record['minimum']} and {record['maximum']}")
    return values


def generate_guide(harness: str) -> dict:
    adapter = get_adapter(harness)
    return {"harness": harness, "kwargs": _option_specs(adapter),
            "adapter_capabilities": adapter.capability_specs,
            "coverage": "Pre/post callbacks cover MCP task tools and permitted native skill reads. Native result transformation is adapter-limited; other native effects remain denied.",
            "system": "extend/replace target the adapter-owned baseline; vendor instructions remain."}


def write_guide(harness: str, destination: Path) -> None:
    guide = generate_guide(harness)
    destination.mkdir(parents=True, exist_ok=True)
    write_json(destination / "harness-kwargs.json", guide)
    rows = [f"# Available kwargs: {harness}", "", guide["coverage"], "",
            "The harness and model are fixed; helpers use that same pair. Unknown kwargs fail.", ""]
    for name, spec in guide["kwargs"].items():
        rows.extend([f"## `{name}`", "", f"Type: {spec['type']}. Default: `{spec['default']}`.",
                     "", spec["description"], "", f"Example: `{spec['example']}`.", ""])
    rows.extend(["## Adapter capability tools", "",
                 "These are the selected harness's existing native tools, not new MCP tools.", ""])
    for name, record in guide["adapter_capabilities"].items():
        rows.extend([f"- {name}: tools {record['tools']}. "
                     + record.get("scope", record.get("reason", "")), ""])
    (destination / "harness-kwargs.md").write_text("\n".join(rows))

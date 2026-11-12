"""Native permission gate and adapter transport for editable program callbacks.

Both pinned CLIs accept this PreToolUse response shape. Codex maps exec_command
to Bash in hook events. MCP policy callbacks remain in their existing task pipeline.
"""

import json
import shlex
import sys
import urllib.request
from pathlib import Path

if __package__:
    from ..programs.parsing import _parse_json
else:
    from program_parsing import _parse_json


def _readable(path, roots, *, directory=False):
    if not isinstance(path, str) or not Path(path).is_absolute():
        return False
    candidate = Path(path)
    if any(p.is_symlink() for p in (candidate, *candidate.parents)):
        return False
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, ValueError):
        return False
    return (any(resolved == Path(root) or Path(root) in resolved.parents for root in roots)
            and (resolved.is_dir() if directory else resolved.is_file()))


def _skill_read(name, inputs, roots, native_tools):
    if not roots or not isinstance(inputs, dict):
        return False
    spec = native_tools.get(name)
    if not isinstance(spec, dict):
        return False
    argument = inputs.get(spec["argument"])
    if spec["kind"] == "file":
        return _readable(argument, roots)
    command = argument
    if spec["kind"] != "command" or not isinstance(command, str):
        return False
    # Only literal cat/ls on selected skill paths; never general shell syntax,
    # scripts, expansion, redirection, background jobs or command substitution.
    if any(x in command for x in ("$", "`", "\n", "\r", "\x00", "*", "?", "[", "]", "{", "}", "~")):
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        args = list(lexer)
    except ValueError:
        return False
    if len(args) == 3 and args[1] == "--":
        args.pop(1)
    if len(args) != 2 or args[0] not in ("cat", "ls"):
        return False
    return _readable(args[1], roots, directory=args[0] == "ls")


def decide(event, allowed, read_roots=(), native_tools=None):
    name = event.get("tool_name") if isinstance(event, dict) else None
    inputs = event.get("tool_input", {}) if isinstance(event, dict) else {}
    if isinstance(name, str) and (name in allowed or _skill_read(name, inputs, read_roots, native_tools or {})):
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "Only explicitly granted session tools and skill reads may execute.",
    }}


def _policy(event, config):
    stage = {"PreToolUse": "pre", "PostToolUse": "post", "PostToolUseFailure": "failure"}[
        event["hook_event_name"]
    ]
    params = {"stage": stage, "id": event["tool_use_id"],
              "name": event["tool_name"], "args": event["tool_input"]}
    if stage == "post":
        params["result"] = event["tool_response"]
    elif stage == "failure":
        params["error"] = event["error"]
    request = urllib.request.Request(config["url"], data=json.dumps({
        "jsonrpc": "2.0", "id": "native-hook", "method": "ari/native_hook", "params": params,
    }, allow_nan=False).encode(), headers={"Authorization": "Bearer " + config["token"],
                                           "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=config["timeout"]) as response:
        value = _parse_json(response.read(1_000_001).decode(), max_bytes=1_000_000)
    if not isinstance(value, dict) or "error" in value or not isinstance(value.get("result"), dict):
        raise ValueError("Native policy service failed")
    result = value["result"]
    if stage == "pre":
        if set(result) != {"allowed", "reason"} or type(result["allowed"]) is not bool or not isinstance(result["reason"], str):
            raise ValueError("Invalid native policy decision")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                "permissionDecision": "allow" if result["allowed"] else "deny",
                "permissionDecisionReason": result["reason"]}}
    if set(result) != {"result"}:
        raise ValueError("Invalid native policy observation")
    if stage == "post" and result["result"] != event["tool_response"]:
        if not config["replacement_field"]:
            raise ValueError("Native result transformation unsupported")
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", config["replacement_field"]: result["result"]}}
    return {}


def main():
    config = None
    event = None
    failure_mode = sys.argv[4] if len(sys.argv) > 4 else None
    try:
        allowed = json.loads(sys.argv[1])
        roots = json.loads(sys.argv[2]) if len(sys.argv) > 2 else []
        config = json.loads(Path(sys.argv[3]).read_text())
        raw = sys.stdin.buffer.read(1_000_001)
        if (len(raw) > 1_000_000 or not isinstance(allowed, list)
                or not all(isinstance(x, str) for x in allowed)
                or not isinstance(roots, list) or not all(isinstance(x, str) for x in roots)):
            raise ValueError("Invalid bounded native gate request")
        event = _parse_json(raw.decode(), max_bytes=1_000_000)
        if event.get("tool_name") in allowed:
            # Never double-run policy for an MCP call already mediated by the host.
            result = decide(event, allowed, roots, config["native_tools"]) if event["hook_event_name"] == "PreToolUse" else {}
        elif not _skill_read(event.get("tool_name"), event.get("tool_input", {}), roots, config["native_tools"]):
            if event.get("hook_event_name") != "PreToolUse":
                raise ValueError("Unexpected native completion outside granted tools")
            result = decide(event, allowed, roots, config["native_tools"])
        else:
            result = _policy(event, config)
        print(json.dumps(result, allow_nan=False))
    except Exception:
        try:
            (Path(sys.argv[3]).parent / "logs/native-hook-failure.json").write_text('{"reason":"native_hook_failed"}')
        except (OSError, IndexError):
            pass
        # Codex must block, including Code Mode. Claude must stop before another
        # model request; its PostToolUse exit-2 alone does not replace raw output.
        if failure_mode == "stop":
            print(json.dumps({"continue": False, "stopReason": "Native program hook failed"}))
        elif event and event.get("hook_event_name") == "PostToolUse":
            print(json.dumps({"decision": "block", "reason": "Native program hook failed"}))
        else:
            print("Native tool admission failed", file=sys.stderr)
            raise SystemExit(2) from None


if __name__ == "__main__":
    main()

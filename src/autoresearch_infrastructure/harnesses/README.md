# Harness sessions

This package starts one coding-agent CLI session for a solver program and returns
its result. It supports two harnesses: `codex` (Codex CLI 0.159.2) and
`claude-code` (Claude Code 2.1.309). Both versions are pinned in the
[solver image](../../../docker/solver/README.md). The package reuses the Codex and
Claude Code agents installed with Harbor 0.24.0.

| File | Role |
| --- | --- |
| [settings.py](settings.py) | The settings a program may pass to `runtime.run_harness`, and the function that checks them |
| [sessions.py](sessions.py) | `HarnessSessionRunner`, which starts sessions and validates their evidence |
| [adapters/](adapters/__init__.py) | One class per harness |
| [native_gate.py](native_gate.py) | The hook that the CLI calls before and after each of its own tool calls |
| [native_hooks.py](native_hooks.py) | Validation of the events that hook sends |
| [capabilities.py](capabilities.py) | The record of which tools and skills a session was granted |
| [container.py](container.py), [session_process.py](session_process.py) | The session container and its process control |

## Settings

`validate_kwargs(harness, kwargs)` is the only function that checks settings. Settings shared by both
harnesses are defined in `settings.py`. Settings specific to one harness are
defined on its adapter class. An unknown setting raises an error. No setting
changes the model or the provider.

`write_guide(harness, directory)` writes `harness-kwargs.md` and
`harness-kwargs.json`, which describe every setting. `write_researcher_context`
puts them in the researcher's read-only context.

## One session

Each `HarnessSessionRunner.run_session` call gets a new home directory,
configuration, working directory and log tree. All sessions in a trial use the
same harness and model.

`system` adds to or replaces the session's base instruction, depending on
`system_mode`. The base instruction is `instruction_baseline`, which a caller can
set and which defaults to a neutral sentence. Codex receives the text as a model
instructions file. Claude Code receives it as its system prompt argument.
Instructions built into the vendor CLI may still apply.

Skills are copied byte for byte and found by the CLI's own skill discovery.
Selecting a skill grants read access to its files and nothing else.

The provider is passed to the session runner by its caller. The adapters read no
project environment variables to choose one.

## Tools

Task tools reach the CLI through an MCP server that is started for the session
and requires the session's token. The CLI's own tools are restricted:

- Codex: a `PreToolUse` hook allows the session's task MCP tools and reads of
  selected skill files. It denies every other call. The shell, multi-agent and
  browser features are disabled.
- Claude Code: the CLI starts with only its `Read` tool, used for selected skill
  files, and a strict MCP configuration. Its `Agent` tool is unavailable.

A helper session has no task tools. It can read skill files only when the program
selects skills for it.

The program's `pre_tool` and `post_tool` hooks run in the program worker. They see
task tool calls and skill file reads. A `post_tool` hook on a skill file read can
only observe the result under Codex. Under Claude Code it may replace
`file.content` of a text `Read`. Any other change to a CLI tool result raises an
error.

`native_gate.py` is fixed launch code. A candidate cannot edit it, and no setting
can re-enable the CLI's own execution tools or widen the tool list.

## Evidence

The original ATIF trajectory of every session, helper sessions included, is kept
byte for byte. A turn that failed or did not finish is reported as incomplete. A
timeout or cancellation keeps the partial logs.

The program bridge owns the limits for the whole trial and runs task tool calls.
This package owns session launch, completion parsing and evidence validation.

## Adding a harness

[adapters/base.py](adapters/base.py) defines `HarnessAdapter`. An adapter class
declares:

- `setting_specs` and `capability_specs`, with descriptions and bounds
- `native_tools`, the CLI tools the gate may see, with their argument names
- `cli_version`
- how to deliver the system instruction and skills, launch the session through
  Harbor, read the output and validate a transformed CLI tool result

`adapters.ADAPTERS` is the only registry. It is built from an explicit list of
classes in [adapters/\_\_init\_\_.py](adapters/__init__.py). There is no subclass
discovery and no import chosen by a candidate. To add a harness, implement the
class and add it to that list. `settings.py` and the session runner need no
change. An adapter's settings cannot redefine a shared setting.

Reading an adapter's settings does not import Harbor. Harbor is imported only when
a session starts.

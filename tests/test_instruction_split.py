"""Splitting an imported instruction into objective, rules and environment, and naming them. No Docker."""

import json
from pathlib import Path

import pytest

import autoresearch_infrastructure as ari
from autoresearch_infrastructure import instruction_split, transforms
from autoresearch_infrastructure.inputs import ResearcherInputs

SOURCE = """# Pick points

Return the `k` points that fill the cube most evenly.

## Submission format

- `seed`: int. Use it for any randomness.
- `budget`: float. `select` must return within it.

## Environment

- Example clouds live in `/app/data/`.
- You have 24 hours and may submit as often as you like.
"""


def write(tmp_path, parts, source=SOURCE):
    (tmp_path / "instruction.md").write_text(source)
    lines = []
    for part in parts:
        lines.append("[[part]]")
        lines.extend(f"{key} = {json.dumps(value)}" for key, value in part.items())
        lines.append("")
    (tmp_path / "split.toml").write_text("\n".join(lines))
    return tmp_path / "split.toml", tmp_path / "instruction.md"


FULL = [
    {"to": "dropped", "source": "# Pick points", "reason": "The title."},
    {"to": "objective", "tag": "verbatim", "source": "Return the `k` points that fill the cube most evenly."},
    {"to": "objective", "tag": "adapted", "source": "## Submission format", "text": "### Submission format",
     "reason": "Heading demoted."},
    {"to": "objective", "tag": "verbatim", "source": "`seed`: int."},
    {"to": "rule", "tag": "adapted", "name": "seeded", "source": "Use it for any randomness.",
     "text": "Do not use randomness that `seed` does not determine.", "reason": "Made a prohibition."},
    {"to": "objective", "tag": "verbatim", "source": "`budget`: float."},
    {"to": "rule", "tag": "adapted", "name": "time_budget", "source": "`select` must return within it.",
     "text": "Do not let `select` run past `budget`.", "reason": "Made a prohibition.",
     "enforced": "The grader kills a slow call."},
    {"to": "dropped", "source": "## Environment", "reason": "Section heading."},
    {"to": "environment", "tag": "verbatim", "name": "examples", "source": "Example clouds live in `/app/data/`."},
    {"to": "dropped", "source": "You have 24 hours and may submit as often as you like.",
     "reason": "The run's settings set the time and the number of proposals."},
    {"to": "rule", "tag": "added", "name": "no_lookup", "text": "Do not return answers stored for particular clouds.",
     "reason": "The importer's: the task states no such rule."},
]


def test_a_split_routes_every_span_once_and_keeps_its_wording(tmp_path):
    instructions, record = instruction_split.load(*write(tmp_path, FULL))
    assert instructions.objective == ("Return the `k` points that fill the cube most evenly.\n\n"
                                      "### Submission format\n\n- `seed`: int.\n- `budget`: float.")
    assert instructions.rule_names == ("seeded", "time_budget", "no_lookup")
    assert instructions.rules[1] == "Do not let `select` run past `budget`."
    assert instructions.environment == ("Example clouds live in `/app/data/`.",)
    assert instructions.environment_names == ("examples",)
    assert record["source_sha256"] and len(record["parts"]) == len(FULL)
    assert record["parts"][6]["enforced"] == "The grader kills a slow call."


@pytest.mark.parametrize("change, message", [
    (lambda parts: parts[:-2] + parts[-1:], r"1 span\(s\) of instruction.md are in no part.*24 hours"),
    (lambda parts: parts + [{"to": "objective", "tag": "verbatim", "source": "`seed`: int."}], "overlaps"),
    (lambda parts: parts + [{"to": "dropped", "source": "missing text", "reason": "x"}], "occurs 0 times"),
    (lambda parts: [dict(parts[0], to="elsewhere")] + parts[1:], "to is one of"),
    (lambda parts: parts[:4] + [dict(parts[4], tag="verbatim")] + parts[5:], "verbatim part has a source and no text"),
    (lambda parts: parts[:4] + [{k: v for k, v in parts[4].items() if k != "reason"}] + parts[5:],
     "adapted part has the source, the new text and the reason"),
    (lambda parts: parts[:4] + [{k: v for k, v in parts[4].items() if k != "name"}] + parts[5:], "needs a stable name"),
    (lambda parts: parts[:-1] + [dict(parts[-1], name="seeded")], "Two rule parts are named 'seeded'"),
    (lambda parts: parts[:2] + [dict(parts[2], text="## Submission format")] + parts[3:], "adapt it to a '###'"),
    (lambda parts: parts[:-1] + [dict(parts[-1], source="Return the")], "added part has new text"),
])
def test_a_split_that_misses_overlaps_or_breaks_its_form_is_refused(tmp_path, change, message):
    with pytest.raises(ValueError, match=message):
        instruction_split.load(*write(tmp_path, change(list(FULL))))


def inputs(**names):
    return ResearcherInputs(goal="G", measured=(), rules=("A", "B", "C"), environment_notes=("N1", "N2"), **names)


def test_rules_and_notes_are_found_by_name_or_number_and_keep_their_names():
    named = inputs(rule_names=("first", "second", "third"), environment_names=("net", "data"))
    omitted = transforms.omit_rule("second")(named)
    assert omitted.named_rules == {"first": "A", "third": "C"}
    assert transforms.reword_rule(2, "C2")(omitted).named_rules == {"first": "A", "third": "C2"}
    assert transforms.add_rule("D", name="fourth")(omitted).named_rules == {"first": "A", "third": "C", "fourth": "D"}
    # Unnamed rules keep the reviewer's old names, and omitting one no longer renumbers the rest.
    plain = transforms.omit_rule(1)(inputs())
    assert plain.named_rules == {"rule_2": "B", "rule_3": "C"}
    assert transforms.add_rule("D")(plain).named_rules == {"rule_2": "B", "rule_3": "C", "rule_4": "D"}
    with pytest.raises(ValueError, match="no rule 'missing'"):
        transforms.omit_rule("missing")(named)
    with pytest.raises(ValueError, match="is taken"):
        transforms.add_rule("D", name="first")(named)


def test_environment_notes_can_be_omitted_or_reworded():
    named = inputs(rule_names=("a", "b", "c"), environment_names=("net", "data"))
    assert transforms.omit_environment_note("net")(named).named_environment == {"data": "N2"}
    blurred = transforms.reword_environment_note("net", "The network may be restricted.")(named)
    assert blurred.named_environment == {"net": "The network may be restricted.", "data": "N2"}
    assert transforms.omit_environment_note(2)(inputs()).named_environment == {"note_1": "N1"}
    assert transforms.reword_environment_note("net", "x").identity == {
        "transform": "reword_environment_note", "note": "net", "text": "x"}


def test_names_must_be_complete_unique_and_plain():
    with pytest.raises(ValueError, match="every one of the 2 rules"):
        ari.Instructions("Goal", rules=("A", "B"), rule_names=("a",))
    with pytest.raises(ValueError, match="names repeat"):
        ari.Instructions("Goal", rules=("A", "B"), rule_names=("a", "a"))
    with pytest.raises(ValueError, match="not a usable name"):
        ari.Instructions("Goal", rules=("A",), rule_names=("Time Budget",))


def deliverable_task(tmp_path):
    from autoresearch_infrastructure.source_tree import materialize

    materialize(tmp_path / "task", {
        "harbor-task/task.toml": '[environment]\nworkdir = "/app"\n[agent]\ntimeout_sec = 7200.0\n',
        "harbor-task/instruction.md": SOURCE,
        "harbor-task/environment/Dockerfile": "FROM busybox\n",
        "harbor-task/tests/feedback.sh": "echo 1 > /logs/verifier/reward.txt\n",
        "seed/answer.txt": "x\n",
        "task.py": ("import autoresearch_infrastructure as ari\n\n\ndef task(run):\n"
                    "    return ari.bases.harbor_deliverable(run, harbor_task='harbor-task', deliverable='/app/out',"
                    " feedback='/tests/feedback.sh', sealed=None, check_seed=False, task_environment=False,"
                    " split='split.toml')\n"),
        "settings.toml": ('reviewer = false\n[limits]\ncandidates = 4\n[researcher]\nname = "terminus-2"\n'
                          'model_name = "openrouter/openai/gpt-6-luna"\n'),
    })
    write(tmp_path / "task", FULL)
    (tmp_path / "task/instruction.md").unlink()  # the split reads the Harbor task's own instruction


def test_a_deliverable_task_with_a_split_records_it_and_takes_the_tasks_time_limit(tmp_path):
    pytest.importorskip("harbor")
    from autoresearch_infrastructure import launch

    deliverable_task(tmp_path)
    run = launch.open_run(tmp_path / "task", tmp_path / "task/settings.toml", tmp_path / "run",
                          "sha256:" + "a" * 64)
    plan = json.loads((tmp_path / "run/plan.json").read_text())
    assert plan["inputs"]["rule_names"] == ["seeded", "time_budget", "no_lookup"]
    assert plan["inputs"]["instruction_split"]["parts"][1]["tag"] == "verbatim"
    assert plan["inputs"]["researcher_timeout"] == 7200.0
    assert run.inputs.named_environment["ari_budget"] == (
        "This session may run for up to 2 hours, and the run allows up to 3 proposals in all.")
    assert "examples" in run.inputs.named_environment and "ari_submission" in run.inputs.named_environment
    assert "24 hours" not in plan["instructions"]  # dropped, and the settings' budget stated instead
    assert Path(tmp_path / "task/split.toml").is_file()


def test_describe_shows_each_part_as_a_hunk_with_its_reason(tmp_path):
    _, record = instruction_split.load(*write(tmp_path, FULL))
    shown = instruction_split.describe(record)
    assert shown.startswith("split.toml: 11 parts: 4 verbatim, 3 adapted, 1 added, 3 dropped\n")
    assert ("@@ rule time_budget (adapted)\n- `select` must return within it.\n"
            "+ Do not let `select` run past `budget`.\n  # Made a prohibition.\n"
            "  # enforced: The grader kills a slow call.") in shown
    assert "@@ dropped\n- # Pick points\n  # The title." in shown
    assert "@@ rule no_lookup (added)\n+ Do not return answers" in shown
    assert "@@ objective (verbatim)\n  `seed`: int." in shown
    changes = instruction_split.describe(record, changes_only=True)
    assert "(verbatim)" not in changes and "@@ rule seeded (adapted)" in changes


def test_ari_instructions_reads_a_task_or_a_run(tmp_path, capsys):
    pytest.importorskip("harbor")
    from autoresearch_infrastructure import launch
    from autoresearch_infrastructure.cli import main

    deliverable_task(tmp_path)
    task, settings = tmp_path / "task", tmp_path / "task/settings.toml"
    assert main(["instructions", str(task), "--config", str(settings), "--image", "sha256:" + "a" * 64,
                 "--changes"]) == 0
    from_task = capsys.readouterr().out
    assert "@@ rule time_budget (adapted)" in from_task and "(verbatim)" not in from_task
    assert not list(tmp_path.glob("run*"))  # opening the task left nothing behind
    launch.open_run(task, settings, tmp_path / "run", "sha256:" + "a" * 64).research.close()
    assert main(["instructions", str(tmp_path / "run"), "--changes"]) == 0
    assert capsys.readouterr().out == from_task
    assert main(["instructions", str(tmp_path / "run"), "--rendered"]) == 0
    assert "Do not let `select` run past `budget`." in capsys.readouterr().out

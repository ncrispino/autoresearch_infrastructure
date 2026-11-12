"""Only completed, bound evaluator artifacts may become isolated viewer images."""

import base64
import json

import pytest

import autoresearch_infrastructure as ari
from autoresearch_infrastructure.persistence import write_json
from autoresearch_infrastructure.source_tree import evidence_digest
from autoresearch_infrastructure.viewer import _artifact_images, build_index, write_html_preview

ERROR = ari.Metric("error", direction="min")
SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="60"><path d="M0 0L100 60"/></svg>'


def valid(candidate, context):
    return ari.Validity()


def evaluate(candidate, context):
    (context.artifacts / "fit.svg").write_text(SVG)
    (context.artifacts / "residuals.json").write_text('[1, 2]')
    return ari.Evaluation({ERROR: 1.5})


def fixture(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "coefficients.json").write_text('[0, 1]')
    output = tmp_path / "run"
    with ari.Research(output, seed=seed, objective=ERROR, evaluator=evaluate, validity=valid,
                      evaluation_config={"data": "fixture"}) as research:
        result = research.record_candidate()
    attempt = next(iter(result["evidence"]["cells"].values()))["trial_starts"][0]
    return output, attempt


def image_attempt(index):
    return index["sessions"][0]["candidates"][0]["evidence"][0]["cells"][0]["trial_starts"][0]


def test_hash_bound_images_are_embedded_as_data_not_document_markup(tmp_path):
    output, attempt = fixture(tmp_path)
    index = build_index(output)
    assert not index["warnings"]
    images = image_attempt(index)["artifact_images"]
    assert len(images) == 1 and images[0]["path"] == "fit.svg"
    assert base64.b64decode(images[0]["data_url"].split(",")[1]) == SVG.encode()
    destination = tmp_path / "viewer.html"
    write_html_preview(output, destination)
    html = destination.read_text()
    assert "data:image/svg+xml;base64," in html
    assert SVG not in html
    assert "image.src = artifact.data_url" in html
    assert "innerHTML" not in html
    assert (output / attempt["result_path"]).is_file()


@pytest.mark.parametrize("mutation", ["image", "other_artifact", "symlink", "wrapper", "escape"])
def test_tampered_or_unbound_artifacts_are_not_previewed(tmp_path, mutation):
    output, attempt = fixture(tmp_path)
    result_path = output / attempt["result_path"]
    image = result_path.parent / "artifacts/fit.svg"
    if mutation == "image":
        image.write_text(SVG + "<!-- changed -->")
    elif mutation == "other_artifact":
        image.with_name("residuals.json").write_text("[]")
    elif mutation == "symlink":
        replacement = tmp_path / "elsewhere.svg"
        replacement.write_text(SVG)
        image.unlink()
        image.symlink_to(replacement)
    elif mutation == "wrapper":
        wrapper = json.loads(result_path.read_text())
        wrapper["condition"] = "different"
        write_json(result_path, wrapper)
    elif mutation == "escape":
        state = json.loads((output / "state.json").read_text())
        candidate = next(iter(state["sessions"]["research"]["candidates"].values()))
        record = next(iter(candidate["evidence"].values()))
        next(iter(record["cells"].values()))["trial_starts"][0]["result_path"] = "../outside.json"
        write_json(output / "state.json", state)
    index = build_index(output)
    assert not image_attempt(index).get("artifact_images")
    assert any("Artifact previews unavailable" in warning for warning in index["warnings"])


def test_image_size_and_aggregate_caps_do_not_change_saved_artifacts(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "first.svg").write_text(SVG)
    (root / "second.svg").write_text(SVG)
    (root / "huge.svg").write_bytes(b" " * 1_000_001)
    expected = evidence_digest(root)
    remaining = [len(SVG.encode())]
    warnings = []
    images = _artifact_images(root, expected, remaining, warnings)
    assert [image["path"] for image in images] == ["first.svg"]
    assert remaining == [0]
    assert warnings == ["Image previews omitted above size limits: huge.svg, second.svg"]
    assert evidence_digest(root) == expected


def test_incomplete_results_are_not_image_evidence(tmp_path):
    output, attempt = fixture(tmp_path)
    state = json.loads((output / "state.json").read_text())
    candidate = next(iter(state["sessions"]["research"]["candidates"].values()))
    record = next(iter(candidate["evidence"].values()))
    next(iter(record["cells"].values()))["trial_starts"][0]["status"] = "infrastructure_error"
    write_json(output / "state.json", state)
    assert not image_attempt(build_index(output)).get("artifact_images")

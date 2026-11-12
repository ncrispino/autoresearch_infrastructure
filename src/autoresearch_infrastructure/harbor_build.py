"""Build an unmodified upstream Harbor task behind a TLS-inspecting proxy, and record how."""

import hashlib
import json
import re
from pathlib import Path

from .persistence import write_json


def ca_build_overlay(
    task: Path, destination: Path, ca_bundle: Path, *, uv_version: str | None = None
) -> Path:
    """Write a Compose overlay that lets a Harbor task build and run with a custom CA bundle.

    The task's own files are not changed. A derived Dockerfile mounts the bundle
    as a build secret in every ``RUN`` step, and the container mounts it
    read-only at run time. TLS verification stays on and the bundle is never
    stored in an image layer.

    ``uv_version`` also replaces the one known ``curl ... | sh`` uv installer
    line with that exact release from PyPI. ``receipt.json`` records both
    changes with hashes. Include the overlay's hash in the experiment's
    ``evaluation_config`` so a different overlay needs a fresh output.

    Only Harbor tasks with a single plain Dockerfile are supported.
    """
    source = task / "environment/Dockerfile"
    text = source.read_text()
    if "--mount=" in text or "# syntax=" in text:
        raise ValueError("Task Dockerfile needs an explicit build-overlay review")
    installer = None
    if uv_version is not None:
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", uv_version):
            raise ValueError("Pin an exact uv release")
        original = "RUN curl -LsSf https://astral.sh/uv/install.sh | sh"
        if original not in text:
            raise ValueError("Task does not use the reviewed uv installer pattern")
        replacement = "RUN python -m pip install uv==" + uv_version
        text = text.replace(original, replacement)
        installer = {
            "original": original,
            "replacement": replacement,
            "reason": "Use the official pinned PyPI distribution through allowed package egress",
        }
    destination.mkdir(parents=True, exist_ok=False)
    variables = " ".join(
        f"{key}=/run/secrets/session_ca"
        for key in (
            "SSL_CERT_FILE",
            "PIP_CERT",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS",
        )
    )
    derived = text.replace(
        "RUN ", "RUN --mount=type=secret,id=session_ca,required=true export " + variables + "; "
    )
    dockerfile = destination / "Dockerfile"
    dockerfile.write_text(derived)
    overlay = destination / "compose.json"
    write_json(
        overlay,
        {
            "services": {
                "main": {
                    "build": {
                        "context": str((task / "environment").resolve()),
                        "dockerfile": str(dockerfile.resolve()),
                        "secrets": ["session_ca"],
                    }
                }
            },
            "secrets": {"session_ca": {"file": str(ca_bundle.resolve())}},
        },
    )
    config = json.loads(overlay.read_text())
    service = config["services"]["main"]
    service["volumes"] = [str(ca_bundle.resolve()) + ":/run/ari-public-ca.pem:ro"]
    service["environment"] = {
        key: "/run/ari-public-ca.pem"
        for key in (
            "SSL_CERT_FILE",
            "PIP_CERT",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS",
        )
    }
    write_json(overlay, config)
    write_json(
        destination / "receipt.json",
        {
            "adaptation": "build CA secrets and read-only public runtime CA; upstream task files untouched",
            "installer_adaptation": installer,
            "source_dockerfile_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "derived_dockerfile_sha256": hashlib.sha256(derived.encode()).hexdigest(),
            "ca_bundle_sha256": hashlib.sha256(ca_bundle.read_bytes()).hexdigest(),
        },
    )
    return overlay

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ARG CODEX_VERSION=0.159.2
ARG CLAUDE_CODE_VERSION=2.1.309
ARG NODE_VERSION=22.23.4
ARG CODEX_CODE_MODE_HOST_SHA256=fb6b0c4a7b24ed0728d1208ebc0061383da60debc6d61cf63e02c48ac7f7630f

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl nodejs npm ripgrep tmux \
    && rm -rf /var/lib/apt/lists/*

# Scratch-environment proposals include executable conformance tests. They run only
# inside the no-network proposal sandbox, before any code can enter the trusted registry.
RUN --mount=type=secret,id=session_ca \
    if [ -f /run/secrets/session_ca ]; then \
        PIP_CERT=/run/secrets/session_ca pip install --no-cache-dir \
            "pydantic>=2.7,<3" "python-dotenv>=1,<2" "pytest>=9,<10"; \
    else \
        pip install --no-cache-dir \
            "pydantic>=2.7,<3" "python-dotenv>=1,<2" "pytest>=9,<10"; \
    fi

RUN --mount=type=secret,id=session_ca \
    NODE_EXTRA_CA_CERTS=/run/secrets/session_ca npm install --global "node@${NODE_VERSION}" \
    && NODE_EXTRA_CA_CERTS=/run/secrets/session_ca \
    npm install --global "@openai/codex@${CODEX_VERSION}" \
    && NODE_EXTRA_CA_CERTS=/run/secrets/session_ca npm install --global \
       "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && codex --version \
    && claude --version \
    && rm -rf /root/.npm

RUN --mount=type=secret,id=session_ca \
    if [ -s /run/secrets/session_ca ]; then export CURL_CA_BUNDLE=/run/secrets/session_ca; fi \
    && curl --fail --location \
      "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz" \
      --output /tmp/code-mode-host.tar.gz \
    && echo "${CODEX_CODE_MODE_HOST_SHA256}  /tmp/code-mode-host.tar.gz" | sha256sum --check \
    && tar -xzOf /tmp/code-mode-host.tar.gz codex-code-mode-host-x86_64-unknown-linux-musl \
       > /usr/local/bin/codex-code-mode-host \
    && chmod 0755 /usr/local/bin/codex-code-mode-host \
    && rm /tmp/code-mode-host.tar.gz

WORKDIR /workspace

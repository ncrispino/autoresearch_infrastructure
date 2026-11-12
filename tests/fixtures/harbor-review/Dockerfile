FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
RUN --mount=type=secret,id=session_ca \
    apt-get -o Acquire::https::CaInfo=/run/secrets/session_ca update \
    && apt-get -o Acquire::https::CaInfo=/run/secrets/session_ca install -y --no-install-recommends tmux \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace

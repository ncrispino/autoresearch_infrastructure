# Solver image

One image serves three roles: the solver's coding-agent CLI sessions, the
researcher and the reviewer. It contains Codex 0.159.2, Claude Code 2.1.309,
Node 22.23.4, Python 3.12 and tmux. tmux is there for Harbor's `terminus-2`
agent, which the shipped run settings use for the researcher and the reviewer.

Build it from the repository root. Docker is required.

```sh
docker build --secret id=session_ca,src=/etc/ssl/certs/ca-certificates.crt \
  -t ari-solver:local autoresearch_infrastructure/docker/solver
```

The `session_ca` secret is the public CA bundle of the managed environment. The
build uses it to reach npm and PyPI. It is not written into an image layer.

The image contains no credentials and no candidate files. The model, provider and
API key are supplied when a container starts.

Pass the image's immutable ID to a run, not the tag:

```sh
docker image inspect ari-solver:local --format '{{.Id}}'
```

`ari --image` requires that ID or a `repository@sha256:` digest. The `pilot` and `canary` modules accept the tag
and resolve it to the ID themselves. A study is bound to the ID it started with.
After changing a CLI version, rebuild, repeat the transport checks and start a new
run directory.

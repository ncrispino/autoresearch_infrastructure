"""Disposable filesystem/process boundary for one native solver session.

This implements the small environment interface used by Harbor's installed agents.
It never mounts the trial container, controller logs, candidate package or Docker socket.
"""
from __future__ import annotations

import asyncio
import io
import os
import shlex
import signal
import tarfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from uuid import uuid4

MAX_PROCESS_OUTPUT_BYTES = 70_000_000
CONTAINER_REMOVE_TIMEOUT = 30


async def _join_owned_task(task):
    """Join an owned task without forwarding repeated caller cancellation."""
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
        except BaseException:
            break
    return task.result(), interrupted


class SessionContainer:
    default_user = None

    def __init__(self, image: str):
        if not image or image.startswith("-"):
            raise ValueError("A frozen session image is required")
        self.image = image
        self.started = False
        self.name = "ari-session-" + uuid4().hex

    async def _process(self, args, *, data=None, timeout=120):
        process = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        output = [bytearray(), bytearray()]
        size = 0

        async def drain(stream, destination):
            nonlocal size
            while chunk := await stream.read(65536):
                size += len(chunk)
                if size > MAX_PROCESS_OUTPUT_BYTES:
                    raise RuntimeError("Session subprocess output limit exceeded")
                destination.extend(chunk)

        async def send_input():
            if process.stdin is None:
                return
            try:
                process.stdin.write(data)
                await process.stdin.drain()
                process.stdin.close()
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass  # Match communicate(): an early subprocess exit can close stdin.

        tasks = [asyncio.create_task(drain(process.stdout, output[0])),
                 asyncio.create_task(drain(process.stderr, output[1])),
                 asyncio.create_task(send_input()), asyncio.create_task(process.wait())]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
        except BaseException:
            # The local client and descendants must not keep pipe readers alive.
            # The surrounding context separately destroys the remote container.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await process.wait()
            raise
        return SimpleNamespace(return_code=process.returncode,
                               stdout=output[0].decode(errors="replace"),
                               stderr=output[1].decode(errors="replace"))

    async def __aenter__(self):
        args = ["docker", "run", "--detach", "--init", "--pull=never", "--name", self.name,
                "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--pids-limit=256", "--memory=1g", "--cpus=1",
                "--tmpfs=/tmp:rw,nosuid,size=1g", "--tmpfs=/workspace:rw,nosuid,size=128m",
                "--add-host=tool-host:host-gateway",
                "--env=NO_PROXY=tool-host,localhost,127.0.0.1",
                "--env=no_proxy=tool-host,localhost,127.0.0.1"]
        if os.environ.get("CODEX_PROXY_CERT"):
            args += ["--mount=type=bind,src=/etc/ssl/certs/ca-certificates.crt,dst=/run/ari-ca.pem,readonly"]
            for name in ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE"):
                args += ["--env=" + name + "=/run/ari-ca.pem"]
        try:
            result = await self._process([*args, self.image, "sleep", "infinity"])
            if result.return_code:
                raise RuntimeError("Could not start isolated harness session container")
            self.started = True
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        # Destroy the complete namespace, including daemonized descendants. This
        # also runs when startup, evidence collection or the native CLI fails.
        cleanup = asyncio.create_task(self._process(["docker", "rm", "--force", self.name], timeout=CONTAINER_REMOVE_TIMEOUT))
        result, interrupted = await _join_owned_task(cleanup)
        if result.return_code and self.started:
            raise RuntimeError("Could not destroy isolated session container")
        if interrupted:
            raise asyncio.CancelledError

    async def exec(self, command, env=None, cwd=None, user=None, timeout_sec=None, **_):
        args = ["docker", "exec"]
        for name, value in (env or {}).items():
            args += ["--env", name + "=" + value]
        if cwd:
            args += ["--workdir", str(cwd)]
        if user is not None:
            args += ["--user", str(user)]
        return await self._process([*args, self.name, "bash", "-o", "pipefail", "-c", command],
                                   timeout=timeout_sec or 120)

    async def _upload(self, source: Path, destination: str, directory: bool):
        target = PurePosixPath(destination)
        if not target.is_absolute() or ".." in target.parts:
            raise ValueError("Session uploads require an absolute destination")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            if directory:
                for path in sorted(source.rglob("*")):
                    if path.is_symlink() or not (path.is_dir() or path.is_file()):
                        raise ValueError("Unsafe session upload")
                    archive.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)
                parent = target
            else:
                if source.is_symlink() or not source.is_file():
                    raise ValueError("Unsafe session upload")
                archive.add(source, arcname=target.name)
                parent = target.parent
        result = await self.exec("mkdir -p " + shlex.quote(str(parent)))
        if result.return_code:
            raise RuntimeError("Could not prepare session upload")
        result = await self._process(
            ["docker", "exec", "--interactive", self.name, "tar", "--no-same-owner", "-xf", "-", "-C", str(parent)],
            data=buffer.getvalue(),
        )
        if result.return_code:
            raise RuntimeError("Could not upload isolated session files")

    async def upload_file(self, source, destination):
        await self._upload(Path(source), destination, False)

    async def upload_dir(self, source, destination):
        await self._upload(Path(source), destination, True)

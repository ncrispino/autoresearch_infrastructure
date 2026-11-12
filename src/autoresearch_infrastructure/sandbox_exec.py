"""Run one shell command with a time limit and an output limit, and print the result as JSON.

``harbor_agent`` sends this file's source into the Harbor task's container and
runs it there, so it imports only the standard library.
"""

import base64
import json
import os
import selectors
import signal
import subprocess
import sys
import time


def main():
    request = json.loads(base64.b64decode(sys.argv[1]))
    process = subprocess.Popen(
        ["bash", "-lc", request["command"]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    streams = selectors.DefaultSelector()
    streams.register(process.stdout, selectors.EVENT_READ, "stdout")
    streams.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + request["timeout_seconds"]
    failure = None
    try:
        while streams.get_map():
            if time.monotonic() >= deadline:
                failure = "command_timeout"
                break
            for key, _ in streams.select(min(0.1, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    streams.unregister(key.fileobj)
                    continue
                remaining = request["max_output_bytes"] - sum(len(x) for x in output.values())
                output[key.data].extend(chunk[: max(0, remaining)])
                if len(chunk) > remaining:
                    failure = "output_limit"
                    break
            if failure:
                break
    finally:
        if failure or process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        streams.close()
    print(
        json.dumps(
            {
                **{k: v.decode("utf-8", errors="replace") for k, v in output.items()},
                "return_code": process.returncode,
                "failure": failure,
            }
        )
    )


if __name__ == "__main__":
    main()

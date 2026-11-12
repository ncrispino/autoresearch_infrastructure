"""Run one generated harness command in its own cancellable process group."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def terminate(pid, sig):
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def main():
    if sys.argv[1] == "stop":
        path = Path(sys.argv[2])
        if path.is_file():
            value = path.read_text().strip()
            if value.isdecimal():
                terminate(int(value), signal.SIGTERM)
                time.sleep(0.2)
                terminate(int(value), signal.SIGKILL)
        return
    timeout, pid_path, command = float(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    process = subprocess.Popen(["bash", "-o", "pipefail", "-c", command], start_new_session=True)
    pid_path.write_text(str(process.pid))
    try:
        result = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            terminate(process.pid, signal.SIGKILL)
            process.wait()
        result = 124
    finally:
        pid_path.unlink(missing_ok=True)
    raise SystemExit(result)


if __name__ == "__main__":
    main()

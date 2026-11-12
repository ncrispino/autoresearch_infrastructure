"""Serve ``record_candidate`` to an agent in a container over a Unix socket."""

import json
import socket
import socketserver
import threading
from pathlib import Path


class _UnixServer(socketserver.ThreadingUnixStreamServer):
    # Request threads are not daemons: an evaluation finishes even if its client disconnects.
    daemon_threads = False


class CandidateToolServer:
    """Accepts one JSON request per connection and passes it to ``runner.dispatch_record_candidate``.

    Requests are handled one at a time. Leaving the context waits for a request
    that is still being evaluated.
    """

    def __init__(self, runner, session_id: str, socket_path: Path):
        self.runner = runner
        self.session_id = session_id
        self.socket_path = socket_path
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(30)
                try:
                    line = self.rfile.readline(8_000_001)
                    if not line or len(line) > 8_000_000:
                        raise ValueError("Invalid bounded tool request")
                    value = json.loads(line)
                    if value.get("name") != "record_candidate":
                        raise ValueError("Unknown tool")
                    with outer.dispatch_lock:
                        result = outer.runner.dispatch_record_candidate(
                            outer.session_id, value.get("arguments", {})
                        )
                except Exception as exc:
                    result = {"error": str(exc)}
                try:
                    self.wfile.write(json.dumps(result).encode() + b"\n")
                except (BrokenPipeError, ConnectionResetError, socket.timeout):
                    # The result is already saved by the runner; the client only missed the reply.
                    pass

        socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.dispatch_lock = threading.Lock()
        self.server = _UnixServer(str(socket_path), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.socket_path.unlink(missing_ok=True)

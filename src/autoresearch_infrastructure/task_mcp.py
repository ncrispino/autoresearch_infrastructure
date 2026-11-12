"""An MCP stdio server that forwards each request to the session's tool URL.

It is copied into each harness session container and uses the standard library only.
"""

import json
import os
import sys
import urllib.request


def main():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for line in sys.stdin:
        if len(line) > 1_000_000:
            raise ValueError("Oversized MCP request")
        value = json.loads(line)
        request = urllib.request.Request(
            os.environ["ARI_TOOL_URL"], data=line.encode(),
            headers={
                "Authorization": "Bearer " + os.environ["ARI_TOOL_TOKEN"],
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        with opener.open(request, timeout=3600) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("Oversized MCP response")
        if "id" in value:
            if not raw:
                raise ValueError("Missing MCP response")
            print(json.dumps(json.loads(raw)), flush=True)


if __name__ == "__main__":
    main()

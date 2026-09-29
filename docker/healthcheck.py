"""Container health probe.

Kept as a file rather than an inline `python -c` string in the Dockerfile
because the Dockerfile parser reads line by line: a quoted JSON string does not
continue across lines, so a multi-line probe fails the build with
`unknown instruction`. That was a real failure in the first CI run of the
Dockerfile, not a hypothetical.

Exit 0 = healthy, exit 1 = unhealthy. A refused connection is the expected
unhealthy case, so it is caught rather than left to print a traceback on every
failed probe. /health is a static route that answers without touching the
database, so an unhealthy verdict means the process is unwell, not that
PostgreSQL is briefly unreachable.
"""

import sys
import urllib.request

URL = "http://127.0.0.1:8000/health"

try:
    healthy = urllib.request.urlopen(URL, timeout=3).status == 200
except Exception:
    healthy = False

sys.exit(0 if healthy else 1)

"""Logging set-up, with a request id stamped on every line.

On a serverless host, concurrent invocations share one instance and their log
lines interleave in a single stream. Without a correlation id, "the shopkeeper
says saving failed" cannot be tied to any particular traceback. Every record
therefore carries an id taken from Vercel's own `x-vercel-id` header, which is
also what appears in the platform's request logs and in a response's headers —
so a user's report, the platform log and the application traceback can all be
lined up.
"""

import logging
import uuid
from contextvars import ContextVar

# "-" for anything logged outside a request, e.g. startup.
current_request_id: ContextVar[str] = ContextVar("current_request_id", default="-")


def new_request_id(vercel_id: str | None) -> str:
    """Prefer the platform's id so application and platform logs agree.

    x-vercel-id looks like `bom1::iad1::7bb9r-1791039269310-8831c5d0fdb6`; the
    last segment is the distinctive part and keeps log lines readable.
    """
    if vercel_id:
        return vercel_id.rsplit("-", 1)[-1][:16]
    return uuid.uuid4().hex[:12]


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id.get()
        return True


def configure(level: int = logging.INFO) -> None:
    """Install a single stderr handler for the whole process.

    Replaces any existing root handlers rather than adding to them: uvicorn
    installs its own, and logging twice makes a noisy stream look like
    duplicated work.
    """
    handler = logging.StreamHandler()
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(
        logging.Formatter("%(levelname)s %(name)s [%(request_id)s] %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # psycopg's pool re-logs the whole connection failure — every resolved
    # host and every IP, a dozen lines — on each reconnect attempt. With an
    # unreachable database that is hundreds of lines a minute, and it buries
    # the one line that says which request failed. The cause is not lost: the
    # pool-open failure in app/db.py logs it once, with a traceback.
    logging.getLogger("psycopg.pool").setLevel(logging.ERROR)

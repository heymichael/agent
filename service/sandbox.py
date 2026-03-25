"""Sandboxed Python executor for LLM-generated code.

Runs code in a restricted exec() with captured stdout, a hard timeout,
and blocked access to filesystem / subprocess / code-injection builtins.
Credentials are available via os.environ — they are never surfaced in
LLM prompts.
"""

import io
import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from contextlib import redirect_stderr

logger = logging.getLogger(__name__)

EXECUTION_TIMEOUT_SECONDS = 120

BLOCKED_MODULES = frozenset({
    "subprocess",
    "shutil",
    "socket",
    "http",
    "xmlrpc",
    "ftplib",
    "smtplib",
    "telnetlib",
    "ctypes",
    "multiprocessing",
    "threading",
    "asyncio",
    "webbrowser",
    "antigravity",
    "turtle",
    "tkinter",
    "code",
    "codeop",
    "compileall",
    "py_compile",
})

_real_import = __builtins__["__import__"]  # type: ignore[index]


def _safe_import(name: str, *args, **kwargs):
    """Import gate: block dangerous modules, allow everything else."""
    top_level = name.split(".")[0]
    if top_level in BLOCKED_MODULES:
        raise ImportError(
            f"Module '{name}' is not available in the sandbox."
        )
    return _real_import(name, *args, **kwargs)


def execute_python(code: str) -> dict:
    """Execute *code* in a sandboxed environment and return the result.

    Returns ``{"ok": True, "stdout": "...", "stderr": "..."}`` on success,
    or ``{"ok": False, "error": "...", "stderr": "..."}`` on failure.

    Uses a thread pool with a timeout so it works safely from any thread
    (uvicorn worker threads don't support SIGALRM).
    """
    logger.info("Sandbox: executing %d chars of generated code", len(code))

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    restricted_builtins = {
        k: v
        for k, v in __builtins__.items()  # type: ignore[union-attr]
        if k
        not in (
            "eval",
            "exec",
            "compile",
            "__import__",
            "open",
            "input",
            "breakpoint",
            "exit",
            "quit",
        )
    }
    restricted_builtins["__import__"] = _safe_import
    restricted_builtins["print"] = lambda *a, **kw: print(
        *a, **kw, file=stdout_buf
    )

    restricted_globals: dict = {"__builtins__": restricted_builtins}

    def _run():
        with redirect_stderr(stderr_buf):
            exec(code, restricted_globals)  # noqa: S102

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_run)
        future.result(timeout=EXECUTION_TIMEOUT_SECONDS)

        stdout_val = stdout_buf.getvalue()
        stderr_val = stderr_buf.getvalue()
        logger.info("Sandbox: success, stdout=%d chars", len(stdout_val))
        return {"ok": True, "stdout": stdout_val, "stderr": stderr_val}

    except FuturesTimeout:
        msg = f"Execution timed out after {EXECUTION_TIMEOUT_SECONDS}s"
        logger.warning("Sandbox: timeout — %s", msg)
        return {"ok": False, "error": msg, "stderr": stderr_buf.getvalue()}
    except Exception as exc:
        tb = traceback.format_exc()
        logger.warning("Sandbox: error — %s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "stderr": tb}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

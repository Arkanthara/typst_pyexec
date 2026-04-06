"""Persistent Jupyter kernel manager with reconnection support."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from queue import Empty

import jupyter_client

logger = logging.getLogger(__name__)

_CONNECTION_FILE = "kernel_connection.json"


def _default_timeout_seconds() -> float:
    """Return default execution timeout, overridable via env var.

    ``TYPST_PYEXEC_CELL_TIMEOUT`` accepts a positive number of seconds.
    Invalid values fall back to 600 seconds.
    """
    raw = os.environ.get("TYPST_PYEXEC_CELL_TIMEOUT", "").strip()
    if not raw:
        return 600.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid TYPST_PYEXEC_CELL_TIMEOUT=%r; using default 600s.",
            raw,
        )
        return 600.0
    if value <= 0:
        logger.warning(
            "Non-positive TYPST_PYEXEC_CELL_TIMEOUT=%r; using default 600s.",
            raw,
        )
        return 600.0
    return value


_TIMEOUT = _default_timeout_seconds()


class KernelManager:
    """Manage a single persistent IPython kernel.

    The kernel is started once and reused across builds to preserve the
    Python namespace.  Connection information is stored in
    *state_dir/kernel_connection.json* so that a subsequent process can
    reconnect without restarting the kernel.

    Parameters
    ----------
    state_dir:
        Directory in which to store the connection file.
    """

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._conn_file = state_dir / _CONNECTION_FILE
        self._km: jupyter_client.KernelManager | None = None
        self._kc: jupyter_client.BlockingKernelClient | None = None
        self._has_namespace_state = False

    @property
    def has_namespace_state(self) -> bool:
        """Whether this process believes the kernel has user namespace state."""
        return self._has_namespace_state

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ensure_running(self) -> None:
        """Start or reconnect to the kernel.

        Tries to reconnect to an existing kernel first.  Falls back to
        starting a fresh one if reconnection fails.
        """
        if self._kc is not None and self._is_alive():
            return

        if self._conn_file.exists():
            try:
                self._reconnect()
                logger.info("Reconnected to existing kernel.")
                return
            except Exception as exc:
                logger.debug("Reconnect failed (%s) — starting fresh kernel.", exc)

        self._start_fresh()

    def shutdown(self) -> None:
        """Gracefully shut down the kernel and remove the connection file."""
        if self._km is not None:
            logger.info("Shutting down kernel…")
            try:
                self._km.shutdown_kernel(now=True)
            except Exception:
                pass
        if self._conn_file.exists():
            self._conn_file.unlink(missing_ok=True)
        self._km = None
        self._kc = None
        self._has_namespace_state = False

    def restart(self) -> None:
        """Hard-restart the kernel (called on crash recovery)."""
        logger.warning("Restarting kernel after crash…")
        if self._km is not None:
            try:
                self._km.restart_kernel()
                self._kc = self._km.blocking_client()
                self._kc.start_channels()
                self._kc.wait_for_ready(timeout=_TIMEOUT)
                logger.info("Kernel restarted successfully.")
                self._has_namespace_state = False
                return
            except Exception as exc:
                logger.debug("In-place restart failed: %s — starting fresh.", exc)
        self._start_fresh()

    def initialize_runtime(self) -> None:
        """Run one-time initialization code in the kernel.

        Sets up matplotlib and imports the figure export module.
        """
        self.ensure_running()
        try:
            init_code = (
                "import os\n"
                "import matplotlib\n"
                "matplotlib.use('Agg')\n"
                "import matplotlib.pyplot as plt\n"
                "plt.ioff()\n"
                "from typst_pyexec.runtime.figure_export import CellFigureContext, save_figures_and_metadata, setup_figure_tracking\n"
                "setup_figure_tracking()\n"
            )
            result = self.execute(init_code)
            if result.get("status") == "error":
                logger.warning(
                    "Runtime initialization failed (non-fatal): %s",
                    result.get("error", ""),
                )
        except Exception as exc:
            logger.warning("Runtime initialization error (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, code: str, timeout: float = _TIMEOUT) -> dict:
        """Execute *code* in the kernel and return a result dict.

        Returns
        -------
        dict with keys:
          ``stdout`` (str), ``stderr`` (str),
          ``display_data`` (list[dict]), ``error`` (str | None),
          ``status`` (``"ok"`` | ``"error"``)
        """
        self.ensure_running()
        assert self._kc is not None  # noqa: S101

        msg_id = self._kc.execute(code)
        result = _empty_result()

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result["error"] = "Execution timed out."
                result["status"] = "error"
                # The kernel may still be running user code after timeout;
                # restart so the next cell starts from a known-good state.
                self.restart()
                break
            try:
                msg = self._kc.get_iopub_msg(timeout=min(remaining, 1.0))
            except Exception:
                continue

            msg_type = msg["msg_type"]
            content = msg["content"]

            if msg_type == "stream":
                key = "stdout" if content.get("name") == "stdout" else "stderr"
                result[key] += content.get("text", "")

            elif msg_type in ("display_data", "execute_result"):
                result["display_data"].append(content.get("data", {}))

            elif msg_type == "error":
                result["error"] = "\n".join(content.get("traceback", []))
                result["status"] = "error"

            elif msg_type == "status":
                if (
                    content.get("execution_state") == "idle"
                    and msg.get("parent_header", {}).get("msg_id") == msg_id
                ):
                    break

        self._has_namespace_state = True
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_fresh(self) -> None:
        km = jupyter_client.KernelManager(kernel_name="python3")
        km.start_kernel()
        kc = km.blocking_client()
        kc.start_channels()
        kc.wait_for_ready(timeout=_TIMEOUT)

        # Persist connection info
        conn_info = km.get_connection_info()
        serializable = _serialize_connection_info(conn_info)
        self._conn_file.write_text(json.dumps(serializable, indent=2), encoding="utf-8")

        self._km = km
        self._kc = kc
        self._has_namespace_state = False
        pid = getattr(getattr(km.provisioner, "process", None), "pid", None)
        logger.info("Started fresh Python kernel (pid=%s).", pid)

    def _reconnect(self) -> None:
        raw = json.loads(self._conn_file.read_text(encoding="utf-8"))
        conn_info = _deserialize_connection_info(raw)
        kc = jupyter_client.BlockingKernelClient()
        kc.load_connection_info(conn_info)
        kc.start_channels()
        kc.wait_for_ready(timeout=_TIMEOUT)
        self._kc = kc
        self._km = None  # We do not own this kernel process
        self._has_namespace_state = True

    def _is_alive(self) -> bool:
        if self._kc is None:
            return False
        try:
            # Kernel-info is a lightweight liveness probe.
            self._kc.kernel_info(reply=True, timeout=1)
            return True
        except (RuntimeError, TimeoutError, OSError, Empty):
            return False
        except Exception:
            return False


def _empty_result() -> dict:
    return {
        "stdout": "",
        "stderr": "",
        "display_data": [],
        "error": None,
        "status": "ok",
    }


def _serialize_connection_info(conn_info: dict) -> dict:
    """Return a JSON-safe copy of Jupyter connection info.

    jupyter_client may expose the HMAC key as bytes, which is not JSON
    serializable. We encode bytes as base64 and restore them on load.
    """
    out: dict = {}
    for key, value in conn_info.items():
        if isinstance(value, bytes):
            out[key] = {
                "__typst_pyexec_bytes__": True,
                "encoding": "base64",
                "value": base64.b64encode(value).decode("ascii"),
            }
        else:
            out[key] = value
    return out


def _deserialize_connection_info(raw: dict) -> dict:
    """Restore connection info values encoded by _serialize_connection_info."""
    out: dict = {}
    for key, value in raw.items():
        if (
            isinstance(value, dict)
            and value.get("__typst_pyexec_bytes__") is True
            and value.get("encoding") == "base64"
        ):
            out[key] = base64.b64decode(value.get("value", ""))
        else:
            out[key] = value
    return out

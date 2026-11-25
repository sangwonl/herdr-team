"""Thin wrapper over the herdr CLI. Every call is argv (no shell) and returns parsed JSON."""
import json
import os
import subprocess


class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def run(*args: str, timeout: float = 120.0) -> dict:
    """Run `herdr <args>` and return the response's `result`. Raises HerdrError on any failure."""
    binary = os.environ.get("HERDR_BIN_PATH", "herdr")
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise HerdrError("herdr_not_found", f"herdr executable not found: {binary}")
    except subprocess.TimeoutExpired:
        raise HerdrError("timeout", f"herdr {' '.join(args[:2])} did not respond ({timeout:.0f}s)")
    except OSError as e:
        raise HerdrError("os_error", f"could not run herdr: {e}")
    if proc.returncode != 0:
        raw = (proc.stderr or proc.stdout).strip()
        try:
            err = json.loads(raw.splitlines()[-1])["error"]
            raise HerdrError(str(err.get("code", "error")), str(err.get("message", raw)))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise HerdrError("error", raw or f"herdr {' '.join(args[:2])} failed (exit code {proc.returncode})")
    try:
        return json.loads(proc.stdout).get("result", {})
    except (ValueError, AttributeError):
        # A few commands print nothing useful on success.
        return {}


def agents() -> list[dict]:
    return run("agent", "list").get("agents", [])


def workspaces() -> list[dict]:
    return run("workspace", "list").get("workspaces", [])


def panes(workspace_id: str) -> list[dict]:
    return run("pane", "list", "--workspace", workspace_id).get("panes", [])


def tabs(workspace_id: str) -> list[dict]:
    return run("tab", "list", "--workspace", workspace_id).get("tabs", [])


def text(*args: str, timeout: float = 30.0) -> str:
    """Run a herdr command that prints plain text (e.g. `agent read`). Empty string on failure."""
    binary = os.environ.get("HERDR_BIN_PATH", "herdr")
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""

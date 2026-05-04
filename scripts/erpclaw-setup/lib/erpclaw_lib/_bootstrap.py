"""Bootstrap self-heal for the deployed erpclaw_lib at ~/.openclaw/erpclaw/lib/.

Problem: ClawHub `update` does NOT re-run the foundation post-install hook.
After a v3.5.1 -> v4.0.0 upgrade, new lib files (e.g., gl_invariants.py) were
not copied to the shared location, breaking addon imports.

Solution: every foundation action invocation calls ensure_lib_synced() at the
top of scripts/db_query.py. The function compares a small version marker file
(~/.openclaw/erpclaw/lib/.erpclaw_lib_version) against the bundled
erpclaw_lib.__version__. On mismatch, it re-runs the lib copy and updates
the marker. ~50µs cost on the happy path (one fstat + one read).

Safety:
  - flock(LOCK_EX) on .bootstrap.lock to prevent concurrent re-installs from
    racing on shutil.copy2 mid-write.
  - Atomic marker write via tempfile + os.replace (tempfile colocated with
    target dir to avoid EXDEV).
  - Per-re-install audit-log entry to ~/.openclaw/erpclaw/logs/bootstrap.log.
  - Honors ERPCLAW_DISABLE_BOOTSTRAP=1 env var.
  - All failure modes degrade silently (warning to stderr, action continues).

Note: copy logic is inlined here (not delegated to erpclaw-setup/db_query.py)
so the every-action self-heal path doesn't require importing the setup
module's heavy dependency chain (which itself imports erpclaw_lib — the very
thing we're trying to repair).
"""
import fcntl
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

LIB_DIR = os.path.expanduser("~/.openclaw/erpclaw/lib")
MARKER_PATH = os.path.join(LIB_DIR, ".erpclaw_lib_version")
LOCK_PATH = os.path.join(LIB_DIR, ".bootstrap.lock")
LOG_PATH = os.path.expanduser("~/.openclaw/erpclaw/logs/bootstrap.log")


def _read_marker() -> str:
    try:
        with open(MARKER_PATH, "r") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _write_marker_atomic(version: str) -> None:
    os.makedirs(LIB_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=LIB_DIR, prefix=".version-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(version + "\n")
        os.replace(tmp_path, MARKER_PATH)
    except OSError:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _audit_log(old_version: str, new_version: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        line = f"{ts} bootstrap re-sync: {old_version or '<none>'} -> {new_version}\n"
        with open(LOG_PATH, "a") as fh:
            fh.write(line)
    except OSError:
        pass


def _copy_bundled_lib(bundled_lib_root: str) -> int:
    """Copy bundled erpclaw_lib/ tree to the deployed location.

    bundled_lib_root is the directory CONTAINING erpclaw_lib/ (i.e., the
    bundled `lib/` dir). Recursive walk, .py files only, skips __pycache__.
    Returns count of files copied. Idempotent under concurrent calls when
    holding the bootstrap lock.
    """
    src_root = os.path.join(bundled_lib_root, "erpclaw_lib")
    dst_root = os.path.join(LIB_DIR, "erpclaw_lib")
    if not os.path.isdir(src_root):
        return 0
    copied = 0
    for cur_dir, sub_dirs, files in os.walk(src_root):
        sub_dirs[:] = [d for d in sub_dirs if d != "__pycache__"]
        rel = os.path.relpath(cur_dir, src_root)
        dst_dir = dst_root if rel == "." else os.path.join(dst_root, rel)
        os.makedirs(dst_dir, exist_ok=True)
        for fname in files:
            if fname.endswith(".py"):
                shutil.copy2(os.path.join(cur_dir, fname),
                             os.path.join(dst_dir, fname))
                copied += 1
    return copied


def ensure_lib_synced(bundled_version: str, bundled_lib_root: str) -> None:
    """Compare deployed lib marker to bundled version; re-install if stale.

    Args:
        bundled_version: erpclaw_lib.__version__ from the BUNDLED lib (the
            target version we want deployed). Caller imports this from the
            bundled path, NOT from the deployed path (which may be stale).
        bundled_lib_root: filesystem path to the bundled `lib/` dir
            (contains the erpclaw_lib/ subdir to be copied).

    Behavior:
        - If ERPCLAW_DISABLE_BOOTSTRAP=1, return immediately.
        - If marker matches bundled_version, return immediately (~50µs).
        - Otherwise, take exclusive lock, double-check marker, copy lib
          tree, write new marker, append audit log entry.
    """
    if os.environ.get("ERPCLAW_DISABLE_BOOTSTRAP") == "1":
        return

    deployed = _read_marker()
    if deployed == bundled_version:
        return

    os.makedirs(LIB_DIR, exist_ok=True)
    try:
        lock_fh = open(LOCK_PATH, "w")
    except OSError as exc:
        print(f"warning: bootstrap lock unavailable ({exc}); skipping self-heal",
              file=sys.stderr)
        return

    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        deployed = _read_marker()
        if deployed == bundled_version:
            return
        try:
            _copy_bundled_lib(bundled_lib_root)
            _write_marker_atomic(bundled_version)
            _audit_log(deployed, bundled_version)
        except Exception as exc:
            print(f"warning: bootstrap self-heal failed: {exc}", file=sys.stderr)
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fh.close()

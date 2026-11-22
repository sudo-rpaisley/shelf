"""Filesystem publication owned by a managed connection's real outer outcome.

Lock order is SQLite writer -> publication fence -> bytes. Direct legacy file
writers take only the fence around their final mutation and MUST release it
before doing SQL. Critical callbacks are filesystem-only, synchronous and never
reacquire SQLite (a failed native commit may already have unlocked the engine).
"""

from collections.abc import Callable
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat

try:
    import fcntl
except ImportError:  # No thread-lock fallback: an unsupported platform fails closed.
    fcntl = None


class TransactionResourceError(RuntimeError):
    pass


def _namespace(publication_dir):
    return Path(publication_dir).resolve()


class _PublicationFence:
    """One stable, non-served inode; never unlink it during ordinary cleanup."""

    def __init__(self, namespace):
        if fcntl is None or not hasattr(os, "O_NOFOLLOW"):
            raise TransactionResourceError("Process-safe cover publication locking is unavailable")
        if namespace == namespace.parent:
            raise TransactionResourceError("Publication directory must have a non-served parent for its lock")
        digest = hashlib.sha256(os.fsencode(namespace)).hexdigest()
        self.path = namespace.parent / f".shelf-cover-publication-{digest}.lock"
        self.fd = None
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
                raise TransactionResourceError("Publication lock must be a private, owned regular file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            current = os.stat(self.path, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise TransactionResourceError("Publication lock inode changed")
            self.fd = fd
        except BaseException:
            os.close(fd)
            raise

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            # Closing the last descriptor releases flock, including on errors.
            os.close(fd)


def _raise_errors(errors):
    if errors:
        primary = errors[0]
        if len(errors) > 1:
            primary.resource_cleanup_errors = (*getattr(primary, "resource_cleanup_errors", ()), *errors[1:])
            for error in errors[1:]:
                primary.add_note(f"Resource cleanup also failed: {type(error).__name__}: {error}")
        raise primary


class TransactionResource:
    """cancel() follows successful local undo; rollback() performs it safely.

    Publishers MUST use rollback() when local recovery can fail: a failure stays
    registered and prevents a later unrelated commit from discarding recovery.
    """

    def __init__(self, owner, after_commit, after_rollback):
        self._owner = owner
        self._after_commit = after_commit
        self._after_rollback = after_rollback
        self._active = True
        self._rollback_required = False

    def cancel(self) -> None:
        if self._active:
            self._active = False
            self._owner.remove(self)

    def rollback(self) -> None:
        """Undo locally under the retained fence; cancel only after success."""
        if self._active:
            self._rollback_required = True
            self._after_rollback()
            self.cancel()

    def _finish(self, committed: bool) -> None:
        if self._active:
            self._active = False
            callback = self._after_commit if committed else self._after_rollback
            callback()


class _Resources:
    def __init__(self, conn, namespace, register):
        self.conn = conn
        self.namespace = namespace
        self.entries = []
        self.fence = None
        self._finishing = False
        self.unregister = register(
            before_commit=self.check_commit,
            after_commit=lambda: self.finish(True),
            before_rollback=lambda: self.finish(False),
            after_outcome=self.detach,
        )

    def acquire(self):
        if self.fence is None:
            self.fence = _PublicationFence(self.namespace)

    def release(self):
        if self.fence is not None:
            fence, self.fence = self.fence, None
            fence.close()

    def detach(self):
        try:
            self.release()
        finally:
            self.unregister()
            if getattr(self.conn, "_transaction_resources", None) is self:
                self.conn._transaction_resources = None

    def remove(self, entry):
        if entry in self.entries:
            self.entries.remove(entry)
        if not self.entries and not self._finishing:
            # Keep the namespace binding until the native outer outcome, but
            # successful last local recovery no longer needs the OS fence.
            self.release()

    def check_commit(self):
        if any(entry._rollback_required for entry in self.entries):
            raise TransactionResourceError("Local resource recovery failed; outer rollback is required")

    def finish(self, committed):
        self._finishing = True
        entries, self.entries = self.entries, []
        errors = []
        try:
            for entry in entries if committed else reversed(entries):
                try:
                    entry._finish(committed)
                except BaseException as exc:
                    errors.append(exc)
        finally:
            self._finishing = False
        # The connection calls detach AFTER the native outcome, even on failure.
        # A failed undo's backup is not a successful-cleanup resource.
        _raise_errors(errors)


def register_transaction_resource(
    conn, *, publication_dir, after_commit: Callable[[], None], after_rollback: Callable[[], None],
) -> TransactionResource:
    """Acquire the fence BEFORE reading any destination/preimage or changing bytes.

    An explicit protected outcome capability is required: ordinary callback lists
    do not qualify. The caller must already own SQLite's write transaction.
    """
    register = getattr(conn, "_register_critical_resource_outcome", None)
    check = getattr(conn, "_check_resource_transaction", None)
    if not callable(register) or not callable(check):
        raise TransactionResourceError("Critical resources require protected commit AND rollback outcomes")
    check()
    if not conn.in_transaction:
        raise TransactionResourceError("Critical resources require an active outer transaction")
    if not callable(after_commit) or not callable(after_rollback):
        raise TypeError("Resource callbacks must be callable")
    namespace = _namespace(publication_dir)
    owner = getattr(conn, "_transaction_resources", None)
    if owner is not None and (owner.conn is not conn or owner.namespace != namespace):
        raise TransactionResourceError("Only one publication namespace is allowed per outer transaction")
    created = owner is None
    if created:
        owner = _Resources(conn, namespace, register)
        conn._transaction_resources = owner
    try:
        owner.acquire()
    except BaseException:
        if created:
            owner.detach()
        raise
    entry = TransactionResource(owner, after_commit, after_rollback)
    owner.entries.append(entry)
    return entry


@contextmanager
def cover_publication_guard(publication_dir, *, conn=None):
    """Fence a final file mutation; only the explicit owning conn may borrow.

    A non-borrowing guard must finish before the caller performs any SQL. Do not
    acquire a guard and then begin a SQLite transaction or register a resource.
    """
    namespace = _namespace(publication_dir)
    owner = getattr(conn, "_transaction_resources", None) if conn is not None else None
    if owner is not None:
        if owner.conn is not conn or owner.namespace != namespace:
            raise TransactionResourceError("Cannot borrow another publication namespace or connection's fence")
        if owner.fence is not None:
            # Filesystem helpers may borrow inside critical recovery, including
            # after an engine auto-unlock. That never permits SQL or a new scope.
            if not owner._finishing:
                conn._check_resource_transaction()
            yield
            return
        conn._check_resource_transaction()
    fence = _PublicationFence(namespace)
    try:
        yield
    finally:
        fence.close()

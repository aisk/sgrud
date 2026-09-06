"""Exceptions raised by sgrud."""

from __future__ import annotations


class SgrudError(Exception):
    """Base class for all sgrud errors."""


class ProcessExited(SgrudError):
    """The target process is gone."""

    def __init__(self, pid: int, returncode: int | None = None):
        self.pid = pid
        self.returncode = returncode
        msg = f"process {pid} has exited"
        if returncode is not None:
            msg += f" with code {returncode}"
        super().__init__(msg)


class AttachError(SgrudError):
    """Could not attach to the target process.

    ``hint`` carries a human readable suggestion on how to fix it.
    """

    def __init__(self, pid: int, message: str, hint: str | None = None, *, transient: bool = False):
        self.pid = pid
        self.hint = hint
        #: True when the target may simply still be starting up.
        self.transient = transient
        text = f"cannot attach to process {pid}: {message}"
        if hint:
            text += f"\n{hint}"
        super().__init__(text)


class NotSupported(SgrudError):
    """The platform or interpreter lacks a required feature."""

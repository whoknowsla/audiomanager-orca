"""Typed errors for the Audio Manager Orca extension.

Every backend failure is reported as one of these types so that command code can
map a failure onto a short spoken message without inspecting subprocess output.
"""

from __future__ import annotations

__all__ = [
    "AmbiguousApplication",
    "AudioManagerError",
    "CommandMissing",
    "ConnectionFailed",
    "MalformedBackendOutput",
    "ObjectDisappeared",
    "OperationTimedOut",
    "PermissionDenied",
    "UnsupportedOperation",
    "user_message_for",
]


class AudioManagerError(Exception):
    """Base class for all audio backend errors."""

    USER_MESSAGE = "The audio manager could not complete that request."

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.USER_MESSAGE)
        self.detail = detail

    def user_message(self) -> str:
        """Returns the short message to present to the user."""

        return self.USER_MESSAGE


class CommandMissing(AudioManagerError):
    """Raised when a required command-line tool is not on PATH."""

    USER_MESSAGE = "The PipeWire command line tools are not installed."


class ConnectionFailed(AudioManagerError):
    """Raised when the PipeWire remote cannot be reached."""

    USER_MESSAGE = "PipeWire is not available."


class PermissionDenied(AudioManagerError):
    """Raised when PipeWire refuses an operation for policy reasons."""

    USER_MESSAGE = "PipeWire did not allow this audio change."


class OperationTimedOut(AudioManagerError):
    """Raised when an operation exceeded the configured timeout."""

    USER_MESSAGE = "The audio service did not respond."


class ObjectDisappeared(AudioManagerError):
    """Raised when a selected node no longer exists or was replaced."""

    USER_MESSAGE = "The selected audio device is no longer available."


class UnsupportedOperation(AudioManagerError):
    """Raised when the backend cannot perform the requested operation."""

    USER_MESSAGE = "This audio operation is not supported on this system."


class AmbiguousApplication(AudioManagerError):
    """Raised when an application cannot be matched to streams safely."""

    USER_MESSAGE = "The current window's audio could not be identified safely."


class MalformedBackendOutput(AudioManagerError):
    """Raised when a tool produced output that could not be parsed."""

    USER_MESSAGE = "The audio service returned unexpected output."


def user_message_for(error: BaseException) -> str:
    """Returns the short user-facing message for error."""

    if isinstance(error, AudioManagerError):
        return error.user_message()
    return AudioManagerError.USER_MESSAGE

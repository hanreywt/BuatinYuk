"""ComfyUI client errors.

Each carries a `user_message`: a short, safe sentence for a Telegram user. It never
contains a path, a node id, a stack trace, or anything about this machine. The full
detail stays in `str(exc)` and goes to the logs only.
"""

from __future__ import annotations


class ComfyError(Exception):
    """Base class. Subclasses set a safe user-facing message."""

    user_message = "Generation failed. Please try again."


class ComfyUnavailable(ComfyError):
    user_message = "The image generator is offline right now. Please try again shortly."


class ComfyTimeout(ComfyError):
    user_message = "That took too long and was stopped. Try a smaller or simpler request."


class ComfyRejectedWorkflow(ComfyError):
    """ComfyUI refused the graph - bad node, missing model, invalid value."""

    user_message = "That request could not be prepared. Please try different settings."


class ComfyExecutionFailed(ComfyError):
    """The graph was accepted but failed partway through."""

    user_message = "Generation failed while running. Please try again."


class ComfyOutputMissing(ComfyError):
    user_message = "Generation finished but produced no usable output. Please try again."


class ComfyInterrupted(ComfyError):
    user_message = "That job was cancelled."

"""One place to build the Anthropic client."""

from __future__ import annotations

import os


def client():
    """Anthropic client; adds the workspace header when the key is not workspace-scoped."""
    import anthropic

    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    headers = {"anthropic-workspace-id": workspace} if workspace else None
    return anthropic.Anthropic(default_headers=headers)

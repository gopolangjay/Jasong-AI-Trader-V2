"""One release identity for the engine and its broker/API gateways."""
import os

VERSION = "6.16-ai-discretionary-demo-v2"


def build_info() -> dict[str, str]:
    return {"version": VERSION, "commit_sha": os.getenv("RENDER_GIT_COMMIT", "unknown")}

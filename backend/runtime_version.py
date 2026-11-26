"""One release identity for the engine and its broker/API gateways."""
import os

VERSION = "6.15-openai-analyst-v3"


def build_info() -> dict[str, str]:
    return {"version": VERSION, "commit_sha": os.getenv("RENDER_GIT_COMMIT", "unknown")}

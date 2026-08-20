from __future__ import annotations

import os

import uvicorn

import execution_reliability
import main as jasong_main


def _install_runtime_health() -> None:
    system = getattr(jasong_main, "V693_SPECIALIST_SYSTEM", None)
    if not isinstance(system, dict):
        system = getattr(jasong_main, "V694_SPECIALIST_SYSTEM", None)
    if not isinstance(system, dict):
        system = {}

    broker = getattr(jasong_main, "IG_DEMO_BROKER", None)
    execution_reliability.install_execution_health_route(
        jasong_main.app,
        system=system,
        broker=broker,
    )


_install_runtime_health()


if __name__ == "__main__":
    uvicorn.run(
        jasong_main.app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        proxy_headers=True,
    )

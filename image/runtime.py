"""The distribution owns profile preparation and the gateway on the platform's durable mount."""

import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from aide import serve as serve_aide
from control import serve
from inference import Availability
from inference import maintain as maintain_inference
from opentelemetry import trace
from processes import managed_process, ready_process
from pydantic import BaseModel, ConfigDict, SecretStr

from research_core import telemetry


class RuntimeRecovery(BaseModel):
    event: Literal["workspace_runtime_recovery"] = "workspace_runtime_recovery"
    workspace_id: str
    boot_id: str
    reason: Literal["execution_uncertain"] = "execution_uncertain"
    completed_recoveries: int
    timestamp: datetime


class Bootstrap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    platform_url: str
    credential: SecretStr


HERMES = "/opt/hermes/.venv/bin/hermes"
PYTHON = "/opt/hermes/.venv/bin/python"
RESEARCH = "/opt/workspace-env/bin/research"
SOURCE = Path("/opt/distribution")
HOME = Path("/workspace/host")
PROFILES = ("orchestrator", "researcher")
WATCHDOG_SECONDS = 3600


async def command(*args: str, env: dict[str, str]) -> None:
    async with managed_process(*args, env=telemetry.process_env(env)) as process:
        code = await process.wait()
    if code:
        raise RuntimeError(f"{args[0]} exited {code}")


async def run(stack: AsyncExitStack) -> None:
    boot_id = uuid4().hex
    workspace_state = (
        "retained" if (HOME / "profiles/orchestrator/.softmax-prepared").exists() else "fresh"
    )
    bootstrap = Bootstrap.model_validate_json(Path("/run/workspace/bootstrap.json").read_text())
    os.environ["IDE_WORKSPACE_ID"] = bootstrap.workspace_id
    os.environ["IDE_RUNTIME_ID"] = boot_id
    image_id = Path("/opt/distribution/image-id").read_text().strip()
    os.environ["IDE_IMAGE"] = f"distribution-{image_id}"
    startup = telemetry.tracer().start_span(
        "workspace.startup", attributes={"workspace.state": workspace_state}
    )
    startup_context = trace.set_span_in_context(startup)

    def finish_startup() -> None:
        if startup.is_recording():
            startup.set_attribute("startup.complete", False)
            startup.set_status(trace.StatusCode.ERROR)
            startup.end()

    stack.callback(finish_startup)
    HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    lease_file = HOME / ".inference-lease"
    lease_file.unlink(missing_ok=True)
    # The inference state beside it survives restarts: a budget block names the spent key and
    # lasts until the lease file names a different one.
    inference = Availability(lease_file)
    Path("/tmp/hermes-runtime").mkdir(exist_ok=True, mode=0o700)
    key_file = HOME / ".gateway-key"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(32))
        key_file.chmod(0o600)
    key = key_file.read_text()
    common = {
        **os.environ,
        "IDE_RUNTIME_ID": boot_id,
        "IDE_WORKSPACE_ID": bootstrap.workspace_id,
        "API_SERVER_ENABLED": "true",
        "API_SERVER_HOST": "127.0.0.1",
        "API_SERVER_PORT": "8642",
        "API_SERVER_KEY": key,
        "GATEWAY_MULTIPLEX_PROFILES": "true",
        "IDE_RESEARCHER_PROFILES": "researcher",
        "IDE_LOCAL_STT_MODEL": "/opt/models/whisper-base",
        "IDE_INFERENCE_LEASE_FILE": str(lease_file),
        # Where the gateway plugin relays reasoning deltas to the AIDE backend (image/aide.py).
        "IDE_REASONING_RELAY": "127.0.0.1:8651",
        "SOFTMAX_PLAYER_CREDENTIAL": bootstrap.credential.get_secret_value(),
        "COGAMES_API_URL": bootstrap.platform_url.removesuffix("/observatory"),
    }
    # Multiplexed gateway startup reads the host profile's dotenv after scoping its environment.
    host_dotenv = HOME / ".env"
    host_dotenv.write_text(
        "".join(
            f"{name}={json.dumps(common[name])}\n"
            for name in (
                "API_SERVER_ENABLED",
                "API_SERVER_HOST",
                "API_SERVER_PORT",
                "API_SERVER_KEY",
                "GATEWAY_MULTIPLEX_PROFILES",
            )
        )
    )
    host_dotenv.chmod(0o600)
    if any(not (HOME / "profiles" / role).exists() for role in PROFILES):
        # Templates contain configuration only; each workspace owns its freshly created store.
        from research_core import layout
        from research_core.prepare import ensure_stores

        with telemetry.span("workspace.store_prepare", context=startup_context):
            workspace = layout.from_env(common)
            workspace.ensure_dirs()
            ensure_stores(workspace)
    for role in PROFILES:
        with telemetry.span("workspace.profile_install", context=startup_context, role=role):
            home = HOME / "profiles" / role
            fresh = not home.exists()
            applied = home / ".softmax-distribution-image"
            current = applied.is_file() and applied.read_text().strip() == image_id
            trace.get_current_span().set_attribute("profile.updated", not current)
            if home.exists() and not current:
                # An interrupted upgrade must not leave an old image's success marker behind.
                # Otherwise rolling back could reuse partially replaced profile content.
                applied.unlink(missing_ok=True)
                await command(HERMES, "profile", "update", role, "--yes", env=common)
            elif fresh:
                staging = home.with_name(f".{role}.installing")
                if staging.exists():
                    shutil.rmtree(staging)
                shutil.copytree(SOURCE / "prepared-profiles" / role, staging)
                staging.rename(home)
        profile_env = {**common, "HERMES_HOME": str(home)}
        # The watcher reads the gateway key from its profile dotenv.
        dotenv = home / ".env"
        dotenv.write_text(
            "".join(
                f"{name}={json.dumps(value)}\n"
                for name, value in profile_env.items()
                if name
                in {
                    "API_SERVER_KEY",
                    "API_SERVER_HOST",
                    "API_SERVER_PORT",
                    "IDE_WORKSPACE",
                    "IDE_WORKSPACE_ENV",
                    "BUILDKIT_HOST",
                    "IDE_RESEARCHER_PROFILES",
                    "IDE_INFERENCE_LEASE_FILE",
                    "SOFTMAX_PLAYER_CREDENTIAL",
                    "COGAMES_API_URL",
                    "OPENROUTER_API_KEY",
                }
            )
        )
        dotenv.chmod(0o600)
        with telemetry.span("workspace.profile_prepare", context=startup_context, role=role):
            if not fresh:
                await command(
                    PYTHON,
                    str(home / "scripts/prepare.py"),
                    "--source",
                    str(SOURCE / role),
                    "--hermes",
                    HERMES,
                    env=profile_env,
                )
        # Publish only after installation and preparation both succeed. A failed upgrade retries.
        pending = applied.with_suffix(".tmp")
        pending.write_text(image_id + "\n")
        pending.replace(applied)

    async def observe_ready(phase: str, ready: asyncio.Event) -> None:
        with telemetry.span(f"workspace.{phase}", context=startup_context):
            await ready.wait()
            trace.get_current_span().set_attribute("readiness.complete", True)

    inference_ready = asyncio.Event()
    relay_connected = asyncio.Event()
    async with asyncio.TaskGroup() as group:
        group.create_task(observe_ready("inference_ready", inference_ready))
        group.create_task(
            maintain_inference(
                platform_url=bootstrap.platform_url,
                workspace_id=bootstrap.workspace_id,
                credential=bootstrap.credential.get_secret_value(),
                homes=(HOME, *(HOME / "profiles" / role for role in PROFILES)),
                availability=inference,
                lease_file=lease_file,
                ready=inference_ready,
            )
        )
        profile_env = {**common, "HERMES_HOME": str(HOME / "profiles/orchestrator")}
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8642", headers={"Authorization": f"Bearer {key}"}
        ) as client:
            with telemetry.span("workspace.gateway_ready", context=startup_context):
                from hermes_cli.config import require_readable_config_before_write, set_config_value

                # Hermes needs both the host config value and WATCHDOG_USEC to send READY=1.
                gateway_config = require_readable_config_before_write().get("gateway")
                if (
                    not isinstance(gateway_config, dict)
                    or gateway_config.get("systemd_watchdog_seconds") != WATCHDOG_SECONDS
                ):
                    set_config_value("gateway.systemd_watchdog_seconds", str(WATCHDOG_SECONDS))
                gateway = await stack.enter_async_context(
                    ready_process(
                        PYTHON,
                        "-m",
                        "gateway.run",
                        env={**common, "WATCHDOG_USEC": str(WATCHDOG_SECONDS * 1_000_000)},
                        timeout=180,
                    )
                )
                # One contract check after the listener's notification, never a readiness poll.
                (await client.get("/v1/capabilities")).raise_for_status()
            with telemetry.span("workspace.application_startup", context=startup_context):
                seam = await stack.enter_async_context(
                    ready_process(
                        RESEARCH,
                        "serve",
                        "--host",
                        "0.0.0.0",
                        "--wake",
                        env=telemetry.process_env(profile_env),
                        timeout=30,
                    )
                )
                async with httpx.AsyncClient(base_url="http://127.0.0.1:8650") as seam_client:
                    (await seam_client.get("/health")).raise_for_status()

            async def reconcile_uncertain_execution() -> None:
                async with httpx.AsyncClient(base_url="http://127.0.0.1:8650") as observer:
                    while True:
                        response = await observer.get("/status")
                        response.raise_for_status()
                        activity = response.json()
                        if activity["restart_required"]:
                            print(
                                RuntimeRecovery(
                                    workspace_id=bootstrap.workspace_id,
                                    boot_id=boot_id,
                                    completed_recoveries=activity["runtime_recovery_count"],
                                    timestamp=datetime.now(UTC),
                                ).model_dump_json(),
                                flush=True,
                            )
                            raise RuntimeError(
                                "Recycling runtime to settle an uncertain native execution"
                            )
                        await asyncio.sleep(1)

            recovery = group.create_task(reconcile_uncertain_execution())
            startup.set_attribute("startup.complete", True)
            startup.end()

            group.create_task(observe_ready("relay_connected", relay_connected))
            control = group.create_task(
                serve(
                    platform_url=bootstrap.platform_url,
                    workspace_id=bootstrap.workspace_id,
                    credential=bootstrap.credential.get_secret_value(),
                    env=profile_env,
                    connected=relay_connected,
                )
            )
            # The AIDE backend runs beside the control connection and gates no startup step; like
            # the services below, its exit recycles the runtime.
            aide_backend = group.create_task(
                serve_aide(
                    platform_url=bootstrap.platform_url,
                    workspace_id=bootstrap.workspace_id,
                    credential=bootstrap.credential.get_secret_value(),
                    env=profile_env,
                    inference=inference,
                )
            )
            await asyncio.wait(
                [
                    group.create_task(gateway.wait()),
                    group.create_task(seam.wait()),
                    control,
                    aide_backend,
                    recovery,
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            raise RuntimeError("A distribution service exited")


async def main() -> None:
    telemetry.configure_logging()
    logging.getLogger("aide").setLevel(logging.INFO)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    try:
        async with AsyncExitStack() as stack, asyncio.TaskGroup() as group:
            runtime = group.create_task(run(stack))
            shutdown = group.create_task(stopping.wait())
            await asyncio.wait([runtime, shutdown], return_when=asyncio.FIRST_COMPLETED)
            runtime.cancel()
            shutdown.cancel()
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


if __name__ == "__main__":
    asyncio.run(main())

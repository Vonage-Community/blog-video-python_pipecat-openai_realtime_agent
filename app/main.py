"""FastAPI server: serves the browser app and a small session API.

    GET  /api/health                 liveness, no secrets
    POST /api/sessions               create a session; returns the browser's credentials
    GET  /api/sessions/{id}          session status            (needs the session key)
    POST /api/sessions/{id}/ready    browser can hear the agent (needs the session key)
    POST /api/sessions/{id}/end      stop the agent, idempotent (needs the session key)

The session key is a random capability returned once by POST /api/sessions. The
browser sends it as `Authorization: Bearer <key>`; a session ID alone grants nothing.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.agent_process import ProcessAgent
from app.config import LOG_LEVELS, ConfigError, Settings, load_settings, setup_logging
from app.session_manager import (
    AgentConnection,
    AgentFactory,
    DemoSession,
    EndReason,
    SessionFinishedError,
    SessionLimitError,
    SessionManager,
    VideoService,
)
from app.video_sessions import VideoApiError, VonageVideo

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Module level on purpose: FastAPI resolves these (string) annotations against
# module globals, so a local alias would silently become a query parameter.
AuthHeader = Annotated[str | None, Header()]


def default_agent_factory(
    settings: Settings, connection: AgentConnection, session: DemoSession
) -> ProcessAgent:
    # Each agent runs in its own process; see app/agent_process.py for why.
    return ProcessAgent(settings, connection, session, label=f"agent {session.id}")


def create_app(
    settings: Settings | None = None,
    video: VideoService | None = None,
    agent_factory: AgentFactory | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        env_level = os.environ.get("LOG_LEVEL", "INFO").upper()
        setup_logging(env_level if env_level in LOG_LEVELS else "INFO")
        manager: SessionManager | None = None
        try:
            loaded = settings or load_settings()
        except ConfigError as exc:
            app.state.config_problems = exc.problems
            logger.error(
                "The server started, but it cannot create sessions until these are fixed "
                "in .env (then restart):\n  - " + "\n  - ".join(exc.problems)
            )
        else:
            app.state.config_problems = []
            app.state.settings = loaded
            manager = SessionManager(
                loaded,
                video or VonageVideo(loaded.vonage_application_id, loaded.vonage_private_key.get()),
                agent_factory or default_agent_factory,
            )
            agent = (
                f"OpenAI Realtime model {loaded.openai_realtime_model}"
                if loaded.agent_mode == "openai"
                else "echo diagnostic (no OpenAI)"
            )
            logger.info(
                f"Ready: agent={agent}; max {loaded.max_active_sessions} session(s) of "
                f"{loaded.max_session_seconds}s; idle timeout {loaded.idle_timeout_seconds}s"
            )
        app.state.manager = manager
        yield
        if manager is not None:
            await manager.shutdown()

    app = FastAPI(title="Vonage + Pipecat + OpenAI Realtime", lifespan=lifespan, docs_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"  # responses carry tokens
        return response

    def manager_of(request: Request) -> SessionManager:
        manager: SessionManager | None = request.app.state.manager
        if manager is None:
            problems = request.app.state.config_problems
            raise HTTPException(
                503,
                {"message": "The server is not configured yet.", "problems": problems},
            )
        return manager

    def authorized_session(request: Request, session_id: str, authorization: str | None):
        manager = manager_of(request)
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Missing session key", headers={"WWW-Authenticate": "Bearer"})
        session = manager.get(session_id)
        if session is None:
            raise HTTPException(404, "Unknown session")
        if not manager.key_matches(session, authorization.removeprefix("Bearer ").strip()):
            raise HTTPException(403, "Wrong session key")
        return manager, session

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/sessions", status_code=201)
    async def create_session(request: Request) -> dict[str, Any]:
        # Requiring JSON forces a CORS preflight, which this API never grants, so
        # other websites cannot start a (billable) session from your browser.
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "Send Content-Type: application/json")
        manager = manager_of(request)
        try:
            created = await manager.create_session()
        except SessionLimitError as exc:
            if exc.retry_after:
                raise HTTPException(
                    429, str(exc), headers={"Retry-After": str(exc.retry_after)}
                ) from exc
            raise HTTPException(409, str(exc)) from exc
        except VideoApiError as exc:
            logger.error(f"Vonage Video API error: {exc}")
            raise HTTPException(502, str(exc)) from exc

        session = created.session
        settings: Settings = request.app.state.settings
        return {
            "id": session.id,
            "sessionKey": session.key,
            # Everything the Web SDK needs to join, and nothing else. The agent's token
            # and every API key stay on the server.
            "applicationId": created.application_id,
            "vonageSessionId": session.vonage_session_id,
            "token": created.browser_token,
            "agentMode": settings.agent_mode,
            **_timing(session),
        }

    @app.get("/api/sessions/{session_id}")
    async def session_status(
        request: Request, session_id: str, authorization: AuthHeader = None
    ) -> dict[str, Any]:
        _, session = authorized_session(request, session_id, authorization)
        return session.status(time.time())

    @app.post("/api/sessions/{session_id}/ready")
    async def session_ready(
        request: Request, session_id: str, authorization: AuthHeader = None
    ) -> dict[str, Any]:
        manager, session = authorized_session(request, session_id, authorization)
        try:
            started = await manager.mark_ready(session)
        except SessionFinishedError as exc:
            raise HTTPException(409, "This session has already ended") from exc
        return {"started": started, **session.status(time.time())}

    @app.post("/api/sessions/{session_id}/end")
    async def session_end(
        request: Request, session_id: str, authorization: AuthHeader = None
    ) -> dict[str, Any]:
        manager, session = authorized_session(request, session_id, authorization)
        await manager.end_session(session, EndReason.LEFT)
        return session.status(time.time())

    # Everything else is the static browser app (index.html, app.js, styles.css).
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def _timing(session: DemoSession) -> dict[str, Any]:
    return {
        "maxSessionSeconds": int(session.expires_at - session.created_at),
        "expiresAt": session.status(session.created_at)["expiresAt"],
    }


app = create_app()

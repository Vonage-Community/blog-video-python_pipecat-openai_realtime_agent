"""Vonage Video API: create routed sessions and per-participant tokens.

The browser and the AI agent join the same session with separate tokens. Each
token carries connection data ({"role": "user"} or {"role": "agent"}) so every
participant can tell the other's stream apart.
"""

from __future__ import annotations

import json
import time
from typing import Literal

from vonage import Auth, HttpClientOptions, Vonage
from vonage_http_client.errors import (
    AuthenticationError,
    ForbiddenError,
    HttpRequestError,
    RateLimitedError,
)
from vonage_video import MediaMode, SessionOptions, TokenOptions, TokenRole

ParticipantRole = Literal["user", "agent"]


class VideoApiError(Exception):
    """A Vonage Video API call failed. The message is safe to show to the browser."""


class VonageVideo:
    def __init__(self, application_id: str, private_key: str, timeout_seconds: int = 10):
        self.application_id = application_id
        self._client = Vonage(
            Auth(application_id=application_id, private_key=private_key),
            HttpClientOptions(timeout=timeout_seconds),
        )

    def create_routed_session(self) -> str:
        """Create a session that uses the Vonage Media Router.

        In a routed session, media flows through Vonage's servers instead of peer to
        peer. Vonage's own Pipecat agent tutorials use routed sessions, so this does too.
        """
        try:
            session = self._client.video.create_session(SessionOptions(media_mode=MediaMode.ROUTED))
        except AuthenticationError as exc:
            raise VideoApiError(
                "Vonage rejected the credentials (401). Check VONAGE_APPLICATION_ID and "
                "that secrets/private.key belongs to that application"
            ) from exc
        except ForbiddenError as exc:
            raise VideoApiError(
                "Vonage refused the request (403). Enable Video for this application with "
                "`vonage apps capabilities update <APPLICATION_ID> video`"
            ) from exc
        except RateLimitedError as exc:
            raise VideoApiError("Vonage rate-limited session creation (429). Retry soon") from exc
        except HttpRequestError as exc:
            status = getattr(exc.response, "status_code", "unknown")
            raise VideoApiError(f"Vonage session creation failed (HTTP {status})") from exc
        except OSError as exc:  # DNS failures, timeouts, refused connections
            raise VideoApiError(
                f"Could not reach the Vonage Video API ({type(exc).__name__}). "
                "Check the container's outbound network access"
            ) from exc
        return session.session_id

    def create_token(self, session_id: str, role: ParticipantRole, ttl_seconds: int) -> str:
        """Sign a token for one participant. Signing is local; no network call."""
        token = self._client.video.generate_client_token(
            TokenOptions(
                session_id=session_id,
                role=TokenRole.PUBLISHER,
                connection_data=json.dumps({"role": role}),
                # Always pass exp explicitly: TokenOptions(exp=None) fails validation.
                exp=int(time.time()) + ttl_seconds,
            )
        )
        return token.decode()

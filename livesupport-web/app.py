"""FastAPI backend for LiveSupport Web Chat.

Receives browser HTTP requests, invokes AgentCore Runtime via
agent_client.AgentClient, and streams the agent's response back to the
browser over Server-Sent Events (SSE).

This module currently implements startup configuration validation and
module-level AgentClient construction. The Rate_Limiter check (see
rate_limiter.py) is applied by the /api/chat endpoint, implemented in
a later task. Routes and SSE wiring are implemented in later tasks.
"""

import json
import logging
import os
import sys
from typing import AsyncIterator, Optional

import boto3
import dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent_client import (
    AgentClient,
    AgentClientError,
    AgentClientTimeoutError,
    InvalidImageError,
)
from rate_limiter import get_client_key, is_allowed

logger = logging.getLogger(__name__)

dotenv.load_dotenv()

# Minimum acceptable Session_Id length; requests with a missing or
# shorter session_id are rejected before AgentClient is invoked
# (Req 3.5).
MIN_SESSION_ID_LENGTH = 33

# Default cap on images per /api/chat request if MAX_IMAGES_PER_MESSAGE
# is unset.
DEFAULT_MAX_IMAGES_PER_MESSAGE = 3


def _get_support_image() -> bool:
    """Reads SUPPORT_IMAGE from the environment; disabled by default.

    Image paste/upload support stays off unless explicitly enabled, so
    deployments pointed at a non-vision model don't silently accept
    image input.
    """
    return os.environ.get("SUPPORT_IMAGE", "0") == "1"


def _get_max_images_per_message() -> int:
    """Reads MAX_IMAGES_PER_MESSAGE from the environment, with a
    default."""
    value = os.environ.get("MAX_IMAGES_PER_MESSAGE")
    if not value:
        return DEFAULT_MAX_IMAGES_PER_MESSAGE
    return int(value)


SUPPORT_IMAGE = _get_support_image()
MAX_IMAGES_PER_MESSAGE = _get_max_images_per_message()


class ChatImage(BaseModel):
    """A single image attached to a chat message.

    ``data`` is base64-encoded image bytes, optionally as a
    ``data:image/<format>;base64,...`` URL (in which case ``format``
    may be omitted and is inferred from the URL).
    """

    data: str
    format: Optional[str] = None


class ChatRequest(BaseModel):
    """Request body for POST /api/chat."""

    message: str
    session_id: str
    images: Optional[list[ChatImage]] = None


def _validate_session_id(session_id: str) -> None:
    """Validates that session_id is present and long enough.

    Raises an HTTPException with a 400 status code if session_id is
    missing/None or shorter than MIN_SESSION_ID_LENGTH, so the caller
    (the /api/chat endpoint) can reject the request before invoking
    AgentClient (Req 3.5).

    Args:
        session_id: The session_id value from the incoming request.

    Raises:
        HTTPException: 400 status code if session_id is missing or
            too short.
    """
    if not session_id or len(session_id) < MIN_SESSION_ID_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=(
                "session_id is required and must be at least "
                f"{MIN_SESSION_ID_LENGTH} characters"
            ),
        )


def _validate_images(images: Optional[list[ChatImage]]) -> None:
    """Validates incoming images against the SUPPORT_IMAGE feature flag
    and the configured per-message image cap.

    Raises an HTTPException with a 400 status code if images are
    supplied while SUPPORT_IMAGE is disabled, or if more than
    MAX_IMAGES_PER_MESSAGE images are supplied.

    Args:
        images: The images list from the incoming request, if any.

    Raises:
        HTTPException: 400 status code if images are not supported or
            the count exceeds the configured maximum.
    """
    if not images:
        return

    if not SUPPORT_IMAGE:
        raise HTTPException(
            status_code=400,
            detail="Image input is not supported by this deployment.",
        )

    if len(images) > MAX_IMAGES_PER_MESSAGE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"At most {MAX_IMAGES_PER_MESSAGE} image(s) are allowed "
                "per message."
            ),
        )


class ConfigurationError(Exception):
    """Raised when required startup configuration is missing or invalid."""


class DependencyVersionError(Exception):
    """Raised when an installed dependency does not meet minimum
    requirements."""


def validate_config() -> tuple[str, str]:
    """Reads and validates AGENT_RUNTIME_ARN and AWS_REGION from the
    environment.

    Note: despite the env var name (kept unchanged for backward
    compatibility with existing .env files), this value is actually
    the AgentCore Harness ARN (resource type "harness/...") passed to
    invoke_harness as harnessArn, not an AgentCore Runtime
    "runtime/..." ARN passed to invoke_agent_runtime.

    Raises and logs a ConfigurationError that aborts startup if either value
    is missing (Req 4.2, 4.3).

    Returns:
        A (harness_arn, region) tuple of the validated configuration
        values.
    """
    harness_arn = os.environ.get("AGENT_RUNTIME_ARN")
    region = os.environ.get("AWS_REGION")

    missing = []
    if not harness_arn:
        missing.append("AGENT_RUNTIME_ARN")
    if not region:
        missing.append("AWS_REGION")

    if missing:
        message = (
            "Missing required configuration environment variable(s): "
            f"{', '.join(missing)}"
        )
        logger.error(message)
        raise ConfigurationError(message)

    return harness_arn, region


def validate_bedrock_agentcore_client_available() -> None:
    """Verifies the installed boto3 exposes the 'bedrock-agentcore' client.

    Raises and logs a DependencyVersionError that aborts startup if the
    installed boto3 does not support the 'bedrock-agentcore' service
    (Req 5.5).
    """
    available_services = boto3.Session().get_available_services()
    if "bedrock-agentcore" not in available_services:
        message = (
            "Installed boto3 does not provide the 'bedrock-agentcore' "
            "client. Upgrade boto3 to a version that supports AgentCore "
            "Runtime."
        )
        logger.error(message)
        raise DependencyVersionError(message)


def _initialize_agent_client() -> AgentClient:
    """Validates startup configuration and constructs the AgentClient.

    Aborts the process (via sys.exit) if validation fails, after the
    underlying error has already been logged.
    """
    try:
        harness_arn, region = validate_config()
        validate_bedrock_agentcore_client_available()
    except (ConfigurationError, DependencyVersionError):
        sys.exit(1)

    return AgentClient(harness_arn, region)


agent_client = _initialize_agent_client()

app = FastAPI()

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "static"
)


@app.get("/")
def index() -> FileResponse:
    """Serves the chat page HTML for the root path (Req 7.1)."""
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


def _format_sse(data: object) -> str:
    """Formats a single SSE event with a JSON-encoded data payload.

    Args:
        data: A JSON-serializable value (chunk text or error payload).

    Returns:
        The formatted SSE event string, e.g. ``data: ...\\n\\n``.
    """
    return f"data: {json.dumps(data)}\n\n"


async def _sse_event_stream(
    message: str,
    session_id: str,
    images: Optional[list] = None,
) -> AsyncIterator[str]:
    """Streams the agent's response as SSE events, mapping errors safely.

    Iterates `agent_client.invoke_stream`, wrapping each yielded chunk
    as an SSE `data: ...\\n\\n` event (Req 2.3). If the call raises
    AgentClientTimeoutError, logs full details server-side and yields
    a single SSE error event with a generic timeout message distinct
    from generic errors, omitting the original exception
    message/traceback (Req 5.1, 5.2). If the call raises
    InvalidImageError (a malformed/undecodable image slipped past
    request-level validation), yields a single SSE error event citing
    the bad image input. If the call raises AgentClientError or any
    other unexpected Exception, logs full details server-side only and
    yields a single generic SSE error event (Req 5.1). In all cases,
    the generator returns once exhausted or after yielding an error
    event, closing the StreamingResponse stream (Req 2.5).

    Args:
        message: The user's chat message.
        session_id: The conversation's Session_Id.
        images: Optional list of image dicts to attach to the message.

    Yields:
        SSE-formatted event strings.
    """
    try:
        async for chunk in agent_client.invoke_stream(
            message, session_id, images
        ):
            yield _format_sse(chunk)
    except InvalidImageError:
        logger.exception("Invalid image payload in chat request")
        yield _format_sse(
            {
                "error": "invalid_image",
                "message": "One of the attached images could not be "
                "processed. Please try a different image.",
            }
        )
        return
    except AgentClientTimeoutError:
        logger.exception("AgentClient invocation timed out")
        yield _format_sse(
            {
                "error": "timeout",
                "message": "The request timed out. Please try again.",
            }
        )
        return
    except AgentClientError:
        logger.exception("AgentClient invocation failed")
        yield _format_sse(
            {
                "error": "internal",
                "message": "Something went wrong. Please try again.",
            }
        )
        return
    except Exception:
        logger.exception("AgentClient invocation failed unexpectedly")
        yield _format_sse(
            {
                "error": "internal",
                "message": "Something went wrong. Please try again.",
            }
        )
        return


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    """Handles POST /api/chat: validates the request, applies the
    Rate_Limiter, and invokes AgentClient to stream the agent's
    response.

    Validates session_id first (Req 3.5) so invalid requests never
    reach the Rate_Limiter or AgentClient. Then applies the
    Rate_Limiter check keyed by client identity before invoking
    AgentCore_Runtime (Req 6.2, 6.3). On passing both checks, streams
    the agent's response as SSE events via `_sse_event_stream`, which
    also maps AgentClient errors onto safe SSE error events (Req 1.3,
    2.3, 2.5, 5.1, 5.2).

    Args:
        req: The parsed chat request body.
        request: The incoming FastAPI request, used to derive the
            client's IP for rate limiting.

    Raises:
        HTTPException: 400 if session_id is missing/too short, or if
            images are supplied while unsupported/over the configured
            cap; 429 if the Rate_Limiter rejects the request.
    """
    _validate_session_id(req.session_id)
    _validate_images(req.images)

    client_ip = request.client.host if request.client else None
    client_key = get_client_key(client_ip)
    if not is_allowed(client_key):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please slow down and try again.",
        )

    images = (
        [image.model_dump() for image in req.images] if req.images else None
    )

    return StreamingResponse(
        _sse_event_stream(req.message, req.session_id, images),
        media_type="text/event-stream",
    )


@app.get("/api/config")
def get_config() -> dict:
    """Exposes feature-flag configuration needed by the frontend.

    Lets the chat UI know whether image paste/upload should be shown
    and how many images are allowed per message, without hardcoding
    those values in static/chat.js (Req: SUPPORT_IMAGE,
    MAX_IMAGES_PER_MESSAGE).
    """
    return {
        "support_image": SUPPORT_IMAGE,
        "max_images_per_message": MAX_IMAGES_PER_MESSAGE,
    }


# Mounted at "/" (not "/static") so that static/index.html's relative
# `<script src="chat.js">` reference resolves to "/chat.js". The explicit
# "/" route above takes precedence over this mount for the root path
# (Req 7.2).
app.mount("/", StaticFiles(directory=_STATIC_DIR), name="static")

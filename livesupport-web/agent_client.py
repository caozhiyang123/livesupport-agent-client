"""Agent_Client module for LiveSupport Web Chat.

Wraps the boto3 ``bedrock-agentcore`` client: builds the invocation
request, calls ``invoke_harness``, and extracts decoded text chunks
from the structured ``EventStream`` response.

This module implements the pure, dependency-free call-kwargs
construction, the ``AgentClient`` class wiring the boto3 client and
thread pool executor, and the ``AgentClient.invoke_stream`` async
generator that performs the end-to-end streaming invocation against
an AgentCore Harness (not an AgentCore Runtime agent directly).
"""

import asyncio
import concurrent.futures
from typing import AsyncIterator, Iterator

import boto3
import botocore.config
import botocore.exceptions

# Qualifier (endpoint name) passed to invoke_harness. If omitted,
# boto3/AgentCore defaults to the "DEFAULT" endpoint; kept explicit
# here for clarity and parity with the rest of the codebase (Req 1.4).
QUALIFIER = "DEFAULT"

# boto3 client read timeout in seconds; must exceed the default 60s
# timeout to accommodate streamed agent responses (Req 4.4).
READ_TIMEOUT_SECONDS = 300


def _build_call_kwargs(arn: str, message: str, session_id: str) -> dict:
    """Pure function: (arn, message, session_id) -> boto3 call kwargs.

    Builds the keyword arguments for the boto3 ``invoke_harness`` call
    without touching the network or any boto3 client instance, so it
    can be property-tested directly (Req 1.3, 1.4, 3.4, 4.4).

    Unlike ``invoke_agent_runtime`` (which accepts a free-form JSON
    ``payload``), ``invoke_harness`` requires a structured
    ``messages`` list in the Converse-style shape: a list of
    ``{"role": ..., "content": [...]}`` dicts, where each content
    block is a tagged union (here, a single ``{"text": ...}`` block
    carrying the user's message verbatim).

    Args:
        arn: The configured AgentCore Harness ARN
            (``harnessArn``, resource type ``harness/...``, not an
            AgentCore Runtime ``runtime/...`` ARN).
        message: The user's message text.
        session_id: The Session_Id to forward as ``runtimeSessionId``.

    Returns:
        A dict with keys ``harnessArn``, ``qualifier``,
        ``runtimeSessionId``, and ``messages``.
    """
    return {
        "harnessArn": arn,
        "qualifier": QUALIFIER,
        "runtimeSessionId": session_id,
        "messages": [
            {
                "role": "user",
                "content": [{"text": message}],
            }
        ],
    }


def extract_text_deltas(events: Iterator[dict]) -> Iterator[str]:
    """Pure generator: extracts text deltas from invoke_harness events.

    ``invoke_harness`` returns a boto3 ``EventStream`` that yields
    already-parsed event dicts (no manual SSE/JSON parsing is needed,
    unlike ``invoke_agent_runtime``'s raw ``text/event-stream`` body).
    Each event is a tagged union; this generator looks for
    ``contentBlockDelta`` events carrying a text delta
    (``event["contentBlockDelta"]["delta"]["text"]``) and yields that
    text in order, ignoring all other event types (``messageStart``,
    ``contentBlockStart``, ``contentBlockStop``, ``messageStop``,
    ``metadata``, tool-use/tool-result deltas, reasoning content,
    etc.) (Req 2.1, 2.2).

    This is a pure, synchronous, dependency-free generator over plain
    dicts (no network or boto3 access) so it can be property-tested
    directly against arbitrary event sequences.

    Args:
        events: An iterable of already-decoded invoke_harness stream
            event dicts.

    Yields:
        The text delta string of each ``contentBlockDelta`` event that
        carries a text delta, in the order the events were
        encountered.
    """
    for event in events:
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        text = delta.get("text")
        if text is not None:
            yield text


# Number of worker threads used to offload blocking boto3 calls so the
# asyncio event loop is not blocked while waiting on a response
# (Req 1.5).
_EXECUTOR_MAX_WORKERS = 4


class AgentClientError(Exception):
    """Raised for client/service errors from AgentCore Harness."""


class AgentClientTimeoutError(AgentClientError):
    """Raised specifically when the invoke_harness call times out."""


class AgentClient:
    """Wraps the boto3 ``bedrock-agentcore`` client for a single
    AgentCore Harness target.

    Owns a boto3 client configured with an elevated read timeout
    (Req 4.4) and a thread pool executor used to offload the blocking
    ``invoke_harness`` call (Req 1.5).
    """

    def __init__(self, harness_arn: str, region: str):
        """Construct the boto3 client and thread pool executor.

        Args:
            harness_arn: The configured AgentCore Harness ARN to
                invoke (resource type ``harness/...``).
            region: The AWS region to construct the boto3 client in,
                matching ``AWS_REGION`` (Req 4.5).
        """
        config = botocore.config.Config(read_timeout=READ_TIMEOUT_SECONDS)
        self._client = boto3.client(
            "bedrock-agentcore", region_name=region, config=config
        )
        self._harness_arn = harness_arn
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=_EXECUTOR_MAX_WORKERS
        )

    async def invoke_stream(
        self, message: str, session_id: str
    ) -> AsyncIterator[str]:
        """Invoke AgentCore Harness and yield decoded text chunks.

        Builds the request via ``_build_call_kwargs``, invokes the
        blocking boto3 ``invoke_harness`` call inside the thread pool
        executor so the asyncio event loop is not blocked (Req 1.5),
        then iterates the returned ``EventStream`` and yields text
        deltas extracted via ``extract_text_deltas`` as they become
        available (Req 2.1).

        Args:
            message: The user's message text.
            session_id: The Session_Id to forward as
                ``runtimeSessionId``.

        Yields:
            Decoded text chunks from the AgentCore Harness response
            stream, in order.

        Raises:
            AgentClientTimeoutError: If the underlying boto3 call
                times out (Req 5.2).
            AgentClientError: If the underlying boto3 call raises any
                other client/service error (Req 5.1).
        """
        kwargs = _build_call_kwargs(self._harness_arn, message, session_id)
        loop = asyncio.get_event_loop()

        try:
            response = await loop.run_in_executor(
                self._executor,
                lambda: self._client.invoke_harness(**kwargs),
            )
        except (
            botocore.exceptions.ReadTimeoutError,
            botocore.exceptions.ConnectTimeoutError,
        ) as e:
            raise AgentClientTimeoutError(
                "Timed out waiting for AgentCore Harness response"
            ) from e
        except self._client.exceptions.ThrottlingException as e:
            raise AgentClientError(
                "AgentCore Harness request was throttled"
            ) from e
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ) as e:
            raise AgentClientError(
                "Failed to invoke AgentCore Harness"
            ) from e

        event_stream = response["stream"]

        try:
            for event in event_stream:
                if "internalServerException" in event:
                    raise AgentClientError(
                        "AgentCore Harness returned an internal server "
                        "error"
                    )
                if "validationException" in event:
                    raise AgentClientError(
                        "AgentCore Harness rejected the request as "
                        "invalid"
                    )
                if "runtimeClientError" in event:
                    raise AgentClientError(
                        "AgentCore Harness runtime reported an error"
                    )
                for chunk in extract_text_deltas(iter([event])):
                    yield chunk
        except (
            botocore.exceptions.ReadTimeoutError,
            botocore.exceptions.ConnectTimeoutError,
        ) as e:
            raise AgentClientTimeoutError(
                "Timed out while reading AgentCore Harness response "
                "stream"
            ) from e
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ) as e:
            raise AgentClientError(
                "Failed while reading AgentCore Harness response stream"
            ) from e

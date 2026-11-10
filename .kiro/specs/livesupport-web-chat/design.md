# Design Document

## Overview

LiveSupport Web Chat is a thin-waisted, stateless system that streams chat responses from an AgentCore Runtime agent to a browser-based chat UI through a FastAPI backend.

## Architecture

LiveSupport Web Chat has three tiers:

```
Browser (static/index.html + static/chat.js)
   │  HTTP POST /api/chat  (JSON: { message, session_id })
   │  <── SSE text/event-stream
   ▼
FastAPI Backend (app.py)
   │  calls agent_client.invoke(...)   [run in thread pool executor]
   ▼
Agent_Client (agent_client.py)
   │  boto3 bedrock-agentcore client.invoke_agent_runtime(...)
   ▼
AgentCore Runtime ("livesupport-agent")
```

The Backend holds no per-user state across requests. Conversation continuity is achieved entirely through the `Session_Id` (a frontend-generated UUID) forwarded as `runtimeSessionId`, letting AgentCore Runtime route to the same microVM and letting AgentCore Memory retain context. The Backend authenticates to AWS using the default boto3 credential chain, which resolves to the EC2 Instance_Role when deployed.

## Project Structure

```
livesupport-web/
├── app.py              # FastAPI app: routes, SSE wiring, rate limiting, error mapping
├── agent_client.py      # boto3 bedrock-agentcore client wrapper + SSE line parser
├── static/
│   ├── index.html        # Chat page markup
│   └── chat.js           # Session id management, fetch + SSE consumption, DOM rendering
├── requirements.txt     # fastapi, uvicorn, boto3 (pinned minimum version), slowapi or custom limiter
└── .env                 # AGENT_RUNTIME_ARN, AWS_REGION, RATE_LIMIT_* (not committed; loaded via python-dotenv or shell env)
```

## Components and Interfaces

### 1. `agent_client.py` — Agent_Client

**Responsibilities:** own the boto3 client, construct the Payload, invoke `invoke_agent_runtime`, parse the SSE-like response body, and yield decoded text chunks.

```python
# agent_client.py

PAYLOAD_FIELD = "prompt"          # centrally defined field name (Req 1.3)
QUALIFIER = "DEFAULT"
READ_TIMEOUT_SECONDS = 300        # > default 60s (Req 4.4)

class AgentClientError(Exception):
    """Raised for client/service errors from AgentCore Runtime."""

class AgentClientTimeoutError(AgentClientError):
    """Raised specifically when the invoke_agent_runtime call times out."""

class AgentClient:
    def __init__(self, runtime_arn: str, region: str):
        config = botocore.config.Config(read_timeout=READ_TIMEOUT_SECONDS)
        self._client = boto3.client("bedrock-agentcore", region_name=region, config=config)
        self._runtime_arn = runtime_arn
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=N)

    async def invoke_stream(self, message: str, session_id: str) -> AsyncIterator[str]:
        """
        Builds the Payload, invokes invoke_agent_runtime in the thread pool
        (Req 1.5), and yields decoded text chunks as they become available.
        Raises AgentClientTimeoutError / AgentClientError on failure (Req 5.1, 5.2).
        """

    def _build_call_kwargs(self, message: str, session_id: str) -> dict:
        """Pure function: (message, session_id) -> boto3 call kwargs.
        {
          "agentRuntimeArn": self._runtime_arn,
          "runtimeSessionId": session_id,
          "qualifier": QUALIFIER,
          "payload": json.dumps({PAYLOAD_FIELD: message}).encode("utf-8"),
        }
        """

    @staticmethod
    def parse_event_stream(raw_lines: Iterable[bytes]) -> Iterator[str]:
        """Pure generator: filters `data:` prefixed lines, JSON-decodes the
        content, and yields decoded string chunks in order (Req 2.1, 2.2)."""
```

`_build_call_kwargs` and `parse_event_stream` are kept as pure, synchronous, dependency-free functions specifically so they can be property-tested without mocking the network.

### 2. `app.py` — Backend

**Responsibilities:** serve static assets, expose `POST /api/chat`, validate `session_id`, apply the Rate_Limiter, call `AgentClient.invoke_stream`, map results/errors onto SSE events, and fail fast on missing configuration at startup.

```python
# app.py

MIN_SESSION_ID_LENGTH = 33   # Req 3.5

@app.on_event("startup")
def validate_config():
    """Reads AGENT_RUNTIME_ARN and AWS_REGION; raises/logs and aborts
    startup if either is missing (Req 4.2, 4.3), and verifies the
    installed boto3 exposes the 'bedrock-agentcore' client (Req 5.5)."""

@app.get("/")
def index() -> FileResponse: ...          # Req 7.1

# static/* served via StaticFiles mount                           # Req 7.2

@app.post("/api/chat")
@rate_limiter.limit(...)                                          # Req 6.1-6.3
async def chat(req: ChatRequest) -> StreamingResponse:
    """
    1. Validate session_id length >= MIN_SESSION_ID_LENGTH, else 400 (Req 3.5)
       without calling AgentClient.
    2. Call agent_client.invoke_stream(req.message, req.session_id).
    3. On success, wrap each yielded chunk as an SSE `data: ...\n\n` event (Req 2.3).
    4. On AgentClientTimeoutError -> emit a single SSE error event with a
       generic timeout message (Req 5.2), no stack trace (Req 5.1).
    5. On AgentClientError/unexpected Exception -> emit a generic SSE error
       event, log full details server-side only (Req 5.1).
    6. Close the stream when the generator is exhausted (Req 2.5).
    """

class ChatRequest(BaseModel):
    message: str
    session_id: str
```

**Rate_Limiter:** a small sliding/fixed-window counter keyed by client IP (or a client-supplied header if present), configured via `RATE_LIMIT_MAX_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` env vars, checked before any call into `AgentClient` (Req 6.3). Implemented either with `slowapi` or a minimal in-memory `{client_key: deque[timestamp]}` counter — sufficient for a single-instance MVP.

### 3. `static/chat.js` — Frontend

**Responsibilities:** manage `Session_Id` lifecycle in memory (and optionally `sessionStorage`), validate input before sending, POST to `/api/chat`, consume the SSE stream via `fetch` + `ReadableStream` (or `EventSource` with a GET fallback — design assumes `fetch` streaming since the request is a POST), append chunks to the DOM, and render errors/incomplete-stream indicators.

```javascript
// chat.js

const MIN_SESSION_ID_LENGTH = 33;

function createSessionId() { return crypto.randomUUID(); }   // Req 3.1 (36 chars, >= 33)

function getOrCreateSessionId() { ... }                       // Req 3.1
function startNewConversation() { sessionId = createSessionId(); }   // Req 3.3

function isBlank(message) { return message.trim().length === 0; }    // Req 1.2

async function sendMessage(message) {
  if (isBlank(message)) return;                                       // Req 1.2
  const body = JSON.stringify({ message, session_id: sessionId });     // Req 1.1, 3.2
  const response = await fetch("/api/chat", { method: "POST", body, ... });
  if (!response.ok) { renderError(await response.json()); return; }    // Req 5.3
  await consumeSseStream(response.body, appendToCurrentAgentMessage);  // Req 2.4
}

function appendToCurrentAgentMessage(chunk) { currentAgentText += chunk; render(); }

function renderError(errorPayload) { /* shows a visible .error-banner */ }  // Req 5.3
function renderIncomplete() { /* shows "response incomplete" indicator */ } // Req 5.4
```

`appendToCurrentAgentMessage` is kept as a pure reducer `(accumulatedText, chunk) -> newText` so it can be property-tested independent of the DOM.

## Data Models

**ChatRequest** (Frontend → Backend, JSON body of `POST /api/chat`):
| Field | Type | Constraint |
|---|---|---|
| `message` | string | non-empty after trim (enforced client-side; server treats blank as a normal message, since Req 1.2 is a Frontend obligation) |
| `session_id` | string | length ≥ 33 (Req 3.5) |

**Payload** (Backend/Agent_Client → AgentCore_Runtime, JSON-encoded bytes):
| Field | Type |
|---|---|
| `prompt` (= `PAYLOAD_FIELD`) | string |

**SSE event** (Backend → Frontend):
```
data: <chunk text or JSON-encoded error object>\n\n
```
Error events use a reserved shape so the Frontend can distinguish them from content chunks, e.g. `data: {"error": "..."}\n\n`, or a separate SSE `event: error` field.

## Error Handling

| Condition | Backend behavior | Frontend behavior |
|---|---|---|
| `session_id` missing/too short | 400 JSON error, AgentClient never invoked (Req 3.5) | shown as request-level error before stream starts |
| boto3 client/service error | SSE error event with generic message, full error logged server-side (Req 5.1) | renders visible error banner (Req 5.3) |
| boto3 read timeout | SSE error event indicating timeout specifically (Req 5.2) | renders visible "timed out" message |
| Missing `AGENT_RUNTIME_ARN`/`AWS_REGION` at startup | process fails to start, config error logged (Req 4.3) | n/a |
| `bedrock-agentcore` client unavailable in installed boto3 | process fails to start, dependency error logged (Req 5.5) | n/a |
| Rate limit exceeded | 429 JSON error before invoking AgentClient (Req 6.1-6.3) | renders visible rate-limit error |
| Connection drops mid-stream | n/a (client disconnect) | detects stream termination without a completion marker, shows "incomplete response" (Req 5.4) |

## Deployment Notes (Req 8.1–8.3)

These are infrastructure configuration concerns, not application logic, and are not unit/property testable; they are captured here for operational completeness and verified by deployment/config review:

- nginx: `proxy_buffering off;` and an elevated `proxy_read_timeout` scoped to the `/api/chat` location block.
- EC2 security group: inbound restricted to TCP 80/443 only.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system—essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Valid message submission produces a correctly shaped request

For any non-empty, non-whitespace message string and any valid Session_Id, invoking the Frontend's send logic SHALL produce exactly one outgoing request whose body contains that exact message text and that exact Session_Id.

**Validates: Requirements 1.1**

### Property 2: Whitespace-only messages never produce a request

For any string composed entirely of whitespace characters (including the empty string), invoking the Frontend's send logic SHALL NOT produce any outgoing request to the Chat_Endpoint, and application state SHALL remain unchanged.

**Validates: Requirements 1.2**

### Property 3: End-to-end call construction is faithful to inputs

For any message string and any Session_Id of at least 33 characters, the kwargs passed to the underlying boto3 `invoke_agent_runtime` call SHALL have `agentRuntimeArn` equal to the configured AgentCore_Runtime ARN, `runtimeSessionId` equal exactly to the given Session_Id (no truncation or modification), `qualifier` equal to `"DEFAULT"`, and `payload` SHALL be JSON-decodable bytes whose value under the configured field name equals the given message string exactly.

**Validates: Requirements 1.3, 1.4, 3.4**

### Property 4: SSE parsing round-trip

For any list of text chunks, encoding each chunk as a `data:` line containing its JSON-encoded form and feeding the resulting lines through Agent_Client's event-stream parser SHALL yield back a list of chunks equal to the original list, in the same order, with any non-`data:` lines interspersed in the input ignored.

**Validates: Requirements 2.1, 2.2**

### Property 5: Streaming delivery round-trip preserves full response text and order

For any list of text chunks produced by Agent_Client, formatting them as Backend SSE events and then feeding those events through the Frontend's SSE consumption and append logic SHALL produce a final rendered agent message equal to the concatenation of all chunks in their original order.

**Validates: Requirements 2.3, 2.4**

### Property 6: Generated Session_Id always satisfies the format invariant

For any invocation of the Frontend's Session_Id generator, the returned value SHALL be a string of at least 33 characters conforming to UUID format.

**Validates: Requirements 3.1**

### Property 7: Session_Id remains stable across a conversation

For any valid Session_Id and any sequence of N non-empty messages sent without activating New_Conversation_Control, every one of the N outgoing requests SHALL carry that same, unchanged Session_Id.

**Validates: Requirements 3.2**

### Property 8: New-conversation control rotates the Session_Id

For any existing valid Session_Id, activating New_Conversation_Control SHALL cause the Frontend to generate a new Session_Id different from the previous one, and every subsequent outgoing request SHALL carry only the new Session_Id.

**Validates: Requirements 3.3**

### Property 9: Short or missing Session_Id is rejected without invoking AgentCore_Runtime

For any request body whose `session_id` is absent or has fewer than 33 characters, the Chat_Endpoint SHALL respond with an error response and the underlying `invoke_agent_runtime` call SHALL never be made.

**Validates: Requirements 3.5**

### Property 10: Error responses never leak internal details and distinguish timeouts

For any exception raised by Agent_Client (including a timeout-specific exception and arbitrary client/service exceptions with randomized internal messages/tracebacks), the Backend's error-mapping SHALL produce a response whose body never contains the original exception's message or traceback text, and SHALL mark timeout exceptions specifically as a timeout-type error distinct from generic errors.

**Validates: Requirements 5.1, 5.2**

### Property 11: Every error response renders a visible Frontend error

For any error response (status code and JSON error body) returned by the Chat_Endpoint, the Frontend's error-handling logic SHALL result in a non-empty, user-visible error message being present in the rendered chat state.

**Validates: Requirements 5.3**

### Property 12: Rate limiting admits exactly the configured threshold and blocks invocation for the rest

For any client identity and any sequence of N requests arriving within a single configured time window, where the configured limit is L: if N ≤ L, all N requests SHALL be permitted to reach Agent_Client; if N > L, exactly the first L requests (in arrival order) SHALL be permitted to reach Agent_Client, and each of the remaining N − L requests SHALL be rejected with a rate-limit error response without Agent_Client ever being invoked for them.

**Validates: Requirements 6.1, 6.2, 6.3**

## Testing Strategy

**Unit/example tests** (covering non-property-testable or configuration-level acceptance criteria):
- Req 1.5: a slow mocked boto3 call does not block a concurrently scheduled async task (executor offload).
- Req 2.5: given a mocked finite chunk stream, the SSE response is closed after the last chunk.
- Req 4.2/4.3/4.4/4.5: startup reads `AGENT_RUNTIME_ARN`/`AWS_REGION` correctly; missing either aborts startup with a logged error; boto3 `Config.read_timeout` > 60; client region matches `AWS_REGION`.
- Req 5.4: simulated mid-stream disconnect results in an "incomplete response" indicator.
- Req 5.5: missing `bedrock-agentcore` client capability aborts startup with a logged dependency error.
- Req 7.1/7.2: `GET /` returns the chat HTML; static JS asset routes return 200.
- Req 8.1–8.3: deployment/config review checklist (not automated unit tests).

**Property-based tests** (Properties 1–12 above): minimum 100 iterations each, using randomized generators for messages (including unicode/whitespace edge cases), Session_Id strings (valid-length and sub-33-length), chunk sequences of varying length/content, and exception types/messages. Pure functions (`_build_call_kwargs`, `parse_event_stream`, the Frontend reducer, the Session_Id generator) are tested directly; network/boto3 boundaries are mocked.

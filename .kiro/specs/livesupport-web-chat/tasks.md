# Implementation Plan: LiveSupport Web Chat

## Overview

Convert the feature design into a series of prompts for a code-generation LLM that will implement each step with incremental progress. Make sure that each prompt builds on the previous prompts, and ends with wiring things together. There should be no hanging or orphaned code that isn't integrated into a previous step. Focus ONLY on tasks that involve writing, modifying, or testing code.

Implementation language: Python (FastAPI backend) + vanilla JavaScript (frontend), as specified in the design document. The system has three tiers: a static browser frontend (`static/index.html`, `static/chat.js`), a FastAPI backend (`app.py`), and an `agent_client.py` module wrapping the boto3 `bedrock-agentcore` client. Tasks below build the project skeleton first, then the pure/testable core of `agent_client.py`, then `app.py` wiring (config validation, endpoints, SSE, rate limiting, error mapping), then the frontend, then deployment configuration templates.

## Tasks

- [x] 1. Set up project skeleton and dependencies
  - Create `livesupport-web/` directory structure: `livesupport-web/app.py` (empty/stub), `livesupport-web/agent_client.py` (empty/stub), `livesupport-web/static/` directory
  - Create `livesupport-web/requirements.txt` pinning `fastapi`, `uvicorn`, a boto3 version that supports the `bedrock-agentcore` client, `python-dotenv`, and `pydantic`
  - Create `livesupport-web/.env.example` template documenting `AGENT_RUNTIME_ARN`, `AWS_REGION`, `RATE_LIMIT_MAX_REQUESTS`, `RATE_LIMIT_WINDOW_SECONDS`
  - _Requirements: 4.2_

- [x] 2. Implement Agent_Client pure/testable core
  - [x] 2.1 Implement `_build_call_kwargs` and payload construction constants in `agent_client.py`
    - Define `PAYLOAD_FIELD = "prompt"`, `QUALIFIER = "DEFAULT"`, `READ_TIMEOUT_SECONDS = 300`
    - Implement `_build_call_kwargs(message, session_id)` as a pure function returning `agentRuntimeArn`, `runtimeSessionId`, `qualifier`, and JSON-encoded `payload` bytes
    - _Requirements: 1.3, 1.4, 3.4, 4.4_

  - [ ]* 2.2 Write property test for end-to-end call construction faithfulness
    - **Property 3: End-to-end call construction is faithful to inputs**
    - **Validates: Requirements 1.3, 1.4, 3.4**

  - [x] 2.3 Implement `parse_event_stream` SSE line parser in `agent_client.py`
    - Pure generator: filters `data:` prefixed lines, JSON-decodes content, yields decoded string chunks in order, ignoring non-`data:` lines
    - _Requirements: 2.1, 2.2_

  - [ ]* 2.4 Write property test for SSE parsing round-trip
    - **Property 4: SSE parsing round-trip**
    - **Validates: Requirements 2.1, 2.2**

- [x] 3. Implement AgentClient class with boto3 wiring and thread pool execution
  - [x] 3.1 Implement `AgentClientError`, `AgentClientTimeoutError`, and `AgentClient.__init__` in `agent_client.py`
    - Construct `botocore.config.Config(read_timeout=READ_TIMEOUT_SECONDS)` and the `bedrock-agentcore` boto3 client with the given region
    - Store `runtime_arn` and create a `ThreadPoolExecutor`
    - _Requirements: 4.1, 4.4, 4.5_

  - [x] 3.2 Implement `AgentClient.invoke_stream` async generator
    - Build kwargs via `_build_call_kwargs`, run the blocking `invoke_agent_runtime` boto3 call inside the thread pool executor so the event loop is not blocked
    - Parse the returned stream body with `parse_event_stream` and yield decoded chunks
    - Catch boto3 client/service exceptions and timeout-specific exceptions, re-raising as `AgentClientError`/`AgentClientTimeoutError`
    - _Requirements: 1.5, 2.1, 5.1, 5.2_

  - [ ]* 3.3 Write unit test verifying thread pool offload does not block the event loop
    - Mock a slow boto3 call and assert a concurrently scheduled async task completes without waiting for it
    - _Requirements: 1.5_

  - [ ]* 3.4 Write unit test for error/timeout exception mapping in `invoke_stream`
    - Mock boto3 raising a generic `ClientError` and a timeout-style exception; assert each maps to `AgentClientError` / `AgentClientTimeoutError` respectively
    - _Requirements: 5.1, 5.2_

- [x] 4. Checkpoint - Ensure all Agent_Client tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Implement Backend startup configuration validation
  - [x] 5.1 Implement startup config validation in `app.py`
    - Read `AGENT_RUNTIME_ARN` and `AWS_REGION` from environment; raise and log a configuration error that aborts startup if either is missing
    - Verify the installed boto3 exposes the `bedrock-agentcore` client; raise and log a dependency version error that aborts startup if not
    - Construct the module-level `AgentClient` instance using the validated ARN/region so the boto3 client region matches `AWS_REGION`
    - _Requirements: 4.2, 4.3, 4.5, 5.5_

  - [ ]* 5.2 Write unit tests for startup configuration validation
    - Missing `AGENT_RUNTIME_ARN` or `AWS_REGION` aborts startup with a logged error
    - Missing `bedrock-agentcore` client capability aborts startup with a logged dependency error
    - boto3 `Config.read_timeout` is greater than 60 and client region matches `AWS_REGION`
    - _Requirements: 4.2, 4.3, 4.4, 4.5, 5.5_

- [x] 6. Implement static page delivery endpoints
  - [x] 6.1 Implement `GET /` and static asset mounting in `app.py`
    - Return the chat page HTML (`static/index.html`) for `GET /`
    - Mount `static/` so `chat.js` and other supporting assets are servable
    - _Requirements: 7.1, 7.2_

  - [ ]* 6.2 Write unit tests for web page delivery
    - `GET /` returns 200 with HTML content
    - Static JS asset route returns 200
    - _Requirements: 7.1, 7.2_

- [x] 7. Implement Rate_Limiter
  - [x] 7.1 Implement an in-memory sliding/fixed-window rate limiter in `app.py` (or a small `rate_limiter.py` module)
    - Key by client IP (or client-supplied header if present), configured via `RATE_LIMIT_MAX_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` env vars
    - Expose a check function returning allow/deny, callable before any `AgentClient` invocation
    - _Requirements: 6.1, 6.3_

  - [ ]* 7.2 Write property test for rate limiting threshold behavior
    - **Property 12: Rate limiting admits exactly the configured threshold and blocks invocation for the rest**
    - **Validates: Requirements 6.1, 6.2, 6.3**

- [x] 8. Implement `POST /api/chat` endpoint with session validation, SSE forwarding, and error mapping
  - [x] 8.1 Implement `ChatRequest` model and `session_id` validation in `app.py`
    - `ChatRequest` with `message: str`, `session_id: str`
    - Reject requests whose `session_id` is missing or shorter than `MIN_SESSION_ID_LENGTH = 33` with a 400 error response, without invoking `AgentClient`
    - _Requirements: 3.5_

  - [ ]* 8.2 Write property test for short/missing Session_Id rejection
    - **Property 9: Short or missing Session_Id is rejected without invoking AgentCore_Runtime**
    - **Validates: Requirements 3.5**

  - [x] 8.3 Implement `POST /api/chat` request handling: payload construction trigger, rate limit check, and AgentClient invocation
    - Apply the Rate_Limiter check before calling `AgentClient.invoke_stream`; on rejection return a 429 JSON error indicating the rate limit was exceeded
    - On passing validation and rate limit, call `agent_client.invoke_stream(req.message, req.session_id)`
    - _Requirements: 1.3, 6.2, 6.3_

  - [x] 8.4 Implement SSE response streaming and error-to-SSE mapping in `app.py`
    - Wrap each yielded chunk as an SSE `data: ...\n\n` event via `StreamingResponse`
    - On `AgentClientTimeoutError`, emit a single SSE error event with a generic timeout message distinct from generic errors, without the original exception message/traceback
    - On `AgentClientError`/unexpected `Exception`, emit a generic SSE error event and log full details server-side only
    - Close the stream once the generator is exhausted
    - _Requirements: 2.3, 2.5, 5.1, 5.2_

  - [ ]* 8.5 Write property test for error response safety and timeout distinction
    - **Property 10: Error responses never leak internal details and distinguish timeouts**
    - **Validates: Requirements 5.1, 5.2**

  - [ ]* 8.6 Write unit test for SSE stream closure after last chunk
    - Given a mocked finite chunk stream, assert the SSE response is closed after the last chunk
    - _Requirements: 2.5_

- [x] 9. Checkpoint - Ensure all Backend tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Implement Frontend session id management (`static/chat.js`)
  - [x] 10.1 Implement `createSessionId`, `getOrCreateSessionId`, and `startNewConversation` in `static/chat.js`
    - `createSessionId` returns a UUID string of at least 33 characters (`crypto.randomUUID()`)
    - `getOrCreateSessionId` returns the active Session_Id, generating one if none exists
    - `startNewConversation` discards the current Session_Id and generates a new one for all subsequent requests
    - _Requirements: 3.1, 3.3_

  - [ ]* 10.2 Write property test for Session_Id format invariant
    - **Property 6: Generated Session_Id always satisfies the format invariant**
    - **Validates: Requirements 3.1**

  - [ ]* 10.3 Write property test for Session_Id stability across a conversation
    - **Property 7: Session_Id remains stable across a conversation**
    - **Validates: Requirements 3.2**

  - [ ]* 10.4 Write property test for New_Conversation_Control Session_Id rotation
    - **Property 8: New-conversation control rotates the Session_Id**
    - **Validates: Requirements 3.3**

- [x] 11. Implement Frontend message submission logic
  - [x] 11.1 Implement `isBlank` and `sendMessage` request construction in `static/chat.js`
    - `isBlank(message)` returns true for whitespace-only or empty strings
    - `sendMessage` short-circuits without sending a request when `isBlank(message)` is true
    - When not blank, builds the JSON body with the exact message text and current Session_Id and issues the `POST /api/chat` fetch request
    - _Requirements: 1.1, 1.2, 3.2_

  - [ ]* 11.2 Write property test for valid message submission request shape
    - **Property 1: Valid message submission produces a correctly shaped request**
    - **Validates: Requirements 1.1**

  - [ ]* 11.3 Write property test for whitespace-only messages never producing a request
    - **Property 2: Whitespace-only messages never produce a request**
    - **Validates: Requirements 1.2**

- [x] 12. Implement Frontend SSE consumption and rendering
  - [x] 12.1 Implement `appendToCurrentAgentMessage` as a pure reducer in `static/chat.js`
    - `(accumulatedText, chunk) -> newText` concatenation reducer, independent of the DOM
    - _Requirements: 2.4_

  - [x] 12.2 Implement `consumeSseStream` wiring in `static/chat.js`
    - Consume the `fetch` response body `ReadableStream`, decode `data:` chunks, and call `appendToCurrentAgentMessage` for each chunk in order to update the rendered DOM
    - _Requirements: 2.4_

  - [ ]* 12.3 Write property test for streaming delivery round-trip
    - **Property 5: Streaming delivery round-trip preserves full response text and order**
    - **Validates: Requirements 2.3, 2.4**

  - [x] 12.4 Implement `renderError` and `renderIncomplete` in `static/chat.js`
    - `renderError` displays a non-empty, user-visible error banner when the Chat_Endpoint returns an error response (request-level or SSE error event)
    - `renderIncomplete` displays an "incomplete response" indicator when the SSE connection is interrupted before the stream completes
    - _Requirements: 5.3, 5.4_

  - [ ]* 12.5 Write property test for visible Frontend error rendering
    - **Property 11: Every error response renders a visible Frontend error**
    - **Validates: Requirements 5.3**

  - [ ]* 12.6 Write unit test for mid-stream disconnect incomplete-response indicator
    - Simulate a stream that ends without a completion marker and assert the "incomplete response" indicator is shown
    - _Requirements: 5.4_

- [x] 13. Implement `static/index.html` chat page markup
  - [x] 13.1 Create `static/index.html` with chat message list, input box, send control, and New_Conversation_Control, wired to `chat.js`
    - _Requirements: 7.1, 7.2_

- [x] 14. Checkpoint - Ensure all Frontend tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 15. Create deployment configuration templates
  - [x] 15.1 Create `deploy/livesupport-web.service` systemd unit file template
    - Document `ExecStart` running the FastAPI app via `uvicorn`, working directory, environment file reference (`.env`), restart policy, and the (non-root) user it runs as
    - _Requirements: 8.3_

  - [x] 15.2 Create `deploy/nginx-livesupport.conf` nginx location block snippet template
    - Include a `location /api/chat` block with `proxy_buffering off;` and an elevated `proxy_read_timeout`, plus a general location block proxying to the uvicorn upstream
    - _Requirements: 8.1, 8.2_

  - [x] 15.3 Add a deployment notes section to `README.md` documenting the EC2 security group restriction to ports 80/443
    - _Requirements: 8.3_

- [x] 16. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP, but all 12 correctness properties and the listed unit tests are included as optional sub-tasks per the design's Testing Strategy.
- Each task references specific requirements for traceability.
- Checkpoints ensure incremental validation after each major component (Agent_Client, Backend, Frontend, and final integration).
- Property tests (Properties 1–12) validate universal correctness properties defined in the design document; each is implemented as its own sub-task placed immediately after the implementation it covers.
- Unit tests validate specific examples, configuration-level behavior, and edge cases not covered by properties (thread pool offload, startup validation, stream closure, static asset delivery, mid-stream disconnect).
- Deployment configuration tasks (15.1–15.3) produce template/documentation files only; they are not automated tests per Requirement 8's infrastructure-level nature.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "2.3", "5.1", "7.1", "10.1", "13.1", "15.1", "15.2", "15.3"] },
    { "id": 2, "tasks": ["2.2", "2.4", "3.1", "5.2", "7.2", "10.2", "10.3", "10.4"] },
    { "id": 3, "tasks": ["3.2", "6.1", "8.1", "11.1"] },
    { "id": 4, "tasks": ["3.3", "3.4", "6.2", "8.2", "11.2", "11.3", "12.1"] },
    { "id": 5, "tasks": ["8.3", "12.2"] },
    { "id": 6, "tasks": ["8.4", "12.4"] },
    { "id": 7, "tasks": ["8.5", "8.6", "12.3", "12.5", "12.6"] }
  ]
}
```

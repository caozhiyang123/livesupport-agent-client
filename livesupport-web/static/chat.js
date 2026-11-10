// chat.js
// Frontend logic for LiveSupport Web Chat.
//
// This file is built incrementally across multiple tasks. This section
// implements Session_Id lifecycle management (Requirements 3.1, 3.3).

const MIN_SESSION_ID_LENGTH = 33;

// Module-level state holding the active Session_Id for the current
// conversation. `null` means no Session_Id has been generated yet.
let sessionId = null;

// Feature-flag state fetched from GET /api/config. Image paste/upload stays
// disabled until confirmed enabled server-side (SUPPORT_IMAGE), so the UI
// never shows image affordances the backend will just reject.
let imageConfig = { support_image: false, max_images_per_message: 3 };

// Pending images (data URLs) attached to the message currently being
// composed, cleared after each send.
let pendingImages = [];

/**
 * Fetches image-support feature-flag configuration from GET /api/config
 * and stores it in module state for use by paste/drop handlers and
 * sendMessage.
 *
 * @returns {Promise<{support_image: boolean, max_images_per_message: number}>}
 */
async function loadImageConfig() {
  try {
    const response = await fetch("/api/config");
    if (response.ok) {
      const data = await response.json();
      imageConfig = {
        support_image: Boolean(data.support_image),
        max_images_per_message: Number(data.max_images_per_message) || 3,
      };
    }
  } catch (e) {
    // Network error fetching config: leave the default (disabled) in
    // place rather than failing the whole page load.
  }
  return imageConfig;
}

/**
 * Converts a File/Blob (e.g. a pasted or dropped image) into a base64
 * data URL (`data:image/<format>;base64,...`).
 *
 * @param {Blob} file the image file/blob to encode
 * @returns {Promise<string>} the resulting data URL
 */
function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

/**
 * Adds a newly selected/pasted/dropped image file to `pendingImages`,
 * enforcing `imageConfig.max_images_per_message`, and renders its
 * thumbnail preview.
 *
 * Silently ignores non-image files and does nothing if image support is
 * disabled or the per-message cap has already been reached.
 *
 * @param {File} file the image file/blob to add
 * @returns {Promise<void>}
 */
async function addPendingImage(file) {
  if (!imageConfig.support_image) {
    return;
  }
  if (!file || !file.type || !file.type.startsWith("image/")) {
    return;
  }
  if (pendingImages.length >= imageConfig.max_images_per_message) {
    return;
  }

  const dataUrl = await fileToDataUrl(file);
  pendingImages.push(dataUrl);
  renderImagePreviews();
}

/**
 * Removes a pending image by index and re-renders the preview strip.
 *
 * @param {number} index index into `pendingImages` to remove
 * @returns {void}
 */
function removePendingImage(index) {
  pendingImages.splice(index, 1);
  renderImagePreviews();
}

/**
 * Renders thumbnail previews of all `pendingImages` into
 * `#image-preview-row`, each with a small remove ("x") control.
 *
 * No-op outside a browser DOM (e.g. under Node-based tests).
 *
 * @returns {void}
 */
function renderImagePreviews() {
  if (typeof document === "undefined") {
    return;
  }

  const row = document.getElementById("image-preview-row");
  if (!row) {
    return;
  }

  row.innerHTML = "";
  if (pendingImages.length === 0) {
    row.style.display = "none";
    return;
  }
  row.style.display = "flex";

  pendingImages.forEach((dataUrl, index) => {
    const wrapper = document.createElement("div");
    wrapper.className = "image-preview-thumb";

    const img = document.createElement("img");
    img.src = dataUrl;
    wrapper.appendChild(img);

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "image-preview-remove";
    removeBtn.textContent = "\u00D7"; // ×
    removeBtn.setAttribute("aria-label", "Remove image");
    removeBtn.addEventListener("click", () => removePendingImage(index));
    wrapper.appendChild(removeBtn);

    row.appendChild(wrapper);
  });
}

/**
 * Splits a base64 data URL into the Backend's expected
 * `{ data, format }` image payload shape.
 *
 * @param {string} dataUrl a `data:image/<format>;base64,...` URL
 * @returns {{data: string, format: string|undefined}}
 */
function dataUrlToImagePayload(dataUrl) {
  const match = /^data:image\/([a-zA-Z0-9.+-]+);base64,(.*)$/.exec(dataUrl);
  if (!match) {
    return { data: dataUrl, format: undefined };
  }
  return { data: match[2], format: match[1] };
}

/**
 * Clears all pending images (e.g. after a successful send or when
 * starting a new conversation).
 *
 * @returns {void}
 */
function clearPendingImages() {
  pendingImages = [];
  renderImagePreviews();
}

/**
 * Fallback UUID v4 generator used when `crypto.randomUUID` is unavailable
 * (e.g. the page is served over plain HTTP from a non-localhost origin,
 * where browsers restrict `crypto.randomUUID` to secure contexts).
 *
 * Produces a 36-character UUID v4 string using `crypto.getRandomValues`
 * for randomness when available (cryptographically strong), falling back
 * to `Math.random` only if `crypto` itself is entirely unavailable. Not a
 * substitute for serving the app over HTTPS in production — see README.md
 * deployment notes.
 *
 * @returns {string} a 36-character UUID v4 string
 */
function _fallbackUuidV4() {
  const bytes = new Uint8Array(16);
  if (typeof crypto !== "undefined" && crypto.getRandomValues) {
    crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i++) {
      bytes[i] = Math.floor(Math.random() * 256);
    }
  }

  // Set version (4) and variant (10) bits per RFC 4122.
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;

  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0"));
  return (
    hex.slice(0, 4).join("") +
    "-" +
    hex.slice(4, 6).join("") +
    "-" +
    hex.slice(6, 8).join("") +
    "-" +
    hex.slice(8, 10).join("") +
    "-" +
    hex.slice(10, 16).join("")
  );
}

/**
 * Generates a new Session_Id.
 *
 * Prefers crypto.randomUUID(), which produces a 36-character UUID string
 * (>= MIN_SESSION_ID_LENGTH), satisfying Requirement 3.1. `randomUUID` is
 * only exposed by browsers in secure contexts (HTTPS or localhost); when
 * unavailable (e.g. plain HTTP on a non-localhost origin), falls back to
 * `_fallbackUuidV4`, which produces an equivalent-shaped UUID v4 string
 * without relying on `randomUUID`.
 *
 * @returns {string} a newly generated UUID string of at least 33 characters
 */
function createSessionId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return _fallbackUuidV4();
}

/**
 * Returns the active Session_Id, generating and storing one first if none
 * exists yet.
 *
 * Requirement 3.1: generate a new Session_Id when none is active.
 * Requirement 3.2 (supporting): reuse the same Session_Id while active.
 *
 * @returns {string} the active Session_Id
 */
function getOrCreateSessionId() {
  if (sessionId === null) {
    sessionId = createSessionId();
  }
  return sessionId;
}

/**
 * Discards the current Session_Id and generates a new one, which will be
 * used for all subsequent requests.
 *
 * Requirement 3.3: New_Conversation_Control rotates the Session_Id.
 *
 * @returns {string} the newly generated Session_Id
 */
function startNewConversation() {
  sessionId = createSessionId();
  clearPendingImages();
  return sessionId;
}

/**
 * Determines whether a message is blank (empty or whitespace-only).
 *
 * Requirement 1.2: whitespace-only or empty messages must not be sent.
 *
 * @param {string} message the message text to check
 * @returns {boolean} true if the message is empty or contains only whitespace
 */
function isBlank(message) {
  return message.trim().length === 0;
}

/**
 * Sends a chat message to the Chat_Endpoint.
 *
 * Requirement 1.1: a non-empty message is sent as an HTTP POST to
 * `/api/chat` containing the exact message text and the current Session_Id.
 * Requirement 1.2: blank (empty/whitespace-only) messages short-circuit
 * without sending any request, UNLESS at least one image is attached (an
 * image-only message with no text is still sendable when image support is
 * enabled).
 * Requirement 3.2: the current Session_Id (obtained via
 * getOrCreateSessionId, generating one if none exists yet) is included so
 * that conversation continuity is maintained.
 *
 * When `pendingImages` is non-empty, each pending image (a base64 data URL)
 * is split into the Backend's `{ data, format }` shape and included as an
 * `images` array on the request body; pending images are cleared after the
 * request is issued regardless of outcome, so a failed send doesn't leave
 * stale images attached to the next message.
 *
 * @param {string} message the message text to send
 * @returns {Promise<void>}
 */
async function sendMessage(message) {
  const images = pendingImages.slice();

  if (isBlank(message) && images.length === 0) {
    return;
  }

  renderUserMessage(message, images);
  clearPendingImages();

  const body = JSON.stringify({
    message,
    session_id: getOrCreateSessionId(),
    images:
      images.length > 0
        ? images.map(dataUrlToImagePayload)
        : undefined,
  });

  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  });

  if (!response.ok) {
    renderError(await response.json());
    return;
  }

  await consumeSseStream(response.body, appendToCurrentAgentMessage);
}

/**
 * Pure reducer that appends a streamed chunk to the accumulated agent
 * message text so far.
 *
 * Requirement 2.4: streamed response chunks are appended, in order, to
 * form the full agent message. Kept independent of the DOM so it can be
 * property-tested as a plain (accumulatedText, chunk) -> newText reducer;
 * any DOM rendering is the responsibility of the caller (consumeSseStream).
 *
 * @param {string} accumulatedText the agent message text accumulated so far
 * @param {string} chunk the next chunk of streamed text to append
 * @returns {string} the new accumulated text
 */
function appendToCurrentAgentMessage(accumulatedText, chunk) {
  return accumulatedText + chunk;
}

// Module-level reference to the DOM element currently displaying the
// in-progress agent message bubble, so repeated renders update the same
// bubble instead of creating a new one per chunk. Reset to null whenever a
// new agent message begins (i.e. at the start of consumeSseStream).
let currentAgentMessageEl = null;

/**
 * Escapes HTML special characters so plain text can be safely inserted
 * into innerHTML without being interpreted as markup.
 *
 * @param {string} text raw text to escape
 * @returns {string} HTML-escaped text
 */
function _escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

/**
 * Creates a new message row (avatar + bubble) and appends it to
 * `#message-list`.
 *
 * Each call creates an independent row, so each user message and each
 * completed agent response is its own visually distinct bubble (rather
 * than all messages sharing one continuously-growing bubble).
 *
 * @param {"user"|"agent"} role which avatar/bubble styling to use
 * @returns {HTMLElement|null} the bubble element to render content into,
 *   or null if `#message-list` is not present in the DOM (e.g. when
 *   running under Node for tests)
 */
function _createMessageRow(role) {
  if (typeof document === "undefined") {
    return null;
  }

  const messageList = document.getElementById("message-list");
  if (!messageList) {
    return null;
  }

  const row = document.createElement("div");
  row.className = "message-row";

  const avatar = document.createElement("div");
  avatar.className = "avatar " + role;
  avatar.textContent = role === "user" ? "\uD83D\uDC64" : "\u26A1";
  // \uD83D\uDC64 = 👤 (user icon), \u26A1 = ⚡ (agent icon)

  const bubble = document.createElement("div");
  bubble.className = "message-bubble " + role;

  row.appendChild(avatar);
  row.appendChild(bubble);
  messageList.appendChild(row);
  messageList.scrollTop = messageList.scrollHeight;

  return bubble;
}

/**
 * Renders a user-submitted message as its own bubble in `#message-list`.
 *
 * User messages are shown as plain escaped text (not Markdown-rendered),
 * since the user's own input should display exactly as typed. Any attached
 * images are rendered as thumbnails above the text.
 *
 * @param {string} text the user's message text
 * @param {string[]} [images] data URLs of images attached to this message
 * @returns {void}
 */
function renderUserMessage(text, images) {
  const bubble = _createMessageRow("user");
  if (!bubble) {
    return;
  }

  if (images && images.length > 0) {
    const imageRow = document.createElement("div");
    imageRow.className = "message-image-row";
    images.forEach((dataUrl) => {
      const img = document.createElement("img");
      img.src = dataUrl;
      img.className = "message-image-thumb";
      imageRow.appendChild(img);
    });
    bubble.appendChild(imageRow);
  }

  const textNode = document.createElement("div");
  textNode.textContent = text;
  bubble.appendChild(textNode);
}

/**
 * Renders the given text as the current agent message bubble inside
 * `#message-list`, creating the bubble element on first use (one bubble
 * per agent response) and updating its content on subsequent calls so
 * the message appears to grow as streamed chunks arrive.
 *
 * The agent's text is Markdown (e.g. **bold**, ### headings, | tables |)
 * and is rendered to real HTML via marked.js, rather than displayed as
 * raw Markdown source text (Requirement 2.4).
 *
 * @param {string} text the full accumulated agent message text to display
 * @returns {void}
 */
function renderAgentMessage(text) {
  if (typeof document === "undefined") {
    return;
  }

  if (currentAgentMessageEl === null) {
    currentAgentMessageEl = _createMessageRow("agent");
    if (!currentAgentMessageEl) {
      return;
    }
  }

  if (typeof marked !== "undefined" && marked && typeof marked.parse === "function") {
    currentAgentMessageEl.innerHTML = marked.parse(text);
  } else {
    // marked.js failed to load (e.g. offline/CDN blocked) — fall back to
    // escaped plain text so raw Markdown source is still readable and no
    // markup injection is possible.
    currentAgentMessageEl.textContent = text;
  }

  const messageList = document.getElementById("message-list");
  if (messageList) {
    messageList.scrollTop = messageList.scrollHeight;
  }
}

/**
 * Extracts the content of a single SSE line, if it is a `data:` line.
 *
 * Mirrors the Backend's SSE framing (`data: <chunk>`), tolerating an
 * optional single space after the colon. Returns null for non-`data:`
 * lines (e.g. blank separator lines), which are ignored.
 *
 * @param {string} line a single line from the decoded SSE stream
 * @returns {string|null} the raw data content, or null if not a data line
 */
function extractSseDataContent(line) {
  if (!line.startsWith("data:")) {
    return null;
  }
  let content = line.slice("data:".length);
  if (content.startsWith(" ")) {
    content = content.slice(1);
  }
  return content;
}

/**
 * Decodes the raw content of a single SSE `data:` line into the chunk
 * value to feed into the reducer.
 *
 * The Backend may encode chunk content as a JSON-encoded string (mirroring
 * Agent_Client's own SSE parsing) or as a plain raw string, depending on
 * exact framing. To be safe, this tries JSON.parse first and falls back to
 * the raw string if parsing fails, so this works regardless of exact
 * framing.
 *
 * @param {string} dataContent the raw content after the `data:` prefix
 * @returns {string} the decoded chunk text
 */
function decodeSseChunk(dataContent) {
  try {
    return JSON.parse(dataContent);
  } catch (e) {
    return dataContent;
  }
}

/**
 * Determines whether a decoded SSE chunk is an error-event payload (as
 * opposed to a plain text content chunk).
 *
 * Mirrors app.py's SSE error-event mapping (task 8.4), which encodes
 * errors as a JSON object shape like `{ error: "timeout"|"internal",
 * message: "..." }` rather than a plain string.
 *
 * @param {*} chunk the decoded chunk value produced by decodeSseChunk
 * @returns {boolean} true if chunk is an error-event object
 */
function isSseErrorChunk(chunk) {
  return typeof chunk === "object" && chunk !== null && "error" in chunk;
}

/**
 * Displays a non-empty, user-visible error banner in `#error-banner`.
 *
 * Handles both a request-level error shape (e.g. `{ detail: "..." }` from
 * FastAPI's HTTPException, used for non-ok fetch responses such as 400/429)
 * and an SSE error-event shape (e.g. `{ error: "timeout"|"internal",
 * message: "..." }`, per app.py's task 8.4 SSE error mapping). Falls back
 * to a generic message if neither shape matches.
 *
 * Requirement 5.3: every error response renders a visible Frontend error.
 *
 * @param {*} errorPayload the parsed error payload (request-level JSON
 *   error body or SSE error-event object)
 * @returns {void}
 */
function renderError(errorPayload) {
  if (typeof document === "undefined") {
    return;
  }

  const banner = document.getElementById("error-banner");
  if (!banner) {
    return;
  }

  let message = null;
  if (errorPayload && typeof errorPayload === "object") {
    if (typeof errorPayload.message === "string" && errorPayload.message.trim() !== "") {
      message = errorPayload.message;
    } else if (typeof errorPayload.detail === "string" && errorPayload.detail.trim() !== "") {
      message = errorPayload.detail;
    } else if (typeof errorPayload.error === "string" && errorPayload.error.trim() !== "") {
      message = errorPayload.error;
    }
  }

  if (!message) {
    message = "An error occurred.";
  }

  banner.textContent = message;
  banner.style.display = "block";
}

/**
 * Displays the "incomplete response" indicator in `#incomplete-indicator`.
 *
 * Requirement 5.4: when the SSE connection is interrupted before the
 * stream completes, a visible "incomplete response" indicator is shown.
 *
 * @returns {void}
 */
function renderIncomplete() {
  if (typeof document === "undefined") {
    return;
  }

  const indicator = document.getElementById("incomplete-indicator");
  if (!indicator) {
    return;
  }

  indicator.style.display = "block";
}

/**
 * Consumes a fetch Response body ReadableStream containing an SSE
 * (text/event-stream) response, decoding `data:` lines in order and
 * folding each decoded chunk into the accumulated message text via
 * `reducer`, rendering the updated text to the DOM after each update.
 *
 * Requirement 2.4: streamed response chunks are appended, in order, to
 * form the full agent message, and the rendered DOM reflects each update.
 *
 * If a decoded chunk is an SSE error-event payload (see isSseErrorChunk),
 * it is routed to `renderError` instead of being appended to the agent
 * message, and no further chunks are appended for this stream
 * (Requirement 5.3).
 *
 * If the underlying `reader.read()` call throws (e.g. a network error or
 * abort interrupting the connection mid-stream) before the stream reaches a
 * normal `done` completion or an SSE error event, `renderIncomplete` is
 * called to surface the interruption, and the accumulated text so far is
 * returned instead of letting the exception propagate (Requirement 5.4).
 *
 * @param {ReadableStream} body the fetch Response.body stream to consume
 * @param {(accumulatedText: string, chunk: string) => string} reducer
 *   pure reducer folding a new chunk into the accumulated text
 * @returns {Promise<string>} the final accumulated text once the stream ends
 */
async function consumeSseStream(body, reducer) {
  currentAgentMessageEl = null;

  const reader = body.getReader();
  const decoder = new TextDecoder();

  let accumulatedText = "";
  let buffer = "";
  let errored = false;

  const handleChunk = (chunk) => {
    if (isSseErrorChunk(chunk)) {
      errored = true;
      renderError(chunk);
      return;
    }
    accumulatedText = reducer(accumulatedText, chunk);
    renderAgentMessage(accumulatedText);
  };

  while (true) {
    let done;
    let value;
    try {
      ({ done, value } = await reader.read());
    } catch (e) {
      // The stream was interrupted before a normal `done` completion or an
      // SSE error event (e.g. a network error/abort mid-stream). Surface
      // this distinctly from a clean completion or a genuine SSE error.
      // Requirement 5.4.
      if (!errored) {
        renderIncomplete();
      }
      break;
    }

    if (value && !errored) {
      buffer += decoder.decode(value, { stream: true });

      // SSE events are separated by newlines; split out complete lines and
      // keep any trailing partial line in the buffer for the next read.
      const lines = buffer.split("\n");
      buffer = lines.pop();

      for (const rawLine of lines) {
        if (errored) {
          break;
        }
        const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
        const dataContent = extractSseDataContent(line);
        if (dataContent === null) {
          continue;
        }
        const chunk = decodeSseChunk(dataContent);
        handleChunk(chunk);
      }
    }

    if (done) {
      // Flush any remaining buffered line (stream may end without a
      // trailing newline after the final data: line).
      if (!errored) {
        const dataContent = extractSseDataContent(buffer);
        if (dataContent !== null) {
          const chunk = decodeSseChunk(dataContent);
          handleChunk(chunk);
        }
      }
      break;
    }
  }

  return accumulatedText;
}

// Support both browser (plain <script>) usage and Node-based test runners
// (e.g. Jest/Vitest via CommonJS require) without affecting browser behavior.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    MIN_SESSION_ID_LENGTH,
    createSessionId,
    _fallbackUuidV4,
    getOrCreateSessionId,
    startNewConversation,
    isBlank,
    sendMessage,
    appendToCurrentAgentMessage,
    consumeSseStream,
    renderUserMessage,
    renderAgentMessage,
    renderError,
    renderIncomplete,
    loadImageConfig,
    fileToDataUrl,
    addPendingImage,
    removePendingImage,
    renderImagePreviews,
    dataUrlToImagePayload,
    clearPendingImages,
  };
}


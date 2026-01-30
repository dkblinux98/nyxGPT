# Client Capability Negotiation

nyxGPT supports client capability negotiation through HTTP headers, allowing the server to adapt responses based on client features.

## Overview

Clients can advertise their capabilities using standard and custom headers. The server detects these capabilities and adjusts the response format, content, and features accordingly.

## Supported Headers

### Standard Headers

- **Accept**: Preferred response content type
  - `text/event-stream`: Server-Sent Events (SSE) with event framing
  - `application/json`: JSON structured events
  - `text/plain`: Plain text streaming (legacy)

### Custom Headers

- **X-Client-Version**: Client version string (e.g., "nyxGPT-web/1.0")
- **X-API-Version**: Requested API version (e.g., "v1")
- **X-Supports-SSE**: Explicit SSE support flag ("true" or "false")
- **X-Supports-Structured-Events**: JSON structured events support
- **X-Supports-RAG**: RAG context support in responses
- **X-Supports-Metadata**: Metadata events support
- **X-Supports-Retry-Events**: Connection retry status events support
- **X-Supports-Streaming**: Any streaming support flag

## Response Formats

### SSE (Server-Sent Events)
Enabled when client sends `Accept: text/event-stream` or `X-Supports-SSE: true`.

```
event: message
id: 1
data: {"type": "text", "data": {"content": "Hello"}}

event: done
id: 2
data: {"type": "done", "data": {"total_tokens": 42}}
```

### JSON Structured Events
Enabled when client sends `Accept: application/json` or `X-Supports-Structured-Events: true`.

```json
{"type": "text", "data": {"content": "Hello", "token_index": 1}}
{"type": "done", "data": {"total_tokens": 42}}
```

### Plain Text (Legacy)
Default format for clients without capability headers.

```
Hello world
```

## Example Requests

### Modern Web Client (SSE + Structured Events)
```http
GET /api/chat/stream HTTP/1.1
Accept: text/event-stream
X-Client-Version: nyxGPT-web/1.0
X-Supports-SSE: true
X-Supports-Structured-Events: true
X-Supports-RAG: true
X-Supports-Metadata: true
```

### Simple Client (Plain Text)
```http
GET /api/chat/stream HTTP/1.1
Accept: text/plain
X-Client-Version: custom-client/0.1
X-Supports-RAG: false
```

### JavaScript Fetch Example
```javascript
fetch('/api/chat/stream', {
  method: 'POST',
  headers: {
    'Accept': 'text/event-stream',
    'Content-Type': 'application/json',
    'X-Client-Version': 'nyxGPT-web/1.0',
    'X-Supports-SSE': 'true',
    'X-Supports-Structured-Events': 'true',
  },
  body: JSON.stringify({
    prompt: 'Hello',
    session: 'default'
  })
})
```

## Graceful Degradation

The server automatically degrades features for older clients:

1. **No capability headers**: Plain text streaming
2. **Partial support**: Server adapts (e.g., SSE without structured events)
3. **Full support**: SSE with JSON structured events, metadata, RAG context

## Feature Detection

Clients can detect server capabilities by inspecting response headers:

- `Content-Type: text/event-stream` → Server sent SSE
- `Content-Type: application/json` → Server sent JSON
- `Content-Type: text/plain` → Server sent plain text

## Best Practices

1. **Always set Accept header**: Use `Accept: text/event-stream` for modern clients
2. **Include version info**: Help server track client versions for analytics
3. **Explicit capability flags**: Use `X-Supports-*` headers for unambiguous negotiation
4. **Handle all formats**: Implement fallbacks for plain text responses
5. **Test degradation**: Verify your client works without advanced features

## Implementation Status

- ✅ Capability detection from headers
- ✅ Response format negotiation
- ✅ Graceful degradation
- ⏳ Server-side adaptation in streaming endpoints (in progress)
- ⏳ WebUI client hint integration (planned)

# Copyright (c) Microsoft. All rights reserved.

"""Tiny local WebSocket proxy that bridges the browser to Azure AI Foundry.

The browser client (``index.html``) cannot set custom WebSocket headers,
but the Foundry gateway requires ``Authorization: Bearer <token>``. This
proxy bridges that gap for local development, and also serves
``index.html`` on the same port so you can just open the browser to
``http://localhost:8765/``:

    Browser  ──http://localhost:8765/──▶  proxy (serves index.html)
    Browser  ──ws://localhost:8765/invocations_ws──▶  proxy
    proxy    ──wss://<foundry>/voice-live/realtime──▶  Foundry (VoiceLive)

The proxy translates between the browser's invocations_ws protocol
(binary PCM + simple JSON events) and the Foundry VoiceLive protocol
(JSON events with base64-encoded audio).

The token is fetched from ``az account get-access-token`` (resource
``https://ai.azure.com``), refreshed lazily per new browser connection.

Usage
-----

    pip install websockets
    az login   # once

    python proxy.py

    # Or with explicit args:
    python proxy.py \
        --foundry https://<account>.services.ai.azure.com/api/projects/<project> \
        --agent hello-world

Then open http://localhost:8765/ in your browser.

Flags
-----
    --listen HOST:PORT   default 127.0.0.1:8765
    --foundry URL        Foundry project endpoint (or AZURE_AI_PROJECT_ENDPOINT)
    --agent NAME         agent name (or AZURE_AI_AGENT_NAME)

Security note: this proxy is for **local development only**. It listens on
loopback by default and uses your own ``az`` identity. Do not expose it on
a public network.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import pathlib
import struct
import subprocess
import sys
from http import HTTPStatus
from urllib.parse import urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv

load_dotenv()

import websockets
from websockets.asyncio.server import serve
from websockets.http11 import Response
from websockets.datastructures import Headers

INDEX_HTML_PATH = pathlib.Path(__file__).with_name("index.html")

# Audio constants matching the browser client
SAMPLE_RATE = 24_000
CHANNELS = 1
AUDIO_HEADER = struct.pack("<II", SAMPLE_RATE, CHANNELS)  # 8-byte LE header

# VoiceLive session configuration sent after connection
SESSION_CONFIG = {
    "type": "session.update",
    "session": {
        "modalities": ["text", "audio"],
        "voice": {"type": "azure-standard", "name": "en-US-Ava:DragonHDLatestNeural"},
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "turn_detection": {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 500,
        },
        "input_audio_echo_cancellation": {"type": "server_echo_cancellation"},
        "input_audio_noise_reduction": {"type": "azure_deep_noise_suppression"},
    },
}


def _voicelive_url(project_endpoint: str, agent: str) -> str:
    """Build the VoiceLive WebSocket URL from the project endpoint."""
    parts = urlsplit(project_endpoint)
    # Extract project name from the endpoint path
    # e.g. /api/projects/ai-project-foo -> ai-project-foo
    project_name = parts.path.rstrip("/").rsplit("/", 1)[-1]
    qs = urlencode({
        "api-version": "2026-06-01-preview",
        "agent-name": agent,
        "agent-project-name": project_name,
    })
    return urlunsplit((
        "wss" if parts.scheme in ("https", "wss") else "ws",
        parts.netloc,
        "/voice-live/realtime",
        qs, "",
    ))


def _entra_token(resource: str = "https://ai.azure.com") -> str:
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource", resource, "-o", "json"],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)["accessToken"]


def _http_response(status: HTTPStatus, body: bytes, content_type: str) -> Response:
    headers = Headers([
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ])
    return Response(status.value, status.phrase, headers, body)


async def _process_request(connection, request):
    """Serve index.html for plain HTTP GETs; let WebSocket upgrades pass."""
    if "upgrade" in request.headers.get("Connection", "").lower():
        return None
    path = request.path.split("?", 1)[0]
    if path in ("/", "/index.html"):
        try:
            body = INDEX_HTML_PATH.read_bytes()
        except FileNotFoundError:
            return _http_response(HTTPStatus.NOT_FOUND, b"index.html not found", "text/plain")
        return _http_response(HTTPStatus.OK, body, "text/html; charset=utf-8")
    return _http_response(HTTPStatus.NOT_FOUND, b"not found", "text/plain")


async def _browser_to_upstream(browser_ws, upstream, label: str) -> None:
    """Translate browser invocations_ws frames -> VoiceLive protocol."""
    try:
        async for msg in browser_ws:
            if isinstance(msg, bytes):
                # Binary PCM16 audio -> base64 input_audio_buffer.append
                audio_b64 = base64.b64encode(msg).decode("ascii")
                event = json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": audio_b64,
                })
                await upstream.send(event)
            else:
                # Text JSON from browser
                try:
                    data = json.loads(msg)
                except (json.JSONDecodeError, TypeError):
                    continue
                if data.get("type") == "text":
                    # Send as a user conversation item + trigger response
                    content = data.get("content", "")
                    await upstream.send(json.dumps({
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": content}],
                        },
                    }))
                    await upstream.send(json.dumps({"type": "response.create"}))
    except websockets.ConnectionClosed:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"[proxy] {label} error: {exc}", file=sys.stderr)
    finally:
        try:
            await upstream.close()
        except Exception:  # noqa: BLE001
            pass


async def _upstream_to_browser(upstream, browser_ws, label: str) -> None:
    """Translate VoiceLive protocol events -> browser invocations_ws frames."""
    transcript_buffer = ""
    try:
        async for msg in upstream:
            if isinstance(msg, bytes):
                # Shouldn't happen in VoiceLive (audio is base64 in JSON)
                continue
            try:
                event = json.loads(msg)
            except (json.JSONDecodeError, TypeError):
                continue

            event_type = event.get("type", "")

            if event_type == "session.created":
                session_id = event.get("session", {}).get("id", "")
                await browser_ws.send(json.dumps({
                    "type": "session_started",
                    "session_id": session_id,
                }))

            elif event_type == "session.updated":
                # Session config acknowledged
                pass

            elif event_type == "input_audio_buffer.speech_started":
                await browser_ws.send(json.dumps({
                    "type": "user_speech_started",
                }))

            elif event_type == "input_audio_buffer.speech_stopped":
                await browser_ws.send(json.dumps({
                    "type": "user_speech_stopped",
                }))

            elif event_type == "conversation.item.input_audio_transcription.completed":
                text = event.get("transcript", "")
                if text:
                    await browser_ws.send(json.dumps({
                        "type": "transcription",
                        "text": text,
                    }))

            elif event_type in ("response.audio_transcript.delta", "response.text.delta"):
                delta = event.get("delta", "")
                if delta:
                    transcript_buffer += delta
                    await browser_ws.send(json.dumps({
                        "type": "bot_text",
                        "delta": delta,
                    }))

            elif event_type in ("response.audio_transcript.done", "response.text.done"):
                text = event.get("transcript", "") or event.get("text", "") or transcript_buffer
                await browser_ws.send(json.dumps({
                    "type": "bot_text",
                    "final": True,
                    "text": text,
                }))
                transcript_buffer = ""

            elif event_type == "response.audio.delta":
                # Base64 PCM16 audio -> binary frame with 8-byte header
                delta_b64 = event.get("delta", "")
                if delta_b64:
                    pcm_data = base64.b64decode(delta_b64)
                    await browser_ws.send(AUDIO_HEADER + pcm_data)

            elif event_type == "response.done":
                await browser_ws.send(json.dumps({
                    "type": "response_done",
                }))

            elif event_type == "error":
                error = event.get("error", {})
                message = error.get("message", "") if isinstance(error, dict) else str(error)
                await browser_ws.send(json.dumps({
                    "type": "error",
                    "message": message,
                }))

            elif event_type in (
                "response.created",
                "response.output_item.added",
                "response.output_item.done",
                "response.content_part.added",
                "response.content_part.done",
                "response.audio.done",
                "conversation.item.created",
                "input_audio_buffer.committed",
                "input_audio_buffer.cleared",
            ):
                pass  # Internal lifecycle events, no browser equivalent

            else:
                print(f"[proxy] unhandled upstream event: {event_type}", file=sys.stderr)

    except websockets.ConnectionClosed:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"[proxy] {label} error: {exc}", file=sys.stderr)
    finally:
        try:
            await browser_ws.close()
        except Exception:  # noqa: BLE001
            pass


def _make_handler(args):
    async def handler(browser_ws):
        peer = browser_ws.remote_address
        url = _voicelive_url(args.foundry, args.agent)
        try:
            token = _entra_token()
        except subprocess.CalledProcessError as exc:
            print(f"[proxy] az token failed: {exc.stderr or exc}", file=sys.stderr)
            await browser_ws.close(code=1011, reason="auth failed")
            return
        headers = {
            "Authorization": f"Bearer {token}",
        }
        print(f"[proxy] {peer} -> {url}")
        try:
            async with websockets.connect(
                url,
                additional_headers=list(headers.items()),
                max_size=4 * 1024 * 1024,
                open_timeout=30,
            ) as upstream:
                # Wait for session.created, forward it, then send session config
                first_msg = await asyncio.wait_for(upstream.recv(), timeout=15)
                first_event = json.loads(first_msg)
                if first_event.get("type") == "session.created":
                    session_id = first_event.get("session", {}).get("id", "")
                    print(f"[proxy] session created: {session_id}")

                # Configure the VoiceLive session and wait for acknowledgment
                await upstream.send(json.dumps(SESSION_CONFIG))
                ack_msg = await asyncio.wait_for(upstream.recv(), timeout=15)
                ack_event = json.loads(ack_msg)
                if ack_event.get("type") == "session.updated":
                    print("[proxy] session configured")
                elif ack_event.get("type") == "error":
                    err = ack_event.get("error", {})
                    print(f"[proxy] session config error: {err}", file=sys.stderr)

                # Now tell the browser the session is ready
                await browser_ws.send(json.dumps({
                    "type": "session_started",
                    "session_id": session_id if first_event.get("type") == "session.created" else "",
                }))

                # Relay frames with protocol translation
                await asyncio.gather(
                    _browser_to_upstream(browser_ws, upstream, "browser->foundry"),
                    _upstream_to_browser(upstream, browser_ws, "foundry->browser"),
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[proxy] upstream connect failed: {exc}", file=sys.stderr)
            try:
                await browser_ws.close(code=1011, reason="upstream failed")
            except Exception:  # noqa: BLE001
                pass
        print(f"[proxy] {peer} closed")
    return handler


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--listen", default="127.0.0.1:8765",
                   help="HOST:PORT to bind (default 127.0.0.1:8765)")
    p.add_argument("--foundry",
                   default=os.getenv("AZURE_AI_PROJECT_ENDPOINT", ""),
                   help="Foundry project endpoint (or set AZURE_AI_PROJECT_ENDPOINT)")
    p.add_argument("--agent",
                   default=os.getenv("AZURE_AI_AGENT_NAME", ""),
                   help="Agent name (or set AZURE_AI_AGENT_NAME)")
    args = p.parse_args()

    if not args.foundry:
        p.error("--foundry is required (or set AZURE_AI_PROJECT_ENDPOINT)")
    if not args.agent:
        p.error("--agent is required (or set AZURE_AI_AGENT_NAME)")

    host, _, port = args.listen.partition(":")
    port_i = int(port or "8765")

    async def run():
        async with serve(
            _make_handler(args),
            host,
            port_i,
            max_size=4 * 1024 * 1024,
            process_request=_process_request,
        ):
            print(
                f"[proxy] listening on http://{host}:{port_i}/  (ws path: /invocations_ws)\n"
                f"[proxy]   -> VoiceLive: {_voicelive_url(args.foundry, args.agent)}"
            )
            await asyncio.Future()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

#!/usr/bin/env python
# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------
"""
FILE: client.py

DESCRIPTION:
    VoiceLive client for the temperature agents (and any other
    Invocations-protocol agent). Speech I/O is **always** VoiceLive (Azure
    realtime over PyAudio); only the conversational backend varies:

    1. ``hosted`` (default) — VoiceLive is bound to a Foundry hosted
       invocations agent via ``AgentSessionConfig``. Foundry routes each
       turn to the agent's ``/invocations`` endpoint, streams the SSE
       response back through VoiceLive, and VoiceLive speaks the result.

    2. ``custom invocation URL`` (``--invocation-url``) — VoiceLive is used
       for STT + TTS only. The script disables VoiceLive's own response
       generation (``turn_detection.create_response = false``), POSTs each
       completed user transcript to the supplied ``/invocations`` URL, then
       injects the agent's reply back into the session and asks VoiceLive
       to speak it. UI events from the agent are printed to the console.

USAGE:
    # Hosted-agent mode (Foundry routing)
    python client.py \\
        --endpoint https://<account>.services.ai.azure.com \\
        --agent-name orchestrator-agent \\
        --project-name my-foundry-project

    # Custom invocation URL (e.g. local container on port 8088)
    python client.py \\
        --endpoint https://<account>.services.ai.azure.com \\
        --invocation-url http://localhost:8088/invocations

    Environment variables (alternative to CLI args):
      AZURE_VOICELIVE_ENDPOINT
      AZURE_AI_HOSTED_AGENT_NAME
      AZURE_AI_PROJECT_NAME
      INVOCATION_URL
      AZURE_VOICELIVE_MODEL
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import queue
import signal
import sys
import uuid
from typing import TYPE_CHECKING, Any, Optional, Union, cast

import pyaudio
from dotenv import load_dotenv

load_dotenv()

# Type-only imports so VoiceLive annotations resolve in IDEs without forcing
# the SDK to be installed at import time for tooling.
if TYPE_CHECKING:
    from azure.ai.voicelive.aio import (  # type: ignore
        AgentSessionConfig,
        VoiceLiveConnection,
    )
    from azure.core.credentials_async import AsyncTokenCredential  # type: ignore

# Azure VoiceLive SDK — required.
from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AssistantMessageItem,
    AudioEchoCancellation,
    AudioInputTranscriptionOptions,
    AudioNoiseReduction,
    AzureSemanticVad,
    AzureStandardVoice,
    InputAudioFormat,
    LlmInterimResponseConfig,
    Modality,
    OutputAudioFormat,
    OutputTextContentPart,
    RequestSession,
    ResponseCreateParams,
    ServerEventType,
    ServerVad,
)
from azure.identity.aio import DefaultAzureCredential

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Audio processor — capture + playback over PyAudio (24kHz PCM16 mono)
# ---------------------------------------------------------------------------


class AudioProcessor:
    """Real-time audio capture and playback with sequence-number interrupts."""

    loop: asyncio.AbstractEventLoop

    class AudioPlaybackPacket:
        def __init__(self, seq_num: int, data: Optional[bytes]):
            self.seq_num = seq_num
            self.data = data

    def __init__(self, connection: "VoiceLiveConnection"):
        self.connection = connection
        self.audio = pyaudio.PyAudio()

        # PCM16, 24kHz, mono — matches VoiceLive defaults
        self.format = pyaudio.paInt16
        self.channels = 1
        self.rate = 24000
        self.chunk_size = 1200  # 50ms chunks

        self.input_stream: Optional[pyaudio.Stream] = None

        self.playback_queue: queue.Queue[AudioProcessor.AudioPlaybackPacket] = queue.Queue()
        self.playback_base = 0
        self.next_seq_num = 0
        self.output_stream: Optional[pyaudio.Stream] = None

        logger.info("AudioProcessor initialized with 24kHz PCM16 mono audio")

    def start_capture(self):
        def _capture_callback(in_data, _frame_count, _time_info, _status_flags):
            audio_base64 = base64.b64encode(in_data).decode("utf-8")
            future = asyncio.run_coroutine_threadsafe(
                self.connection.input_audio_buffer.append(audio=audio_base64),
                self.loop,
            )
            future.add_done_callback(
                lambda f: logger.error("Error in audio buffer append: %s", f.exception())
                if f.exception()
                else None
            )
            return (None, pyaudio.paContinue)

        if self.input_stream:
            return

        self.loop = asyncio.get_running_loop()
        try:
            self.input_stream = self.audio.open(
                format=self.format,
                channels=self.channels,
                rate=self.rate,
                input=True,
                frames_per_buffer=self.chunk_size,
                stream_callback=_capture_callback,
            )
            logger.info("Started audio capture")
        except Exception:
            logger.exception("Failed to start audio capture")
            raise

    def start_playback(self):
        if self.output_stream:
            return

        remaining = bytes()

        def _playback_callback(_in_data, frame_count, _time_info, _status_flags):
            nonlocal remaining
            frame_count *= pyaudio.get_sample_size(pyaudio.paInt16)

            out = remaining[:frame_count]
            remaining_local = remaining[frame_count:]

            while len(out) < frame_count:
                try:
                    packet = self.playback_queue.get_nowait()
                except queue.Empty:
                    out = out + bytes(frame_count - len(out))
                    continue
                except Exception:
                    logger.exception("Error in audio playback")
                    raise

                if not packet or not packet.data:
                    logger.info("End of playback queue.")
                    break

                if packet.seq_num < self.playback_base:
                    if len(remaining_local) > 0:
                        remaining_local = bytes()
                    continue

                num_to_take = frame_count - len(out)
                out = out + packet.data[:num_to_take]
                remaining_local = packet.data[num_to_take:]

            remaining = remaining_local

            if len(out) >= frame_count:
                return (out, pyaudio.paContinue)
            return (out, pyaudio.paComplete)

        try:
            self.output_stream = self.audio.open(
                format=self.format,
                channels=self.channels,
                rate=self.rate,
                output=True,
                frames_per_buffer=self.chunk_size,
                stream_callback=_playback_callback,
            )
            logger.info("Audio playback system ready")
        except Exception:
            logger.exception("Failed to initialize audio playback")
            raise

    def _get_and_increase_seq_num(self):
        seq = self.next_seq_num
        self.next_seq_num += 1
        return seq

    def queue_audio(self, audio_data: Optional[bytes]) -> None:
        self.playback_queue.put(
            AudioProcessor.AudioPlaybackPacket(
                seq_num=self._get_and_increase_seq_num(), data=audio_data
            )
        )

    def skip_pending_audio(self):
        self.playback_base = self._get_and_increase_seq_num()

    def shutdown(self):
        if self.input_stream:
            self.input_stream.stop_stream()
            self.input_stream.close()
            self.input_stream = None
        logger.info("Stopped audio capture")

        if self.output_stream:
            self.skip_pending_audio()
            self.queue_audio(None)
            self.output_stream.stop_stream()
            self.output_stream.close()
            self.output_stream = None
        logger.info("Stopped audio playback")

        if self.audio:
            self.audio.terminate()
        logger.info("Audio processor cleaned up")


# ---------------------------------------------------------------------------
# UI event rendering (custom ui.* events from a hosted agent)
# ---------------------------------------------------------------------------


def _print_ui_event(evt: dict[str, Any]) -> None:
    """Print any non-speech event streamed back by a custom /invocations agent."""
    etype = evt.get("type", "event")
    payload = {k: v for k, v in evt.items() if k != "type"}
    print(f"  [{etype}] {json.dumps(payload)}")


# ---------------------------------------------------------------------------
# VoiceLive client — two backend configurations sharing the same audio plumbing
# ---------------------------------------------------------------------------


class VoiceLiveClient:
    """VoiceLive client.

    * If ``agent_config`` is provided, VoiceLive is bound to a Foundry hosted
      invocations agent and the model's own response loop drives the
      conversation.

    * If ``invocation_url`` is provided instead, VoiceLive runs as a
      speech-only pipeline: ``turn_detection.create_response`` is set to
      ``False``, completed user transcripts are POSTed to ``invocation_url``,
      and the agent reply is injected back into the session and spoken.
    """

    # In custom-backend mode VoiceLive synthesizes the external agent's
    # reply verbatim via ResponseCreateParams.pre_generated_assistant_message.
    # No system prompt needed — the model just TTSs the supplied text.

    def __init__(
        self,
        endpoint: str,
        credential: "AsyncTokenCredential",
        agent_config: Optional["AgentSessionConfig"] = None,
        invocation_url: Optional[str] = None,
        invocation_session_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        if (agent_config is None) == (invocation_url is None):
            raise ValueError(
                "Provide exactly one of `agent_config` or `invocation_url`"
            )

        self.endpoint = endpoint
        self.credential = credential
        self.agent_config = agent_config
        self.invocation_url = invocation_url
        self.invocation_session_id = (
            invocation_session_id or f"local-{uuid.uuid4().hex[:8]}"
        )
        self.model = model
        self.connection: Optional["VoiceLiveConnection"] = None
        self.audio_processor: Optional[AudioProcessor] = None
        self.session_ready = False
        self._pending_turn: Optional[asyncio.Task[None]] = None
        self._http_client = None  # lazy

    # ---- public entry point ------------------------------------------------

    async def start(self) -> None:
        try:
            connect_kwargs: dict[str, Any] = {
                "endpoint": self.endpoint,
                "credential": self.credential,
            }
            if self.agent_config is not None:
                logger.info(
                    "Connecting to VoiceLive with hosted agent %s in project %s",
                    self.agent_config.get("agent_name"),
                    self.agent_config.get("project_name"),
                )
                connect_kwargs["agent_config"] = self.agent_config
                if self.model:
                    connect_kwargs["model"] = self.model
            else:
                logger.info(
                    "Connecting to VoiceLive in custom-backend mode (model=%s, invocation=%s)",
                    self.model or "<default>",
                    self.invocation_url,
                )
                if self.model:
                    connect_kwargs["model"] = self.model

            async with connect(**connect_kwargs) as connection:
                self.connection = connection

                ap = AudioProcessor(connection)
                self.audio_processor = ap

                await self._setup_session()
                ap.start_playback()

                self._print_banner()
                await self._process_events()
        except Exception:
            logger.exception("Voice client encountered an error")
            raise
        finally:
            if self.audio_processor:
                self.audio_processor.shutdown()
            if self._http_client is not None:
                try:
                    await self._http_client.aclose()
                except Exception:  # pragma: no cover
                    pass

    # ---- session setup -----------------------------------------------------

    def _print_banner(self) -> None:
        print("\n" + "=" * 60)
        print("  TEMPERATURE AGENTS — VOICE CLIENT (VoiceLive)")
        if self.agent_config is not None:
            print("  Mode    : hosted agent")
            print(f"  Agent   : {self.agent_config.get('agent_name')}")
            print(f"  Project : {self.agent_config.get('project_name')}")
        else:
            print("  Mode    : custom invocation URL")
            print(f"  Backend : {self.invocation_url}")
            print(f"  Session : {self.invocation_session_id}")
        print("  Try: 'What's the temperature outside?' or 'How warm is it inside?'")
        print("  Press Ctrl+C to exit")
        print("=" * 60 + "\n")

    async def _setup_session(self) -> None:
        logger.info("Setting up voice conversation session...")
        voice_config = AzureStandardVoice(name="en-US-Ava:DragonHDLatestNeural")

        if self.agent_config is not None:
            # Hosted-agent mode: VoiceLive's own response loop is in charge.
            turn_detection_config = ServerVad(
                threshold=0.5,
                prefix_padding_ms=300,
                silence_duration_ms=500,
            )
            session_config = RequestSession(
                modalities=[Modality.TEXT, Modality.AUDIO],
                voice=voice_config,
                input_audio_format=InputAudioFormat.PCM16,
                output_audio_format=OutputAudioFormat.PCM16,
                turn_detection=turn_detection_config,
                input_audio_echo_cancellation=AudioEchoCancellation(),
                input_audio_noise_reduction=AudioNoiseReduction(
                    type="azure_deep_noise_suppression"
                ),
                interim_response=LlmInterimResponseConfig(latency_threshold_ms=500),
            )
        else:
            # Custom-backend mode: VoiceLive is STT + TTS only. Use the
            # `azure-speech` transcription model (built-in on AI Services
            # accounts), which requires Azure Semantic VAD. Disable
            # auto-response so we drive turns manually after calling the
            # external invocations endpoint.
            turn_detection_config = AzureSemanticVad(
                threshold=0.5,
                prefix_padding_ms=300,
                silence_duration_ms=500,
                create_response=False,
            )

            session_config = RequestSession(
                modalities=[Modality.TEXT, Modality.AUDIO],
                voice=voice_config,
                input_audio_format=InputAudioFormat.PCM16,
                output_audio_format=OutputAudioFormat.PCM16,
                turn_detection=turn_detection_config,
                input_audio_transcription=AudioInputTranscriptionOptions(
                    model="azure-speech",
                    language="en-US",
                ),
                input_audio_echo_cancellation=AudioEchoCancellation(),
                input_audio_noise_reduction=AudioNoiseReduction(
                    type="azure_deep_noise_suppression"
                ),
            )

        conn = self.connection
        assert conn is not None
        await conn.session.update(session=session_config)
        logger.info("Session configuration sent")

    # ---- main event loop ---------------------------------------------------

    async def _process_events(self) -> None:
        try:
            conn = self.connection
            assert conn is not None
            async for event in conn:
                await self._handle_event(event)
        except Exception:
            logger.exception("Error processing events")
            raise

    async def _handle_event(self, event) -> None:
        logger.debug("Received event: %s", event.type)
        ap = self.audio_processor
        assert ap is not None

        if event.type == ServerEventType.SESSION_UPDATED:
            logger.info("Session ready: %s", event.session.id)
            self.session_ready = True
            ap.start_capture()

        elif (
            event.type
            == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED
        ):
            transcript = getattr(event, "transcript", "") or ""
            print(f"\n  You: {transcript}")
            if self.invocation_url is not None and transcript.strip():
                self._schedule_custom_turn(transcript.strip())

        elif (
            event.type
            == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED
        ):
            # Non-fatal: server still delivers the transcript via the
            # COMPLETED event. Swallow silently.
            pass

        elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            print(f'\n  Agent: {getattr(event, "transcript", "")}')

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            logger.info("User started speaking — stopping playback")
            print("  [Listening...]")
            ap.skip_pending_audio()

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
            logger.info("User stopped speaking")
            print("  [Processing...]")

        elif event.type == ServerEventType.RESPONSE_CREATED:
            logger.info("Assistant response created")

        elif event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
            ap.queue_audio(event.delta)

        elif event.type == ServerEventType.RESPONSE_AUDIO_DONE:
            logger.info("Assistant finished speaking")

        elif event.type == ServerEventType.RESPONSE_DONE:
            logger.info("Response complete")
            print("  [Ready for next input...]")

        elif event.type == ServerEventType.ERROR:
            logger.error("VoiceLive error: %s", event.error.message)
            print(f"  Error: {event.error}")

        elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA:
            delta = getattr(event, "delta", "")
            if delta:
                print(f"{delta}", end="", flush=True)

        else:
            logger.debug("Unhandled event type: %s", event.type)

    # ---- custom-backend turn handling --------------------------------------

    def _schedule_custom_turn(self, transcript: str) -> None:
        if self._pending_turn is not None and not self._pending_turn.done():
            logger.warning(
                "Skipping new turn — previous /invocations call still in flight"
            )
            return
        self._pending_turn = asyncio.create_task(self._run_custom_turn(transcript))

    async def _run_custom_turn(self, transcript: str) -> None:
        assert self.invocation_url is not None
        try:
            reply, ui_events = await self._post_invocation(transcript)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Invocation call failed")
            print(f"  Error talking to {self.invocation_url}: {exc}")
            return

        if reply:
            print(f"\n  Agent: {reply}")
        for evt in ui_events:
            _print_ui_event(evt)

        if reply:
            await self._speak_reply(reply)

    async def _post_invocation(
        self, transcript: str
    ) -> tuple[str, list[dict[str, Any]]]:
        import httpx  # required for custom-backend mode (in requirements.txt)

        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, read=120.0)
            )

        url = f"{self.invocation_url}?agent_session_id={self.invocation_session_id}"
        payload = {"type": "input_audio.transcription", "input": transcript}
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}

        spoken_parts: list[str] = []
        spoken_done: Optional[str] = None
        ui_events: list[dict[str, Any]] = []

        async with self._http_client.stream(
            "POST", url, json=payload, headers=headers
        ) as resp:
            resp.raise_for_status()
            async for raw_line in resp.aiter_lines():
                if not raw_line or not raw_line.startswith("data:"):
                    continue
                data = raw_line[len("data:") :].strip()
                if not data:
                    continue
                try:
                    evt = json.loads(data)
                except json.JSONDecodeError:
                    logger.warning("Could not parse SSE line: %s", data)
                    continue

                etype = evt.get("type", "")
                if etype == "output_audio_transcription.delta":
                    spoken_parts.append(evt.get("delta", ""))
                elif etype == "output_audio_transcription.done":
                    spoken_done = evt.get("text", "")
                elif etype == "done":
                    break
                else:
                    ui_events.append(evt)

        text = spoken_done if spoken_done is not None else "".join(spoken_parts)
        return (text, ui_events)

    async def _speak_reply(self, text: str) -> None:
        """Have VoiceLive synthesize `text` verbatim as the assistant turn."""
        conn = self.connection
        assert conn is not None

        assistant_msg = AssistantMessageItem(
            content=[OutputTextContentPart(text=text)],
        )
        await conn.response.create(
            response=ResponseCreateParams(
                modalities=[Modality.AUDIO],
                pre_generated_assistant_message=assistant_msg,
            )
        )


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------


async def run_client(args: argparse.Namespace) -> None:
    credential = DefaultAzureCredential()
    logger.info("Using DefaultAzureCredential")

    if args.invocation_url:
        client = VoiceLiveClient(
            endpoint=args.endpoint,
            credential=credential,
            invocation_url=args.invocation_url,
            invocation_session_id=args.session_id,
            model=args.model or "gpt-realtime",
        )
    else:
        agent_config: "AgentSessionConfig" = {
            "agent_name": args.agent_name,
            "project_name": args.project_name,
        }
        client = VoiceLiveClient(
            endpoint=args.endpoint,
            credential=credential,
            agent_config=agent_config,
            model=args.model or "gpt-realtime",
        )

    await client.start()


def main(args: argparse.Namespace) -> None:
    def signal_handler(_sig, _frame):
        logger.info("Received shutdown signal")
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        asyncio.run(run_client(args))
    except KeyboardInterrupt:
        print("\n  Voice client shut down. Goodbye!")
    except Exception as e:  # noqa: BLE001
        print("Fatal Error: ", e)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "VoiceLive client for the temperature agents. "
            "Speech I/O is always VoiceLive; the conversational backend can "
            "be either a Foundry hosted agent or a custom /invocations URL."
        ),
    )
    parser.add_argument(
        "--endpoint",
        default=os.getenv("AZURE_VOICELIVE_ENDPOINT", ""),
        help="VoiceLive endpoint (or set AZURE_VOICELIVE_ENDPOINT).",
    )
    parser.add_argument(
        "--agent-name",
        default=os.getenv("AZURE_AI_HOSTED_AGENT_NAME", "orchestrator-agent"),
        help="Hosted-agent mode: the deployed invocations agent name "
             "(default: orchestrator-agent, which routes to weather-agent "
             "and homeassistant-agent).",
    )
    parser.add_argument(
        "--project-name",
        default=os.getenv("AZURE_AI_PROJECT_NAME", ""),
        help="Hosted-agent mode: the Foundry project containing the agent "
             "(or set AZURE_AI_PROJECT_NAME).",
    )
    parser.add_argument(
        "--invocation-url",
        default=os.getenv("INVOCATION_URL", ""),
        help="Custom-backend mode: full URL of an /invocations endpoint "
             "(e.g. http://localhost:8088/invocations). When set, VoiceLive "
             "is used for STT + TTS only and conversational logic comes from "
             "this endpoint instead of a Foundry hosted agent.",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Custom-backend mode: agent_session_id pinned across turns "
             "(default: random per process).",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("AZURE_VOICELIVE_MODEL", ""),
        help="Custom-backend mode: VoiceLive realtime model to use "
             "(default: gpt-realtime).",
    )
    args = parser.parse_args()

    if not args.endpoint:
        print("Error: --endpoint is required (or set AZURE_VOICELIVE_ENDPOINT).")
        sys.exit(1)

    if not args.invocation_url and not args.project_name:
        print(
            "Error: either --invocation-url (custom backend) or --project-name "
            "(hosted agent, or set AZURE_AI_PROJECT_NAME) is required."
        )
        sys.exit(1)

    # Audio device sanity check.
    try:
        p = pyaudio.PyAudio()
        input_devices = [
            i
            for i in range(p.get_device_count())
            if cast(
                Union[int, float],
                p.get_device_info_by_index(i).get("maxInputChannels", 0) or 0,
            )
            > 0
        ]
        output_devices = [
            i
            for i in range(p.get_device_count())
            if cast(
                Union[int, float],
                p.get_device_info_by_index(i).get("maxOutputChannels", 0) or 0,
            )
            > 0
        ]
        p.terminate()
        if not input_devices:
            print("Error: No audio input devices found.")
            sys.exit(1)
        if not output_devices:
            print("Error: No audio output devices found.")
            sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"Error: Audio system check failed: {e}")
        sys.exit(1)

    print("  Temperature Agents — VoiceLive Client")
    print("=" * 60)
    print(f"  Endpoint : {args.endpoint}")
    if args.invocation_url:
        print(f"  Backend  : custom invocation URL → {args.invocation_url}")
    else:
        print(f"  Backend  : hosted agent {args.agent_name} (project {args.project_name})")
    print("=" * 60)

    main(args)

"""Azure VoiceLive realtime handler bound to a Foundry hosted agent.

This replaces the Hugging Face realtime WebSocket backend
(``huggingface_realtime.py``) with Azure **VoiceLive**. VoiceLive performs the
speech pipeline (speech-to-text, turn detection and text-to-speech) while the
conversation itself is driven by a Foundry **hosted agent** — by default the
``orchestrator-agent`` that fans out to the specialist agents in this repo.

The handler implements the same :class:`ConversationHandler` contract used by
the local audio loop (``console.py``): microphone frames arrive via
:meth:`receive`, synthesized audio and UI messages are produced through the
inherited ``emit``/``output_queue`` plumbing.

VoiceLive and the OpenAI-compatible realtime API share the same event shape, so
the event loop below mirrors the Hugging Face handler closely. The main
differences are:

* the transport is :func:`azure.ai.voicelive.aio.connect` bound to a hosted
  agent (``agent_name`` / ``project_name``) authenticated with
  ``DefaultAzureCredential``;
* microphone audio is resampled to VoiceLive's 24 kHz PCM16 input and the 24 kHz
  PCM16 responses are resampled back to the robot playback rate;
* voices are Azure neural voices instead of the Hugging Face speaker catalog.

Robot motion tool calls (moves/dance/emotion) are preserved: if VoiceLive
surfaces client-side ``response.function_call_arguments.done`` events they are
executed through the same :class:`BackgroundToolManager`. When bound to a
hosted agent the conversation logic — including any agent-side tools — lives in
the agent; audio-reactive head motion continues to work through the daemon
wobbler regardless.
"""

from __future__ import annotations

import json
import time
import uuid
import base64
import asyncio
import logging
from typing import TYPE_CHECKING, Any, Final, Optional

import numpy as np
from numpy.typing import NDArray

from azure.identity.aio import DefaultAzureCredential
from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AssistantMessageItem,
    AudioEchoCancellation,
    AudioInputTranscriptionOptions,
    AudioNoiseReduction,
    AzureSemanticVad,
    AzureStandardVoice,
    InputAudioFormat,
    InputTextContentPart,
    Modality,
    OutputAudioFormat,
    OutputTextContentPart,
    RequestSession,
    ResponseCreateParams,
    ServerEventType,
    UserMessageItem,
)

from reachy_mini_conversation_app.config import (
    config,
    get_available_voices,
    get_default_voice,
)
from reachy_mini_conversation_app.prompts import (
    get_session_greeting_prompt,
    get_session_voice,
)
from reachy_mini_conversation_app.streaming import AdditionalOutputs, audio_to_int16
from reachy_mini_conversation_app.conversation_handler import ConversationHandler
from reachy_mini_conversation_app.tools import core_tools
from reachy_mini_conversation_app.tools.core_tools import ToolDependencies
from reachy_mini_conversation_app.tools.background_tool_manager import (
    BackgroundToolManager,
    ToolCallRoutine,
    ToolNotification,
)

if TYPE_CHECKING:
    from azure.ai.voicelive.aio import VoiceLiveConnection
    from azure.core.credentials_async import AsyncTokenCredential


logger = logging.getLogger(__name__)

# VoiceLive realtime uses 24 kHz PCM16 mono for both input and output.
VOICELIVE_SAMPLE_RATE: Final[int] = 24000
# Playback rate handed to the robot media pipeline. Matches the rate the Hugging
# Face backend produced so the wobbler / playback behaviour is unchanged.
ROBOT_PLAYBACK_RATE: Final[int] = 16000

_RESPONSE_DONE_TIMEOUT: Final[float] = 30.0


def _resample_int16(samples: NDArray[np.int16], src_rate: int, dst_rate: int) -> NDArray[np.int16]:
    """Resample mono int16 audio using linear interpolation (adequate for speech)."""
    if src_rate == dst_rate or samples.size == 0:
        return samples
    duration = samples.shape[0] / float(src_rate)
    dst_count = max(1, int(round(duration * dst_rate)))
    src_positions = np.linspace(0.0, samples.shape[0] - 1, num=samples.shape[0], dtype=np.float64)
    dst_positions = np.linspace(0.0, samples.shape[0] - 1, num=dst_count, dtype=np.float64)
    resampled = np.interp(dst_positions, src_positions, samples.astype(np.float64))
    return np.clip(np.round(resampled), -32768, 32767).astype(np.int16)


class VoiceLiveRealtimeHandler(ConversationHandler):
    """Realtime stream handler that bridges robot audio to Azure VoiceLive."""

    # Sample rate of the audio frames emitted to the robot playback pipeline.
    SAMPLE_RATE = ROBOT_PLAYBACK_RATE

    def __init__(
        self,
        deps: ToolDependencies,
        instance_path: Optional[str] = None,
        startup_voice: Optional[str] = None,
    ) -> None:
        """Initialize the handler."""
        super().__init__()

        self.deps = deps
        self.instance_path = instance_path

        self.connection: Optional["VoiceLiveConnection"] = None
        self.output_queue: "asyncio.Queue[Any]" = asyncio.Queue()

        self._credential: Optional["AsyncTokenCredential"] = None
        self._voice_override: Optional[str] = self._resolve_backend_voice(
            startup_voice, source="persisted startup voice"
        )

        # Background tool manager (preserves moves/dance/emotion plumbing).
        self.tool_manager = BackgroundToolManager()

        # Lifecycle / response gating.
        self._connected_event: asyncio.Event = asyncio.Event()
        self._response_done_event: asyncio.Event = asyncio.Event()
        self._response_done_event.set()
        self._pending_responses: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
        self._startup_greeting_sent = False
        self._stopping = False

    # ------------------------------------------------------------------
    # Connection settings
    # ------------------------------------------------------------------

    def _endpoint(self) -> str:
        endpoint = (config.AZURE_VOICELIVE_ENDPOINT or "").strip()
        if not endpoint:
            raise RuntimeError(
                "AZURE_VOICELIVE_ENDPOINT is not set. Configure the VoiceLive "
                "endpoint before starting the conversation app."
            )
        return endpoint

    def _connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "endpoint": self._endpoint(),
            "credential": self._credential,
            "model": config.AZURE_VOICELIVE_MODEL,
        }
        agent_name = (config.AZURE_AI_HOSTED_AGENT_NAME or "").strip()
        project_name = (config.AZURE_AI_PROJECT_NAME or "").strip()
        if agent_name:
            kwargs["agent_name"] = agent_name
        if project_name:
            kwargs["project_name"] = project_name
        return kwargs

    # ------------------------------------------------------------------
    # Voice helpers
    # ------------------------------------------------------------------

    def _resolve_backend_voice(
        self,
        voice: Optional[str],
        *,
        source: str,
        fallback: Optional[str] = None,
    ) -> Optional[str]:
        """Return a backend-supported voice, optionally falling back when unsupported."""
        available_voices = get_available_voices()
        voice_value = (voice or "").strip()
        if not voice_value:
            return fallback

        voice_by_lowercase = {candidate.lower(): candidate for candidate in available_voices}
        normalized_voice = voice_by_lowercase.get(voice_value.lower())
        if normalized_voice is not None:
            return normalized_voice

        if voice:
            logger.warning(
                "Ignoring unsupported %s %r; expected one of %s",
                source,
                voice,
                available_voices,
            )
        return fallback

    def get_current_voice(self) -> str:
        """Return the voice currently selected for this handler."""
        default_voice = get_default_voice()
        voice = self._voice_override or get_session_voice(default=default_voice)
        return self._resolve_backend_voice(voice, source="session voice", fallback=default_voice) or default_voice

    async def get_available_voices(self) -> list[str]:
        """Return voices available for the VoiceLive backend."""
        return get_available_voices()

    async def change_voice(self, voice: str) -> str:
        """Change the voice, updating the active session when possible."""
        default_voice = get_default_voice()
        resolved_voice = (
            self._resolve_backend_voice(voice, source="requested voice", fallback=default_voice) or default_voice
        )
        self._voice_override = resolved_voice
        if self.connection is not None:
            try:
                await self.connection.session.update(
                    session=RequestSession(voice=AzureStandardVoice(name=resolved_voice)),
                )
                return f"Voice changed to {resolved_voice}."
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to update live session for voice change: %s", e)
                return "Voice change failed. Will take effect on next connection."
        return "Voice changed. Will take effect on next connection."

    async def apply_personality(self, profile: str | None) -> str:
        """Apply a new personality (profile) at runtime if possible."""
        try:
            from reachy_mini_conversation_app.config import config as _config
            from reachy_mini_conversation_app.config import set_custom_profile

            set_custom_profile(profile)
            logger.info(
                "Set custom profile to %r (config=%r)",
                profile,
                getattr(_config, "REACHY_MINI_CUSTOM_PROFILE", None),
            )

            try:
                voice = self.get_current_voice()
            except Exception as e:  # noqa: BLE001
                logger.error("Failed to resolve personality content: %s", e)
                return f"Failed to apply personality: {e}"

            # Rebuild the tool registry so profile tool changes take effect.
            core_tools.initialize_tools(force=True)

            if self.connection is not None:
                try:
                    # Instructions are read-only in hosted-agent mode, so only
                    # the voice can be pushed to the live session.
                    await self.connection.session.update(
                        session=RequestSession(
                            voice=AzureStandardVoice(name=voice),
                        ),
                    )
                    logger.info("Applied personality via live update: %s", profile or "built-in default")
                    return "Applied personality."
                except Exception as e:  # noqa: BLE001
                    logger.warning("Live personality update failed: %s", e)
                    return "Applied personality. Will take effect on next connection."
            logger.info(
                "Applied personality recorded: %s (no live connection; applies next session)",
                profile or "built-in default",
            )
            return "Applied personality. Will take effect on next connection."
        except Exception as e:  # noqa: BLE001
            logger.error("Error applying personality '%s': %s", profile, e)
            return f"Failed to apply personality: {e}"

    # ------------------------------------------------------------------
    # ConversationHandler contract
    # ------------------------------------------------------------------

    def _is_connected(self) -> bool:
        return self.connection is not None

    def _idle_behavior_ready(self) -> bool:
        """Hold idle behavior while a model response is still active."""
        return self._response_done_event.is_set()

    async def start_up(self) -> None:
        """Connect to VoiceLive and run the realtime session."""
        self._credential = DefaultAzureCredential()
        try:
            await self._run_session()
        finally:
            self.connection = None
            self._connected_event.clear()
            if self._credential is not None:
                try:
                    await self._credential.close()
                except Exception:  # noqa: BLE001
                    pass
                self._credential = None

    async def shutdown(self) -> None:
        """Shut down the handler."""
        self._stopping = True
        self._response_done_event.set()

        await self.tool_manager.shutdown()

        if self.connection is not None:
            try:
                await self.connection.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("connection.close() ignored: %s", e)
            finally:
                self.connection = None

        while not self.output_queue.empty():
            try:
                self.output_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def receive(self, frame: tuple[int, NDArray[np.int16]]) -> None:
        """Receive a microphone frame and forward it to VoiceLive."""
        conn = self.connection
        if conn is None:
            return

        sample_rate, audio_frame = frame
        if audio_frame.size == 0:
            return

        # Collapse to mono (channels-last convention).
        if audio_frame.ndim == 2:
            if audio_frame.shape[1] > audio_frame.shape[0]:
                audio_frame = audio_frame.T
            if audio_frame.shape[1] > 1:
                audio_frame = audio_frame[:, 0]
            else:
                audio_frame = audio_frame[:, 0]

        audio_frame = audio_to_int16(np.ascontiguousarray(audio_frame))
        audio_frame = _resample_int16(audio_frame, int(sample_rate), VOICELIVE_SAMPLE_RATE)

        try:
            audio_message = base64.b64encode(audio_frame.tobytes()).decode("utf-8")
            await conn.input_audio_buffer.append(audio=audio_message)
        except Exception as e:  # noqa: BLE001
            logger.debug("Dropping audio frame: connection not ready (%s)", e)

    # ------------------------------------------------------------------
    # Session setup + event loop
    # ------------------------------------------------------------------

    def _build_session_config(self) -> RequestSession:
        """Return the VoiceLive session configuration for hosted-agent mode."""
        language = getattr(config, "REALTIME_TRANSCRIPTION_LANGUAGE", None) or "en-US"
        # In hosted-agent mode the agent owns its instructions; they are
        # read-only on the VoiceLive session, so `instructions` must not be
        # sent here or the session.update is rejected.
        return RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            voice=AzureStandardVoice(name=self.get_current_voice()),
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            # Azure Semantic VAD supports the built-in azure-speech transcription
            # model and lets VoiceLive auto-drive the hosted agent's response.
            turn_detection=AzureSemanticVad(
                threshold=0.5,
                prefix_padding_ms=300,
                silence_duration_ms=500,
            ),
            input_audio_transcription=AudioInputTranscriptionOptions(
                model="azure-speech",
                language=language,
            ),
            input_audio_echo_cancellation=AudioEchoCancellation(),
            input_audio_noise_reduction=AudioNoiseReduction(type="azure_deep_noise_suppression"),
        )

    async def _run_session(self) -> None:
        """Establish and manage a single VoiceLive session."""
        async with connect(**self._connect_kwargs()) as conn:
            self.connection = conn
            try:
                await conn.session.update(session=self._build_session_config())
                logger.info(
                    "VoiceLive session initialized (agent=%r project=%r voice=%r)",
                    config.AZURE_AI_HOSTED_AGENT_NAME,
                    config.AZURE_AI_PROJECT_NAME,
                    self.get_current_voice(),
                )
            except Exception:
                logger.exception("VoiceLive session.update failed; aborting startup")
                raise

            self._connected_event.set()
            self.tool_manager.start_up(tool_callbacks=[self._handle_tool_result])
            response_sender_task = asyncio.create_task(self._response_sender_loop(), name="vl-response-sender")

            try:
                async for event in conn:
                    await self._handle_event(event)
            finally:
                response_sender_task.cancel()
                try:
                    await response_sender_task
                except asyncio.CancelledError:
                    pass
                await self.tool_manager.shutdown()

    async def _handle_event(self, event: Any) -> None:
        logger.debug("VoiceLive event: %s", event.type)

        if event.type == ServerEventType.SESSION_UPDATED:
            logger.info("VoiceLive session ready: %s", getattr(event.session, "id", "<unknown>"))
            await self._send_startup_greeting_prompt()

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            self._mark_activity("user_speech_started")
            if self._clear_queue:
                self._clear_queue()
            self.deps.movement_manager.set_listening(True)

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
            self._mark_activity("user_speech_stopped")
            self.deps.movement_manager.set_listening(False)

        elif event.type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_DELTA:
            self._mark_activity("user_transcription_delta")
            delta = getattr(event, "delta", "") or ""
            if delta:
                await self.output_queue.put(AdditionalOutputs({"role": "user_partial", "content": delta}))

        elif event.type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            self._mark_activity("user_transcription_completed")
            transcript = (getattr(event, "transcript", "") or "").strip()
            self.deps.movement_manager.set_listening(False)
            if transcript:
                await self.output_queue.put(AdditionalOutputs({"role": "user", "content": transcript}))

        elif event.type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED:
            logger.debug("Input transcription failed (non-fatal)")

        elif event.type == ServerEventType.RESPONSE_CREATED:
            self._mark_activity("response_created")
            self._response_done_event.clear()

        elif event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
            self._mark_activity("assistant_audio_delta")
            await self._queue_output_audio(getattr(event, "delta", None))

        elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            self._mark_activity("assistant_transcript_done")
            transcript = getattr(event, "transcript", "") or ""
            if transcript:
                await self.output_queue.put(AdditionalOutputs({"role": "assistant", "content": transcript}))

        elif event.type == ServerEventType.RESPONSE_DONE:
            self._response_done_event.set()

        elif event.type == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
            await self._handle_function_call(event)

        elif event.type == ServerEventType.ERROR:
            err = getattr(event, "error", None)
            message = getattr(err, "message", str(err) if err else "unknown error")
            self._response_done_event.set()
            logger.error("VoiceLive error: %s", message)
            await self.output_queue.put(AdditionalOutputs({"role": "assistant", "content": f"[error] {message}"}))

    async def _queue_output_audio(self, delta: Optional[bytes | str]) -> None:
        if not delta:
            return
        # The SDK deserializes the ``delta`` field (typed ``bytes``) into raw
        # PCM16 bytes already; only decode when a base64 string slips through.
        pcm_bytes = delta if isinstance(delta, (bytes, bytearray)) else base64.b64decode(delta)
        if not pcm_bytes:
            return
        pcm = np.frombuffer(pcm_bytes, dtype=np.int16)
        pcm = _resample_int16(pcm, VOICELIVE_SAMPLE_RATE, self.SAMPLE_RATE)
        await self.output_queue.put((self.SAMPLE_RATE, pcm.reshape(1, -1)))

    # ------------------------------------------------------------------
    # Greeting + response management
    # ------------------------------------------------------------------

    async def _send_startup_greeting_prompt(self) -> None:
        """Prompt the agent to open the conversation once the session is ready."""
        if self._startup_greeting_sent or self.connection is None:
            return
        self._startup_greeting_sent = True

        greeting_prompt = get_session_greeting_prompt().strip()
        if not greeting_prompt:
            return

        try:
            await self.connection.conversation.item.create(
                item=UserMessageItem(
                    content=[InputTextContentPart(text=greeting_prompt)],
                ),
            )
            self._mark_activity("startup_greeting_prompt")
            await self._safe_response_create()
            logger.info("Queued startup greeting prompt")
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to queue startup greeting prompt: %s", e)

    async def _safe_response_create(self, **kwargs: Any) -> None:
        """Enqueue a response.create() for the serial sender worker."""
        await self._pending_responses.put(kwargs)

    async def _response_sender_loop(self) -> None:
        """Serialize manual response.create() calls (greeting + post-tool)."""
        while not self._stopping:
            try:
                kwargs = await self._pending_responses.get()
            except asyncio.CancelledError:
                return

            if self.connection is None:
                continue

            try:
                await asyncio.wait_for(self._response_done_event.wait(), timeout=_RESPONSE_DONE_TIMEOUT)
            except asyncio.TimeoutError:
                self._response_done_event.set()

            if self.connection is None:
                continue

            try:
                response_params = kwargs.get("response") or ResponseCreateParams(
                    modalities=[Modality.TEXT, Modality.AUDIO]
                )
                self._response_done_event.clear()
                await self.connection.response.create(response=response_params)
            except Exception as e:  # noqa: BLE001
                logger.debug("response.create failed: %s", e)
                self._response_done_event.set()

    # ------------------------------------------------------------------
    # Tool-calling plumbing (client-side function calls, best-effort)
    # ------------------------------------------------------------------

    async def _handle_function_call(self, event: Any) -> None:
        self._mark_activity("tool_call_received")
        tool_name = getattr(event, "name", None)
        args_json_str = getattr(event, "arguments", None)
        call_id = str(getattr(event, "call_id", uuid.uuid4()))

        logger.info("Tool call received — tool=%r call_id=%s args=%s", tool_name, call_id, args_json_str)
        if not isinstance(tool_name, str) or not isinstance(args_json_str, str):
            logger.error("Invalid tool call: tool=%r args=%r", tool_name, args_json_str)
            return

        background_tool = await self.tool_manager.start_tool(
            call_id=call_id,
            tool_call_routine=ToolCallRoutine(
                tool_name=tool_name,
                args_json_str=args_json_str,
                deps=self.deps,
            ),
            is_idle_tool_call=False,
        )
        await self.output_queue.put(
            AdditionalOutputs(
                {
                    "role": "assistant",
                    "content": f"🛠️ Used tool {tool_name} with args {args_json_str}. "
                    f"Tool ID: {background_tool.tool_id}",
                },
            ),
        )

    async def _handle_tool_result(self, completed_tool: ToolNotification) -> None:
        """Return a completed tool's result to VoiceLive."""
        if completed_tool.error is not None:
            logger.error("Tool '%s' (id=%s) failed: %s", completed_tool.tool_name, completed_tool.id, completed_tool.error)
            tool_result: Any = {"error": completed_tool.error}
        elif completed_tool.result is not None:
            tool_result = completed_tool.result
        else:
            tool_result = {"error": "No result returned from tool execution"}

        await self.output_queue.put(
            AdditionalOutputs({"role": "assistant", "content": json.dumps(tool_result)}),
        )

        if self.connection is None:
            return

        if completed_tool.is_idle_tool_call or not isinstance(completed_tool.id, str):
            return

        try:
            await asyncio.wait_for(self._response_done_event.wait(), timeout=_RESPONSE_DONE_TIMEOUT)
        except asyncio.TimeoutError:
            self._response_done_event.set()

        if self.connection is None:
            return

        try:
            await self.connection.conversation.item.create(
                item={
                    "type": "function_call_output",
                    "call_id": completed_tool.id,
                    "output": json.dumps(tool_result),
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed to submit tool result: %s", e)
            return

        tool = core_tools.ALL_TOOLS.get(completed_tool.tool_name)
        if completed_tool.error is not None or tool is None or getattr(tool, "needs_response", True):
            await self._safe_response_create()

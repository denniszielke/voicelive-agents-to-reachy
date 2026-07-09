# reachy_conversation — Reachy Mini + VoiceLive + Orchestrator

A port of [`pollen-robotics/reachy_mini_conversation_app`](https://github.com/pollen-robotics/reachy_mini_conversation_app)
that runs the Reachy Mini conversation app against this repo's Foundry
**orchestrator-agent** through Azure **VoiceLive**, instead of the original
Hugging Face realtime WebSocket.

Two changes were made to the upstream app:

1. **Camera / image recognition removed.** The `camera` tool,
   `camera_frame_encoding` module and all camera references (profiles, prompts,
   handler image plumbing) are gone. Head motion still reacts to speech through
   the robot daemon's audio wobbler.
2. **Backend swapped to VoiceLive → orchestrator.** The Hugging Face
   `HuggingFaceRealtimeHandler` is replaced by
   [`VoiceLiveRealtimeHandler`](reachy_mini_conversation_app/voicelive_realtime.py),
   a `ConversationHandler` that connects with `azure.ai.voicelive.aio.connect`
   bound to a Foundry hosted agent (default `orchestrator-agent`). VoiceLive
   handles speech-to-text, turn detection and text-to-speech; the orchestrator
   drives the conversation and fans out to the specialist agents.

## How the audio is bridged

```
robot mic ─(native rate)─▶ VoiceLiveRealtimeHandler.receive()
                           └─ resample → 24 kHz PCM16 → input_audio_buffer.append
VoiceLive ─(24 kHz PCM16)─▶ response.audio.delta
                           └─ resample → 16 kHz → output_queue → robot speaker
```

- Speech I/O, VAD and TTS: **Azure VoiceLive** (`azure-ai-voicelive`).
- Conversation: **orchestrator-agent** (hosted agent binding).
- Robot control tool infrastructure (moves / dance / emotion, MCP, memory, web
  UI) is preserved. Client-side `response.function_call_arguments.done` events
  are still executed through the `BackgroundToolManager`; when bound to a hosted
  agent the conversational tools live in the agent.

## Configuration

Reads the same `./.env` as the other front-ends in this repo (written by
`azd up`). Relevant variables:

| Variable | Purpose | Default |
|----------|---------|---------|
| `AZURE_VOICELIVE_ENDPOINT` | VoiceLive endpoint (`https://<account>.services.ai.azure.com`) | *(required)* |
| `AZURE_AI_HOSTED_AGENT_NAME` | Hosted agent VoiceLive routes turns to | `orchestrator-agent` |
| `AZURE_AI_AGENT_NAME` | Fallback agent name if the hosted name is unset | `orchestrator-agent` |
| `AZURE_AI_PROJECT_NAME` | Foundry project containing the agent | *(optional)* |
| `AZURE_VOICELIVE_MODEL` | VoiceLive realtime model | `gpt-realtime` |
| `REALTIME_TRANSCRIPTION_LANGUAGE` | STT language | `en-US` |

Authentication uses `DefaultAzureCredential` (run `az login`).

## Running

The app requires the Reachy Mini robot SDK (`reachy-mini` and friends), so it
runs on a configured Reachy Mini. From the repo root:

```bash
pip install -r src/reachy_conversation/requirements.txt
PYTHONPATH=src/reachy_conversation python -m reachy_mini_conversation_app.main --ui
```

Add `--debug` for verbose logging. The web UI (personality / voice selection)
is served at http://localhost:7860/ when `--ui` is passed.

## Voices

Voice selection now exposes Azure neural HD voices
(`en-US-Ava:DragonHDLatestNeural`, …) instead of the Hugging Face speaker
catalog. See `VOICELIVE_AVAILABLE_VOICES` in
[`config.py`](reachy_mini_conversation_app/config.py).

## License

This directory is a derivative work of
[`pollen-robotics/reachy_mini_conversation_app`](https://github.com/pollen-robotics/reachy_mini_conversation_app),
licensed under the Apache License 2.0. The upstream license is retained in
[`LICENSE`](LICENSE). Modifications (camera removal, VoiceLive/orchestrator
backend) are made under the same license.

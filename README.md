# VoiceLive Agents

A VoiceLive front-end talking to Azure AI Foundry **hosted agents**. A single
**orchestrator-agent** fronts the conversation and routes each request to one or
both specialist agents:

- **orchestrator-agent** — routes questions and combines answers (Agent Framework)
- **weather-agent** — outside (outdoor) temperature (LangGraph)
- **homeassistant-agent** — inside (indoor) temperature (Agent Framework)

Three front-ends consume VoiceLive locally: `chat_client` (browser proxy),
`voice_dual_chat` (browser + WebRTC), and `voice-invocation` (terminal mic/speaker).
A fourth, `reachy_conversation`, runs the Reachy Mini robot conversation app
against the orchestrator through VoiceLive (camera/image recognition removed).

![workflows](./screenshot.png)

---

## Architecture

```mermaid
graph LR
  subgraph Local front-ends
    CC[chat_client]
    WR[voice_dual_chat]
    VI[voice-invocation]
  end

  CC & WR & VI -->|"speech + text (VoiceLive realtime)"| VLS[VoiceLive service]
  VLS -->|"hosted-agent binding (responses / invocations)"| ORC[orchestrator-agent]
  ORC -->|"invocations_ws (WebSocket)"| WA[weather-agent]
  ORC -->|"invocations_ws (WebSocket)"| HA[homeassistant-agent]
```

### How the pieces communicate

1. **Front-end ⇄ VoiceLive** — A front-end streams microphone audio to the
   VoiceLive realtime service and plays back the synthesized reply. VoiceLive
   handles speech-to-text, turn detection and text-to-speech.

2. **VoiceLive ⇄ orchestrator** — VoiceLive is bound to the `orchestrator-agent`
   as a Foundry **hosted agent** (Responses protocol, with A2A + Invocations
   enabled). Each completed user turn is routed to the orchestrator, whose reply
   is streamed back through VoiceLive and spoken aloud.

3. **Orchestrator ⇄ specialist agents** — The orchestrator decides which
   specialist(s) can answer and calls them over the Foundry **`invocations_ws`**
   protocol — a duplex WebSocket the platform relays untouched to the agent
   container. A small JSON wire format is used per turn:

   ```jsonc
   // orchestrator -> agent
   { "type": "message", "text": "What is the outside temperature?" }
   // agent -> orchestrator
   { "type": "done",  "text": "It's currently 12°C outside, measured 2 minutes ago." }
   { "type": "error", "message": "<detail>" }   // on failure
   ```

   The orchestrator can fan out to **both** specialists in a single turn (e.g.
   "how warm is it inside and outside?") and combine their answers before
   replying.

### Why two protocols

| Hop | Protocol | Why |
|-----|----------|-----|
| VoiceLive → orchestrator | `responses` (+ `invocations`) | VoiceLive binds to a hosted agent and drives its own response loop. |
| orchestrator → specialists | `invocations_ws` | A duplex WebSocket pass-through under full container control — the specialists define their own JSON wire format. Standard `responses` agents would not accept the streaming turn shape; `invocations_ws` keeps the contract in the container. |

> ℹ️ `invocations_ws` is a Foundry **public preview** feature that is currently
> available **only in North Central US**. Because the specialist agents use
> `invocations_ws`, the whole stack must be deployed to **North Central US**
> (`northcentralus`). The orchestrator's managed identity also needs Foundry
> data-plane RBAC to open the WebSocket to the specialist agents.

### Region requirements

| Agent | Protocol | Where it can run |
|-------|----------|------------------|
| orchestrator-agent | `responses` | Any [Hosted Agents region](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents#region-availability) |
| weather-agent | `invocations_ws` | **North Central US only** (preview) |
| homeassistant-agent | `invocations_ws` | **North Central US only** (preview) |

Since all three must live in the same Foundry project, deploy everything in
**North Central US** (`northcentralus`).

> Prefer a different region? Foundry Hosted Agents (Responses + Invocations) are
> also available in East US 2, Sweden Central, Canada Central/East, Southeast
> Asia, Poland Central, South Africa North, Korea Central, South India, Brazil
> South, West US, West US 3, Norway East, Japan East, France Central, Germany
> West Central, Switzerland North, Spain Central and Australia East — but
> **`invocations_ws` is not** (North Central US only). To run outside North
> Central US you must switch the specialist agents back to the `responses`
> protocol.

---

## 1. Prerequisites

- [Azure Developer CLI (`azd`)](https://aka.ms/azd) and [Azure CLI (`az`)](https://aka.ms/azcli)
- Python 3.13 and `pip`
- An Azure subscription with access to Azure AI Foundry + VoiceLive
- PortAudio (only for `voice-invocation`, which uses the mic/speaker):
  - macOS: `brew install portaudio`
  - Debian/Ubuntu: `sudo apt-get install -y portaudio19-dev`

```bash
az login
azd auth login
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

---

## 2. Provision the infrastructure

```bash
azd env set AZURE_ENV_NAME voicelive
# invocations_ws (used by the specialist agents) is preview and North Central
# US only, so the whole stack must be deployed there.
azd env set AZURE_LOCATION northcentralus

# Which hosted agent the VoiceLive front-ends bind to. Use the
# orchestrator-agent so a single conversation can reach both specialists;
# you can also bind directly to weather-agent or homeassistant-agent.
azd env set AZURE_AI_AGENT_NAME orchestrator-agent
azd env set AZURE_AI_HOSTED_AGENT_NAME orchestrator-agent

# Provision the AI Foundry project, model deployment and ACR. On success the
# postdeploy hook copies .azure/<env>/.env to ./.env for the local apps.
azd up
```

---

## 3. Deploy the agents

All three agents are built as container images in ACR and registered as Foundry
hosted agents. Each is registered with the protocol it speaks:

- **orchestrator-agent** — Responses + A2A + Invocations (so VoiceLive can bind
  to it and stream turns).
- **weather-agent** / **homeassistant-agent** — `invocations_ws` (the duplex
  WebSocket the orchestrator connects to).

```bash
# Build images + register all hosted agents in one step
python -m scripts.deploy_agents

# (optional) build the images only, without registering
python -m scripts.build_containers

# (optional) remove the hosted agents
python -m scripts.delete_agents
```

Run a quick local smoke test of a specialist agent's logic without VoiceLive:

```bash
python -m src.weather_agent.agent --query "What's the temperature outside?"
```

---

## 4. Test the VoiceLive apps locally

All three read configuration from `./.env`. Make sure you are `az login`'d
(the apps authenticate with `DefaultAzureCredential` / your `az` identity).

### 4a. chat_client — browser proxy (text + audio)

```bash
python src/chat_client/proxy.py
# then open http://localhost:8765/
```

Uses `AZURE_AI_PROJECT_ENDPOINT` and `AZURE_AI_AGENT_NAME`. Override per run:

```bash
python src/chat_client/proxy.py --agent homeassistant-agent
```

### 4b. voice_dual_chat — browser + WebRTC

`voice_dual_chat` opens one browser voice session with the hosted
`orchestrator-agent`. The orchestrator routes each turn to `weather-agent`,
`homeassistant-agent`, or both, then VoiceLive speaks the combined response.

```text
Browser microphone <-> VoiceLive <-> orchestrator-agent <-> specialist agents
       WebRTC          hosted agent       invocations_ws
```

The local FastAPI server authenticates with `DefaultAzureCredential` and relays
the WebRTC SDP exchange over WebSockets. Microphone and synthesized audio flow
directly between the browser and VoiceLive; Azure credentials are never sent to
the browser.

Before starting, deploy all three agents, run `az login`, install the root
requirements, and ensure `./.env` contains the outputs copied by `azd up`.

| Variable | Purpose |
|----------|---------|
| `AZURE_VOICELIVE_ENDPOINT` | VoiceLive resource endpoint; falls back to the project endpoint. |
| `AZURE_AI_PROJECT_ENDPOINT` | Foundry project endpoint and project-name fallback. |
| `AZURE_AI_PROJECT_NAME` | Foundry project containing the hosted orchestrator. |
| `AZURE_AI_AGENT_NAME` | Hosted agent name; defaults to `orchestrator-agent`. |
| `AZURE_VOICELIVE_MODEL` | Realtime model deployment; defaults to `gpt-realtime`. |
| `REALTIME_TRANSCRIPTION_LANGUAGE` | Input transcription locale; defaults to `en-US`. |
| `WEBRTCLIVE_PORT` | Local server port; defaults to `8090`. |

The app intentionally ignores `AZURE_AI_AGENT_NAMES`: it creates one voice
session and lets the orchestrator fan out to both specialists.

```bash
python src/voice_dual_chat/server.py
```

If port `8090` is already in use, stop the existing server with `Ctrl+C` in its
terminal or start this instance on another port:

```bash
WEBRTCLIVE_PORT=8091 python src/voice_dual_chat/server.py
```

Then open <http://localhost:8091> instead.

Open <http://localhost:8090>, select **Connect**, and allow microphone access.
The status changes to **connected** after VoiceLive completes the WebRTC
handshake. Try:

- "What is the temperature outside?"
- "How warm is it inside?"
- "Compare the indoor and outdoor temperatures."

The health endpoint at <http://localhost:8090/health> should report
`orchestrator-agent` and the expected Foundry project.

Troubleshooting:

- **HTTP 401/403** — run `az login` and verify access to both the VoiceLive
  resource and Foundry project.
- **Hosted agent not found** — verify `AZURE_AI_PROJECT_NAME` and confirm that
  `orchestrator-agent` deployed successfully.
- **No microphone** — use `http://localhost` or HTTPS and grant browser
  microphone permission.
- **Address already in use** — stop the process already listening on port
  `8090`, or set `WEBRTCLIVE_PORT` to an unused port as shown above.
- **Connected but no reply** — inspect the browser event log and server output,
  then verify both specialists are deployed with the `invocations_ws` protocol.

See the [component guide](src/voice_dual_chat/README.md) for the same workflow
close to the implementation.

### 4c. voice-invocation — terminal mic/speaker

Hosted-agent mode (VoiceLive routes to the Foundry agent):

```bash
python src/voice-invocation/client.py
# speak: "What's the temperature outside?"  — Ctrl+C to exit
```

Uses `AZURE_VOICELIVE_ENDPOINT`, `AZURE_AI_HOSTED_AGENT_NAME`,
`AZURE_AI_PROJECT_NAME`, `AZURE_VOICELIVE_MODEL`.

Custom-backend mode (VoiceLive does STT+TTS only; conversation comes from a
local agent container's `/invocations` endpoint):

```bash
# run an agent container locally on port 8088, then:
python src/voice-invocation/client.py \
  --invocation-url http://localhost:8088/invocations
```

### 4d. reachy_conversation — Reachy Mini robot

Runs the [Reachy Mini conversation app](src/reachy_conversation/README.md)
against the orchestrator through VoiceLive. This is a port of
`pollen-robotics/reachy_mini_conversation_app` with camera/image recognition
removed and the Hugging Face realtime WebSocket replaced by a VoiceLive handler
bound to the hosted agent. Requires the Reachy Mini robot SDK.

```bash
pip install -r src/reachy_conversation/requirements.txt
PYTHONPATH=src/reachy_conversation python -m reachy_mini_conversation_app.main --ui
```

Uses `AZURE_VOICELIVE_ENDPOINT`, `AZURE_AI_HOSTED_AGENT_NAME`,
`AZURE_AI_PROJECT_NAME`, `AZURE_VOICELIVE_MODEL`. See
[src/reachy_conversation/README.md](src/reachy_conversation/README.md) for
details.

---

## 5. Switching which agent answers

By default the front-ends bind to the **orchestrator-agent**, which routes each
question to `weather-agent`, `homeassistant-agent`, or both. To bind a front-end
directly to a single specialist instead (bypassing the orchestrator):

```bash
azd env set AZURE_AI_AGENT_NAME homeassistant-agent
azd env set AZURE_AI_HOSTED_AGENT_NAME homeassistant-agent
cp .azure/voicelive/.env ./.env
```

Or override per run with the CLI flags shown above.

---

## License

This project is licensed under the **Apache License 2.0** — see [`LICENSE`](LICENSE)
and [`NOTICE`](NOTICE). The `src/reachy_conversation/` directory is a derivative
of [`pollen-robotics/reachy_mini_conversation_app`](https://github.com/pollen-robotics/reachy_mini_conversation_app)
(also Apache-2.0). Portions of `src/voice-invocation/client.py` derive from
Azure SDK samples (© Microsoft Corporation, MIT).

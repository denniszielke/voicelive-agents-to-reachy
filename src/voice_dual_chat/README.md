# Orchestrator Voice Chat

This browser app opens a WebRTC voice session with Azure VoiceLive. VoiceLive
binds the session to the Foundry hosted `orchestrator-agent`; the orchestrator
then routes each spoken turn to `weather-agent`, `homeassistant-agent`, or both.

```text
Browser microphone <-> VoiceLive <-> orchestrator-agent <-> specialist agents
       WebRTC          hosted agent       invocations_ws
```

The local FastAPI server handles authentication and relays WebRTC signaling.
Microphone and synthesized audio flow directly between the browser and
VoiceLive. Credentials are never sent to the browser.

## Prerequisites

1. Provision the Azure resources and deploy all three hosted agents as described
   in the repository README.
2. Sign in with `az login`. The server uses `DefaultAzureCredential`.
3. Install the root dependencies with `pip install -r requirements.txt`.
4. Ensure the workspace-root `.env` contains the outputs from `azd up`.

Required configuration:

| Variable | Purpose |
|---|---|
| `AZURE_VOICELIVE_ENDPOINT` | VoiceLive resource endpoint. The project endpoint is used as a fallback. |
| `AZURE_AI_PROJECT_ENDPOINT` | Foundry project endpoint and project-name fallback. |
| `AZURE_AI_PROJECT_NAME` | Foundry project containing the hosted orchestrator. |
| `AZURE_AI_AGENT_NAME` | Hosted agent name; defaults to `orchestrator-agent`. |
| `AZURE_VOICELIVE_MODEL` | Realtime model deployment; defaults to `gpt-realtime`. |

Optional variables are `AZURE_AI_AGENT_VERSION`,
`REALTIME_TRANSCRIPTION_LANGUAGE` (default `en-US`), and `WEBRTCLIVE_PORT`
(default `8090`). This app intentionally ignores `AZURE_AI_AGENT_NAMES`: it
opens one voice session and lets the orchestrator fan out to both specialists.

## Run

```bash
python src/voice_dual_chat/server.py
```

If port `8090` is already occupied, stop the existing server with `Ctrl+C` or
choose another port:

```bash
WEBRTCLIVE_PORT=8091 python src/voice_dual_chat/server.py
```

Open <http://localhost:8091> when using the alternate port.

Open <http://localhost:8090>, select **Connect**, allow microphone
access, and speak after the status changes to connected. For example:

- "What is the temperature outside?"
- "How warm is it inside?"
- "Compare the indoor and outdoor temperatures."

Check readiness at <http://localhost:8090/health>. The response should report
`orchestrator-agent` and the expected Foundry project.

## Troubleshooting

- **HTTP 401/403:** run `az login` and verify your identity has access to the
  VoiceLive resource and Foundry project.
- **Hosted agent not found:** verify `AZURE_AI_PROJECT_NAME` and that
  `orchestrator-agent` was deployed successfully.
- **No microphone:** use `http://localhost` or HTTPS and grant browser
  microphone permission.
- **Address already in use:** stop the process listening on port `8090`, or set
  `WEBRTCLIVE_PORT` to an unused port as shown above.
- **Connected but no reply:** inspect the event log and server output, then
  verify both specialist agents are deployed with the `invocations_ws` protocol.
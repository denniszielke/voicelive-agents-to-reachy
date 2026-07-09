---
name: ops
description: >
  Operations runbook for the voicelive-agents-to-reachy repo. USE THIS SKILL when
  the user asks to provision, build, deploy, register, run, or clean up any part
  of the project — including infrastructure (azd), agent container images, the
  Foundry hosted agents (orchestrator / weather / homeassistant), and the local
  VoiceLive front-ends. Covers the full lifecycle: provision → build → deploy
  agents → run front-ends → clean up.
---

# Ops Runbook — voicelive-agents-to-reachy

A VoiceLive front-end talking to Azure AI Foundry **hosted agents**. A single
**orchestrator-agent** fronts the conversation and routes each turn to one or
both specialist agents:

- **orchestrator-agent** — routes questions and combines answers (Agent
  Framework). Hosted with the **`responses`** protocol so VoiceLive can bind to
  it; calls the specialists over **`invocations_ws`**.
- **weather-agent** — outside (outdoor) temperature (LangGraph). Hosted with the
  **`invocations_ws`** protocol.
- **homeassistant-agent** — inside (indoor) temperature (Agent Framework). Hosted
  with the **`invocations_ws`** protocol.

Communication:

```
front-end ──VoiceLive realtime──> VoiceLive ──(hosted agent)──> orchestrator-agent
                                                                  ├─ invocations_ws ─> weather-agent
                                                                  └─ invocations_ws ─> homeassistant-agent
```

All commands run from the **repo root**. Configuration comes from `./.env`, which
`azd up` writes automatically (via the `postdeploy` hook in `azure.yaml`). Use the
project venv:

```bash
source .venv/bin/activate   # or prefix commands with: .venv/bin/python
```

> **CRITICAL — region.** `invocations_ws` is a Foundry **public preview** feature
> currently available **only in North Central US**. Because the specialist agents
> use `invocations_ws`, the whole stack must be provisioned in **`northcentralus`**.
> The general Hosted Agents regions (East US 2, Sweden Central, etc.) do **not**
> support `invocations_ws` — deploying there fails with `bad_request: Unsupported
> region for Foundry Hosted Agents`.

---

## 0. Prerequisites

| Tool | Version | Install |
|---|---|---|
| Azure Developer CLI (`azd`) | latest | https://aka.ms/azd |
| Azure CLI (`az`) | ≥ 2.60 | https://aka.ms/azcli |
| Python | 3.13 + | |
| PortAudio | — | `voice-invocation` only — macOS: `brew install portaudio`; Debian/Ubuntu: `sudo apt-get install -y portaudio19-dev` |

```bash
az login
azd auth login
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

---

## 1. Provision Infrastructure

Creates the AI Foundry project, model deployment, ACR, Application Insights and
Log Analytics, and writes all outputs to `./.env` (the `postdeploy` hook copies
`.azure/<env>/.env` → `./.env`).

```bash
azd env set AZURE_ENV_NAME voicelive
# invocations_ws is preview + North Central US only, so provision there.
azd env set AZURE_LOCATION northcentralus

# Which hosted agent the VoiceLive front-ends bind to. Default to the
# orchestrator-agent so one conversation can reach both specialists.
azd env set AZURE_AI_AGENT_NAME orchestrator-agent
azd env set AZURE_AI_HOSTED_AGENT_NAME orchestrator-agent

azd up
```

Provision only / deploy only / tear down:
```bash
azd provision
azd deploy
azd down
```

Relevant provisioning parameters (see `infra/main.parameters.json`):
`AZURE_ENV_NAME`, `AZURE_LOCATION`, `AZURE_AI_DEPLOYMENTS_LOCATION` (defaults to
`AZURE_LOCATION`), `AZURE_PRINCIPAL_ID`, `AZURE_PRINCIPAL_TYPE` (User),
`ENABLE_HOSTED_AGENTS` (true), `SKIP_CONNECTION_CREATION` (false),
`SKIP_ROLE_ASSIGNMENTS` (false).

---

## 2. Build the Agent Images

Builds all three agent images in ACR (no local Docker) from the repo-root context.
Only builds — does not register. The registry is resolved from
`AZURE_CONTAINER_REGISTRY_ENDPOINT` / `AZURE_REGISTRY`, else discovered from the
resource group via `az acr list`. Both `:<timestamp>` and `:latest` tags are
pushed.

```bash
python -m scripts.build_containers            # auto timestamp tag + :latest
python -m scripts.build_containers 20260709   # explicit tag
```

The agents (name, Dockerfile, protocol) are declared in `scripts/_helpers.py`
`AGENTS`:

| Agent | Dockerfile | Protocol | Version |
|---|---|---|---|
| orchestrator-agent | `src/orchestrator_agent/Dockerfile` | `responses` | 2.0.0 |
| weather-agent | `src/weather_agent/Dockerfile` | `invocations_ws` | 1.0.0 |
| homeassistant-agent | `src/homeassistant_agent/Dockerfile` | `invocations_ws` | 1.0.0 |

---

## 3. Deploy (Build + Register) the Agents

Builds each image in ACR and registers a Foundry hosted-agent version with the
protocol it speaks (`scripts/deploy_agents.py`):

- **orchestrator-agent** → Responses + A2A + Invocations endpoint (VoiceLive binds
  here).
- **weather-agent / homeassistant-agent** → `invocations_ws` endpoint (the
  orchestrator connects here).

```bash
python -m scripts.deploy_agents
```

Each version is tagged `voiceLiveCompatible=true` and gets an agent card. The
shared container env (`AZURE_AI_PROJECT_ENDPOINT`,
`AZURE_AI_MODEL_DEPLOYMENT_NAME`, `AZURE_OPENAI_CHAT_DEPLOYMENT_NAME`,
`MODEL_DEPLOYMENT_NAME`) is injected from `./.env`.

Quick local smoke test of a specialist agent's logic (no VoiceLive):
```bash
python -m src.weather_agent.agent --query "What's the temperature outside?"
```

---

## 4. Agent-to-agent wiring (invocations_ws)

The orchestrator reaches each specialist over the Foundry `invocations_ws`
WebSocket. It builds the URL from `AZURE_AI_PROJECT_ENDPOINT`:

```
wss://{account}.services.ai.azure.com/api/projects/agents/endpoint/protocols/invocations_ws
    ?project_name={project}&agent_name={name}&agent_session_id={id}
    &foundry_features=HostedAgents=V1Preview
    &api-version=2025-11-15-preview
```

Wire format (JSON text frames, defined by the specialist containers):

```jsonc
// orchestrator -> agent
{ "type": "message", "text": "What is the outside temperature?" }
// agent -> orchestrator
{ "type": "done",  "text": "It's 12°C outside, measured 2 minutes ago." }
{ "type": "error", "message": "<detail>" }
```

> **GOTCHA — empty-body HTTP 400 on the WS upgrade.** The AzureML gateway rejects
> the `invocations_ws` upgrade unless an **`api-version`** query parameter is
> present (the protocol docs omit it). The orchestrator adds
> `api-version=2025-11-15-preview` by default; override with
> `AZURE_AI_INVOCATIONS_WS_API_VERSION`. A 400 with an empty body and no auth
> failure almost always means the `api-version` param is missing.
>
> **GOTCHA — 401/403 on upgrade.** The orchestrator's Entra Agent Identity needs
> Foundry data-plane RBAC (token scope `https://ai.azure.com/.default`) to open
> the WebSocket. 404 → wrong `agent_name`/`project_name`, missing preview flag,
> or unsupported region (must be North Central US).

---

## 5. Run the Front-ends Locally

All read configuration from `./.env` and authenticate with `DefaultAzureCredential`
(`az login`). By default they bind to `orchestrator-agent`.

### chat_client — browser proxy (text + audio)
```bash
python src/chat_client/proxy.py
# open http://localhost:8765/
python src/chat_client/proxy.py --agent homeassistant-agent   # bind a specialist
```
Uses `AZURE_AI_PROJECT_ENDPOINT`, `AZURE_AI_AGENT_NAME`.

### webrtclive — browser + WebRTC
```bash
python src/webrtclive/server.py
# open http://localhost:8090/   (health: http://localhost:8090/health)
```
Uses `AZURE_AI_PROJECT_ENDPOINT`, `AZURE_AI_PROJECT_NAME`, `AZURE_AI_AGENT_NAME`,
`AZURE_VOICELIVE_ENDPOINT`, `AZURE_VOICELIVE_MODEL`. Port: `WEBRTCLIVE_PORT`.

### webrtc_orchestrator — browser + WebRTC (multi-agent tabs)
```bash
python src/webrtc_orchestrator/server.py
# open http://localhost:8090/   (health: http://localhost:8090/health)
```
Signaling proxy between the browser and VoiceLive; VoiceLive binds to the selected
hosted agent. The browser tabs come from `/config` = `AZURE_AI_AGENT_NAMES`
(comma-separated; falls back to `AZURE_AI_AGENT_NAME`, then `orchestrator-agent`).
To show all three tabs:
```bash
azd env set AZURE_AI_AGENT_NAMES "orchestrator-agent,weather-agent,homeassistant-agent"
# (or edit ./.env directly; re-copy from .azure/<env>/.env after azd runs)
```

### voice-invocation — terminal mic/speaker
Hosted-agent mode (VoiceLive routes to the Foundry agent):
```bash
python src/voice-invocation/client.py
# speak: "What's the temperature outside?"  — Ctrl+C to exit
```
Uses `AZURE_VOICELIVE_ENDPOINT`, `AZURE_AI_HOSTED_AGENT_NAME`,
`AZURE_AI_PROJECT_NAME`, `AZURE_VOICELIVE_MODEL`.

Custom-backend mode (VoiceLive does STT+TTS only; conversation from a local
`/invocations` endpoint):
```bash
python src/voice-invocation/client.py --invocation-url http://localhost:8088/invocations
```

### voice_dual_chat — browser dual chat
```bash
python src/voice_dual_chat/server.py
```

---

## 6. Switching which agent answers

By default the front-ends bind to `orchestrator-agent`, which routes to
`weather-agent`, `homeassistant-agent`, or both. Bind directly to a specialist
instead:

```bash
azd env set AZURE_AI_AGENT_NAME homeassistant-agent
azd env set AZURE_AI_HOSTED_AGENT_NAME homeassistant-agent
cp .azure/voicelive/.env ./.env
```
Or override per run with the CLI flags shown above (e.g. `--agent`,
`--agent-name`, `?agent=` query param).

---

## 7. Cleanup

```bash
# delete all registered hosted agents (orchestrator, weather, homeassistant)
python -m scripts.delete_agents

# tear down all Azure resources
azd down
```

`delete_agents` iterates the `AGENTS` list in `scripts/_helpers.py` and tolerates
not-found / API drift.

---

## 8. Environment Variable Reference

Most variables are written to `./.env` by `azd up`.

| Variable | Source | Used by |
|---|---|---|
| `AZURE_ENV_NAME` | azd | provisioning, `postdeploy` env copy |
| `AZURE_LOCATION` | manual | provisioning (**must be `northcentralus`**) |
| `AZURE_RESOURCE_GROUP` | azd | registry discovery |
| `AZURE_CONTAINER_REGISTRY_ENDPOINT` / `AZURE_REGISTRY` | azd | build + deploy |
| `AZURE_AI_PROJECT_ENDPOINT` | azd | agents, orchestrator WS calls |
| `AZURE_AI_PROJECT_NAME` | azd / derived | front-ends |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | azd | agent container model (default `gpt-5.4-mini`) |
| `AZURE_OPENAI_CHAT_DEPLOYMENT_NAME` / `MODEL_DEPLOYMENT_NAME` | azd | mirror of the model deployment |
| `AZURE_AI_AGENT_NAME` | manual | front-end binding (default `orchestrator-agent`) |
| `AZURE_AI_AGENT_NAMES` | manual | webrtc_orchestrator tab list |
| `AZURE_AI_HOSTED_AGENT_NAME` | manual | voice-invocation hosted-agent binding |
| `AZURE_AI_INVOCATIONS_WS_API_VERSION` | manual | orchestrator WS `api-version` (default `2025-11-15-preview`) |
| `AZURE_VOICELIVE_ENDPOINT` | azd | webrtc / voice-invocation |
| `AZURE_VOICELIVE_MODEL` | azd | VoiceLive realtime model (default `gpt-realtime`) |
| `WEBRTCLIVE_PORT` | manual | webrtc server port (default `8090`) |
| `INVOCATION_URL` | manual | voice-invocation custom-backend mode |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | azd | telemetry |

---

## 9. Conventions

- Run all scripts from the **repo root** as modules: `python -m scripts.<name>`.
- Scripts read `./.env` via `python-dotenv` — `azd up` writes it, and the
  `postdeploy` hook copies `.azure/<env>/.env` → `./.env`.
- Image builds use `az acr build` (no local Docker). Both `:<timestamp>` and
  `:latest` tags are pushed on every build. Platform is `linux/amd64`.
- Protocols per agent live in `scripts/_helpers.py` `AGENTS`
  (`protocol` + `protocol_version`); `deploy_agents.py` maps them to the right
  endpoint config (`responses` vs `invocations_ws`).
- The orchestrator is the only VoiceLive-facing agent; it fans out to the
  specialists over `invocations_ws` (agent-to-agent). Specialists are **not**
  reachable via `responses`.
- **Region is North Central US** for the whole stack while `invocations_ws` is in
  preview.

---

## 10. Troubleshooting quick reference

| Symptom | Cause | Fix |
|---|---|---|
| `bad_request: Unsupported region for Foundry Hosted Agents` on deploy | Region not `northcentralus` (invocations_ws preview) | `azd env set AZURE_LOCATION northcentralus`, re-provision + redeploy |
| WS upgrade rejected, **HTTP 400, empty body** | Missing `api-version` on the invocations_ws URL | Ensure orchestrator adds `api-version` (default `2025-11-15-preview`); override via `AZURE_AI_INVOCATIONS_WS_API_VERSION` |
| WS upgrade **401 / 403** | Orchestrator agent identity lacks Foundry data-plane RBAC | Grant the agent identity access (token scope `https://ai.azure.com/.default`) |
| WS upgrade **404** | Wrong `agent_name`/`project_name`, missing preview flag, or wrong region | Verify names + `foundry_features=HostedAgents=V1Preview` + North Central US |
| `ParentResourceNotFound` on `az acr build` | ACR just recreated in a new region (ARM propagation) | Wait a moment and retry the build/deploy |
| `HTTP 403 … 'Agent365.Observability.OtelWrite'` in agent logs | Telemetry export permission not granted | Non-fatal — does not affect routing; grant the OtelWrite app role to the agent identity to silence it |

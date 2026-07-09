targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the environment that can be used as part of naming resource convention')
param environmentName string

@minLength(1)
@maxLength(90)
@description('Name of the resource group to use or create')
param resourceGroupName string = 'rg-${environmentName}'

@minLength(1)
@description('Primary location for all resources')
@allowed([
  'australiaeast'
  'brazilsouth'
  'canadacentral'
  'canadaeast'
  'eastus'
  'eastus2'
  'francecentral'
  'germanywestcentral'
  'italynorth'
  'japaneast'
  'koreacentral'
  'northcentralus'
  'norwayeast'
  'polandcentral'
  'southafricanorth'
  'southcentralus'
  'southeastasia'
  'southindia'
  'spaincentral'
  'swedencentral'
  'switzerlandnorth'
  'uaenorth'
  'uksouth'
  'westus'
  'westus2'
  'westus3'
])
param location string

@metadata({azd: {
  type: 'location'
  usageName: [
    'OpenAI.GlobalStandard.gpt-5.4-mini,10'
  ]}
})
param aiDeploymentsLocation string

@description('Id of the user or app to assign application roles')
param principalId string

@description('Principal type of user or app')
param principalType string

@description('Optional. Name of an existing AI Services account within the resource group.')
param aiFoundryResourceName string = ''

@description('Optional. Name of the AI Foundry project.')
param aiFoundryProjectName string = 'ai-voicelive-${environmentName}'

@description('Name of the chat model deployment to use')
param chatModelDeploymentName string = 'gpt-5.4-mini'

// VoiceLive realtime model. This is a VoiceLive platform-managed model
// (azure-realtime / gpt-realtime) — it is NOT deployed as an Azure OpenAI
// deployment; the voice front-ends just reference it by name. Set to
// 'azure-realtime' or 'gpt-realtime' (both pre-deployed for VoiceLive; see
// https://learn.microsoft.com/azure/ai-services/speech-service/regions?tabs=voice-live).
@description('Name of the VoiceLive realtime model (used by the voice front-ends)')
param realtimeModelDeploymentName string = 'gpt-realtime'

@description('OpenAI API version used by the hosted apps')
param openAiApiVersion string = '2024-05-01-preview'

@description('List of model deployments')
param aiProjectDeploymentsJson string = '[{"name":"gpt-5.4-mini","model":{"name":"gpt-5.4-mini","format":"OpenAI","version":"2026-03-17"},"sku":{"name":"GlobalStandard","capacity":1000}}]'

@description('List of connections')
param aiProjectConnectionsJson string = '[]'

@description('List of resources to create and connect to the AI project')
param aiProjectDependentResourcesJson string = '[]'

var aiProjectDeployments = json(aiProjectDeploymentsJson)
var aiProjectConnections = json(aiProjectConnectionsJson)
var aiProjectDependentResources = json(aiProjectDependentResourcesJson)

@description('Enable hosted agent deployment')
param enableHostedAgents bool

@description('Enable monitoring for the AI project')
param enableMonitoring bool = true

@description('Set to true to skip creating project connections that already exist (idempotent re-runs after partial failure)')
param skipConnectionCreation bool = false

@description('Set to true to skip creating role assignments that already exist (idempotent re-runs after partial failure)')
param skipRoleAssignments bool = false

var tags = {
  'azd-env-name': environmentName
}

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

// Add ACR if hosted agents are enabled
var hasAcr = contains(map(aiProjectDependentResources, r => r.resource), 'registry')
var dependentResources = (enableHostedAgents) && !hasAcr ? union(aiProjectDependentResources, [
  {
    resource: 'registry'
    connectionName: 'acr-connection'
  }
]) : aiProjectDependentResources

module aiProject 'core/ai/ai-project.bicep' = {
  scope: rg
  name: 'ai-project'
  params: {
    tags: tags
    location: aiDeploymentsLocation
    aiFoundryProjectName: aiFoundryProjectName
    principalId: principalId
    principalType: principalType
    existingAiAccountName: aiFoundryResourceName
    deployments: aiProjectDeployments
    connections: aiProjectConnections
    additionalDependentResources: dependentResources
    enableMonitoring: enableMonitoring
    enableHostedAgents: enableHostedAgents
    skipConnectionCreation: skipConnectionCreation
    skipRoleAssignments: skipRoleAssignments
  }
}

output AZURE_AI_PROJECT_ID string = aiProject.outputs.projectId
output AZURE_AI_PROJECT_NAME string = aiProject.outputs.projectName
output AZURE_AI_PROJECT_ENDPOINT string = aiProject.outputs.AZURE_AI_PROJECT_ENDPOINT
output AZURE_OPENAI_ENDPOINT string = aiProject.outputs.AZURE_OPENAI_ENDPOINT
// VoiceLive realtime endpoint is the account services.ai.azure.com host (no
// project path) — derive it from the project endpoint the front-ends use.
output AZURE_VOICELIVE_ENDPOINT string = split(aiProject.outputs.AZURE_AI_PROJECT_ENDPOINT, '/api/projects')[0]
output APPLICATIONINSIGHTS_CONNECTION_STRING string = aiProject.outputs.APPLICATIONINSIGHTS_CONNECTION_STRING
output AZURE_AI_MODEL_DEPLOYMENT_NAME string = chatModelDeploymentName
output AZURE_OPENAI_CHAT_DEPLOYMENT_NAME string = chatModelDeploymentName
output AZURE_VOICELIVE_MODEL string = realtimeModelDeploymentName
output OPENAI_API_VERSION string = openAiApiVersion

// ACR (for hosted agents)
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = aiProject.outputs.dependentResources.registry.loginServer
output AZURE_REGISTRY string = aiProject.outputs.dependentResources.registry.loginServer

output AZURE_RESOURCE_GROUP string = resourceGroupName
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId

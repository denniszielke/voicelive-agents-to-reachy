targetScope = 'resourceGroup'

@description('Tags that will be applied to all resources')
param tags object = {}

@description('Main location for the resources')
param location string

var resourceToken = uniqueString(subscription().id, resourceGroup().id, location)

@description('Name of the project')
param aiFoundryProjectName string

param deployments deploymentsType

@description('Id of the user or app to assign application roles')
param principalId string

@description('Principal type of user or app')
param principalType string

@description('Optional. Name of an existing AI Services account in the current resource group. If not provided, a new one will be created.')
param existingAiAccountName string = ''

@description('List of connections to provision')
param connections array = []

@description('Also provision dependent resources and connect to the project')
param additionalDependentResources dependentResourcesType

@description('Enable monitoring via appinsights and log analytics')
param enableMonitoring bool = true

@description('Enable hosted agent deployment')
param enableHostedAgents bool = false

@description('Set to true to skip creating connections that already exist (for idempotent re-runs)')
param skipConnectionCreation bool = false

@description('Set to true to skip creating role assignments that already exist (for idempotent re-runs)')
param skipRoleAssignments bool = false

// Load abbreviations
var abbrs = loadJsonContent('../../abbreviations.json')

// Determine which resources to create
var hasStorageConnectionInConfig = length(filter(additionalDependentResources, conn => conn.resource == 'storage')) > 0
var hasAcrConnection = length(filter(additionalDependentResources, conn => conn.resource == 'registry')) > 0
// Always deploy Storage (required by the Foundry Agents capability host)
var hasStorageConnection = true

var storageConnectionName = hasStorageConnectionInConfig ? filter(additionalDependentResources, conn => conn.resource == 'storage')[0].connectionName : 'storage-connection'
var acrConnectionName = hasAcrConnection ? filter(additionalDependentResources, conn => conn.resource == 'registry')[0].connectionName : ''

module logAnalytics '../monitor/loganalytics.bicep' = if (enableMonitoring) {
  name: 'logAnalytics'
  params: {
    location: location
    tags: tags
    name: 'logs-${resourceToken}'
  }
}

module applicationInsights '../monitor/applicationinsights.bicep' = if (enableMonitoring) {
  name: 'applicationInsights'
  params: {
    location: location
    tags: tags
    name: 'appi-${resourceToken}'
    logAnalyticsWorkspaceId: logAnalytics!.outputs.id
  }
}

resource aiAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: !empty(existingAiAccountName) ? existingAiAccountName : 'ai-account-${resourceToken}'
  location: location
  tags: tags
  sku: {
    name: 'S0'
  }
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: !empty(existingAiAccountName) ? existingAiAccountName : 'ai-account-${resourceToken}'
    networkAcls: {
      defaultAction: 'Allow'
      virtualNetworkRules: []
      ipRules: []
    }
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }

  @batchSize(1)
  resource seqDeployments 'deployments' = [
    for dep in (deployments??[]): {
      name: dep.name
      properties: {
        model: dep.model
      }
      sku: dep.sku
    }
  ]

  resource project 'projects' = {
    name: aiFoundryProjectName
    location: location
    identity: {
      type: 'SystemAssigned'
    }
    properties: {
      description: '${aiFoundryProjectName} Project'
      displayName: '${aiFoundryProjectName}Project'
    }
    dependsOn: [
      seqDeployments
    ]
  }

  resource aiFoundryAccountCapabilityHost 'capabilityHosts@2025-10-01-preview' = if (enableHostedAgents) {
    name: 'agents'
    properties: {
      capabilityHostKind: 'Agents'
      enablePublicHostingEnvironment: true
    }
  }
}

resource appInsightConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = if (enableMonitoring && !skipConnectionCreation) {
  parent: aiAccount::project
  name: 'appi-connection'
  properties: {
    category: 'AppInsights'
    target: applicationInsights!.outputs.id
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: applicationInsights!.outputs.connectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: applicationInsights!.outputs.id
    }
  }
}

module aiConnections './connection.bicep' = [for (connection, index) in connections: {
  name: 'connection-${connection.name}'
  params: {
    aiServicesAccountName: aiAccount.name
    aiProjectName: aiAccount::project.name
    connectionConfig: {
      name: connection.name
      category: connection.category
      target: connection.target
      authType: connection.authType
    }
    apiKey: ''
    skipCreation: skipConnectionCreation
  }
}]

resource localUserAiDeveloperRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!skipRoleAssignments) {
  scope: resourceGroup()
  name: guid(subscription().id, resourceGroup().id, principalId, '64702f94-c441-49e6-a78b-ef80e0188fee')
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', '64702f94-c441-49e6-a78b-ef80e0188fee')
  }
}

resource localUserCognitiveServicesUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!skipRoleAssignments) {
  scope: resourceGroup()
  name: guid(subscription().id, resourceGroup().id, principalId, 'a97b65f3-24c7-4388-baec-2e87135dc908')
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
  }
}

resource projectCognitiveServicesUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!skipRoleAssignments) {
  scope: aiAccount
  name: guid(subscription().id, resourceGroup().id, aiAccount::project.name, '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  properties: {
    principalId: aiAccount::project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  }
}

module storage '../storage/storage.bicep' = if (hasStorageConnection) {
  name: 'storage'
  params: {
    location: location
    tags: tags
    resourceName: 'st${resourceToken}'
    connectionName: storageConnectionName
    principalId: principalId
    principalType: principalType
    aiServicesAccountName: aiAccount.name
    aiProjectName: aiAccount::project.name
    skipConnectionCreation: skipConnectionCreation
    skipRoleAssignments: skipRoleAssignments
  }
}

module acr '../host/acr.bicep' = if (hasAcrConnection) {
  name: 'acr'
  params: {
    location: location
    tags: tags
    resourceName: '${abbrs.containerRegistryRegistries}${resourceToken}'
    connectionName: acrConnectionName
    principalId: principalId
    principalType: principalType
    aiServicesAccountName: aiAccount.name
    aiProjectName: aiAccount::project.name
    skipConnectionCreation: skipConnectionCreation
    skipRoleAssignments: skipRoleAssignments
  }
}

// Outputs
output AZURE_AI_PROJECT_ENDPOINT string = aiAccount::project.properties.endpoints['AI Foundry API']
output AZURE_OPENAI_ENDPOINT string = aiAccount.properties.endpoints['OpenAI Language Model Instance API']
output aiServicesEndpoint string = aiAccount.properties.endpoint
output accountId string = aiAccount.id
output projectId string = aiAccount::project.id
output aiServicesAccountName string = aiAccount.name
output aiServicesProjectName string = aiAccount::project.name
output aiServicesPrincipalId string = aiAccount.identity.principalId
output projectName string = aiAccount::project.name
output logAnalyticsWorkspaceName string = enableMonitoring ? logAnalytics!.outputs.name : ''
output APPLICATIONINSIGHTS_CONNECTION_STRING string = enableMonitoring ? applicationInsights!.outputs.connectionString : ''

output dependentResources object = {
  registry: {
    name: hasAcrConnection ? acr!.outputs.containerRegistryName : ''
    loginServer: hasAcrConnection ? acr!.outputs.containerRegistryLoginServer : ''
    connectionName: hasAcrConnection ? acr!.outputs.containerRegistryConnectionName : ''
  }
  storage: {
    accountName: hasStorageConnection ? storage!.outputs.storageAccountName : ''
    connectionName: hasStorageConnection ? storage!.outputs.storageConnectionName : ''
  }
}

type deploymentsType = {
  @description('Specify the name of cognitive service account deployment.')
  name: string

  @description('Required. Properties of Cognitive Services account deployment model.')
  model: {
    @description('Required. The name of Cognitive Services account deployment model.')
    name: string

    @description('Required. The format of Cognitive Services account deployment model.')
    format: string

    @description('Required. The version of Cognitive Services account deployment model.')
    version: string
  }

  @description('The resource model definition representing SKU.')
  sku: {
    @description('Required. The name of the resource model definition representing SKU.')
    name: string

    @description('The capacity of the resource model definition representing SKU.')
    capacity: int
  }
}[]?

type dependentResourcesType = {
  @description('The type of dependent resource to create')
  resource: 'storage' | 'registry' | 'azure_ai_search' | 'bing_grounding' | 'bing_custom_grounding'

  @description('The connection name for this resource')
  connectionName: string
}[]

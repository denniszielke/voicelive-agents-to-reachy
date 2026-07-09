targetScope = 'resourceGroup'

@description('The location used for all deployed resources')
param location string = resourceGroup().location

@description('Tags that will be applied to all resources')
param tags object = {}

@description('Resource name for the container registry')
param resourceName string

@description('Id of the user or app to assign application roles')
param principalId string

@description('Principal type of user or app')
param principalType string

@description('AI Services account name for the project parent')
param aiServicesAccountName string = ''

@description('AI project name for creating the connection')
param aiProjectName string = ''

@description('Name for the AI Foundry ACR connection')
param connectionName string = 'acr-connection'

@description('Set to true to skip creating the connection if it already exists (idempotent re-runs)')
param skipConnectionCreation bool = false

@description('Set to true to skip creating role assignments that already exist (idempotent re-runs)')
param skipRoleAssignments bool = false

resource aiAccount 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = if (!empty(aiServicesAccountName) && !empty(aiProjectName)) {
  name: aiServicesAccountName

  resource aiProject 'projects' existing = {
    name: aiProjectName
  }
}

module containerRegistry 'br/public:avm/res/container-registry/registry:0.1.1' = {
  name: 'registry'
  params: {
    name: resourceName
    location: location
    tags: tags
    publicNetworkAccess: 'Enabled'
    roleAssignments: skipRoleAssignments ? [] : [
      {
        principalId: principalId
        principalType: principalType
        roleDefinitionIdOrName: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
      }
      {
        principalId: aiAccount::aiProject.identity.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
      }
    ]
  }
}

module acrConnection '../ai/connection.bicep' = if (!empty(aiServicesAccountName) && !empty(aiProjectName)) {
  name: 'acr-connection-creation'
  params: {
    aiServicesAccountName: aiServicesAccountName
    aiProjectName: aiProjectName
    connectionConfig: {
      name: connectionName
      category: 'ContainerRegistry'
      target: containerRegistry.outputs.loginServer
      authType: 'ManagedIdentity'
      credentials: {
        clientId: aiAccount::aiProject.identity.principalId
        resourceId: containerRegistry.outputs.resourceId
      }
      isSharedToAll: true
      metadata: {
        ResourceId: containerRegistry.outputs.resourceId
      }
    }
    skipCreation: skipConnectionCreation
  }
}

output containerRegistryName string = containerRegistry.outputs.name
output containerRegistryLoginServer string = containerRegistry.outputs.loginServer
output containerRegistryResourceId string = containerRegistry.outputs.resourceId
output containerRegistryConnectionName string = acrConnection.outputs.connectionName

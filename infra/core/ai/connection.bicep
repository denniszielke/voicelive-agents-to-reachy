targetScope = 'resourceGroup'

@description('AI Services account name')
param aiServicesAccountName string

@description('AI project name')
param aiProjectName string

type ConnectionConfig = {
  @description('Name of the connection')
  name: string

  @description('Category of the connection')
  category: string

  @description('Target endpoint or URL for the connection')
  target: string

  @description('Authentication type')
  authType: 'AAD' | 'AccessKey' | 'AccountKey' | 'ApiKey' | 'CustomKeys' | 'ManagedIdentity' | 'None' | 'OAuth2' | 'PAT' | 'SAS' | 'ServicePrincipal' | 'UsernamePassword'

  @description('Whether the connection is shared to all users (optional, defaults to true)')
  isSharedToAll: bool?

  @description('Credentials for non-ApiKey authentication types (optional)')
  credentials: object?

  @description('Additional metadata for the connection (optional)')
  metadata: object?
}

@description('Connection configuration')
param connectionConfig ConnectionConfig

@secure()
@description('API key for ApiKey based connections (optional)')
param apiKey string = ''

@description('Set to true to reference an existing connection instead of creating a new one (idempotent re-runs)')
param skipCreation bool = false

resource aiAccount 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: aiServicesAccountName

  resource project 'projects' existing = {
    name: aiProjectName
  }
}

// Reference an already-existing connection without issuing a PUT
resource existingConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' existing = if (skipCreation) {
  parent: aiAccount::project
  name: connectionConfig.name
}

// Create the connection on first deploy
resource connection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = if (!skipCreation) {
  parent: aiAccount::project
  name: connectionConfig.name
  properties: {
    category: connectionConfig.category
    target: connectionConfig.target
    authType: connectionConfig.authType
    isSharedToAll: connectionConfig.?isSharedToAll ?? true
    credentials: connectionConfig.authType == 'ApiKey' ? {
      key: apiKey
    } : connectionConfig.?credentials
    metadata: connectionConfig.?metadata
  }
}

output connectionName string = skipCreation ? existingConnection.name : connection!.name
output connectionId string = skipCreation ? existingConnection.id : connection!.id

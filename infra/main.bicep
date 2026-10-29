// Capacity attribution service (spec 6): everything outside Fabric.
// Deploy with infra/deploy.sh. Resource group scope.

@description('Short prefix for resource names (lowercase letters/digits, max 8).')
@maxLength(8)
param prefix string = 'cuscope'

param location string = resourceGroup().location

@description('Comma-separated Fabric capacity IDs to harvest.')
param capacityIds string

@description('Workspace ID of the Capacity Metrics app (Pro workspace).')
param metricsWorkspaceId string

@description('Semantic model ID of the Capacity Metrics app.')
param metricsDatasetId string

@description('UTC_offset entered when the metrics app was installed (e.g. 5.5).')
param metricsUtcOffset string = '0'

@description('Installed metrics app version, recorded with every raw response.')
param metricsAppVersion string = 'unknown'

@description('Comma-separated workspace IDs to enrich (Spark sessions, jobs, warehouses).')
param enrichWorkspaceIds string = ''

@description('Load-gen workspace and semantic model IDs; leave empty to keep the loadgen timer idle.')
param loadgenWorkspaceId string = ''
param loadgenDatasetId string = ''

@description('Interactive retention for the custom tables, days.')
param retentionDays int = 90

var suffix = uniqueString(resourceGroup().id)
var names = {
  law: '${prefix}-law-${suffix}'
  appi: '${prefix}-appi-${suffix}'
  dcr: '${prefix}-dcr-${suffix}'
  plan: '${prefix}-plan-${suffix}'
  func: '${prefix}-func-${suffix}'
  funcStorage: take('${prefix}fn${suffix}', 24)
  rawStorage: take('${prefix}raw${suffix}', 24)
}

// ---------------------------------------------------------------------------------------
// Custom table schemas: single source for the tables and the DCR stream declarations.
// ---------------------------------------------------------------------------------------
var tables = {
  FabricOps_CL: [
    { name: 'TimeGenerated', type: 'datetime' }
    { name: 'capacity_id', type: 'string' }
    { name: 'class', type: 'string' }
    { name: 'operation_id', type: 'string' }
    { name: 'operation', type: 'string' }
    { name: 'user_name', type: 'string' }
    { name: 'status', type: 'string' }
    { name: 'billing_type', type: 'string' }
    { name: 'start_time', type: 'datetime' }
    { name: 'end_time', type: 'datetime' }
    { name: 'smoothing_start', type: 'datetime' }
    { name: 'smoothing_end', type: 'datetime' }
    { name: 'workspace_id', type: 'string' }
    { name: 'workspace_name', type: 'string' }
    { name: 'item_id', type: 'string' }
    { name: 'item_name', type: 'string' }
    { name: 'item_kind', type: 'string' }
    { name: 'duration_s', type: 'real' }
    { name: 'total_cu_s', type: 'real' }
    { name: 'timepoint_cu_s', type: 'real' }
    { name: 'throttling_s', type: 'real' }
    { name: 'sample_timepoint', type: 'datetime' }
    { name: 'extra', type: 'dynamic' }
  ]
  FabricTimepoints_CL: [
    { name: 'TimeGenerated', type: 'datetime' }
    { name: 'capacity_id', type: 'string' }
    { name: 'timepoint', type: 'datetime' }
    { name: 'metrics', type: 'dynamic' }
  ]
  FabricCapacities_CL: [
    { name: 'TimeGenerated', type: 'datetime' }
    { name: 'capacity_id', type: 'string' }
    { name: 'capacity_name', type: 'string' }
    { name: 'sku', type: 'string' }
    { name: 'region', type: 'string' }
    { name: 'attrs', type: 'dynamic' }
  ]
  FabricItems_CL: [
    { name: 'TimeGenerated', type: 'datetime' }
    { name: 'capacity_id', type: 'string' }
    { name: 'item_id', type: 'string' }
    { name: 'item_name', type: 'string' }
    { name: 'item_kind', type: 'string' }
    { name: 'workspace_id', type: 'string' }
    { name: 'workspace_name', type: 'string' }
    { name: 'attrs', type: 'dynamic' }
  ]
  FabricSparkSessions_CL: [
    { name: 'TimeGenerated', type: 'datetime' }
    { name: 'livy_id', type: 'string' }
    { name: 'workspace_id', type: 'string' }
    { name: 'item_id', type: 'string' }
    { name: 'item_name', type: 'string' }
    { name: 'item_type', type: 'string' }
    { name: 'capacity_id', type: 'string' }
    { name: 'operation_name', type: 'string' }
    { name: 'job_type', type: 'string' }
    { name: 'state', type: 'string' }
    { name: 'submitter_id', type: 'string' }
    { name: 'submitter_type', type: 'string' }
    { name: 'job_instance_id', type: 'string' }
    { name: 'spark_application_id', type: 'string' }
    { name: 'submitted_time', type: 'datetime' }
    { name: 'start_time', type: 'datetime' }
    { name: 'end_time', type: 'datetime' }
    { name: 'queued_s', type: 'real' }
    { name: 'running_s', type: 'real' }
    { name: 'total_s', type: 'real' }
    { name: 'driver_cores', type: 'int' }
    { name: 'executor_cores', type: 'int' }
    { name: 'num_executors', type: 'int' }
    { name: 'dynamic_allocation', type: 'boolean' }
    { name: 'dyn_max_executors', type: 'int' }
    { name: 'is_high_concurrency', type: 'boolean' }
  ]
  FabricJobInstances_CL: [
    { name: 'TimeGenerated', type: 'datetime' }
    { name: 'job_instance_id', type: 'string' }
    { name: 'workspace_id', type: 'string' }
    { name: 'item_id', type: 'string' }
    { name: 'item_name', type: 'string' }
    { name: 'item_type', type: 'string' }
    { name: 'job_type', type: 'string' }
    { name: 'invoke_type', type: 'string' }
    { name: 'status', type: 'string' }
    { name: 'root_activity_id', type: 'string' }
    { name: 'start_time', type: 'datetime' }
    { name: 'end_time', type: 'datetime' }
    { name: 'failure_reason', type: 'dynamic' }
  ]
  FabricWhRequests_CL: [
    { name: 'TimeGenerated', type: 'datetime' }
    { name: 'workspace_id', type: 'string' }
    { name: 'item_id', type: 'string' }
    { name: 'item_name', type: 'string' }
    { name: 'distributed_statement_id', type: 'string' }
    { name: 'session_id', type: 'long' }
    { name: 'login_name', type: 'string' }
    { name: 'start_time', type: 'datetime' }
    { name: 'end_time', type: 'datetime' }
    { name: 'status', type: 'string' }
    { name: 'allocated_cpu_time_ms', type: 'long' }
    { name: 'total_elapsed_time_ms', type: 'long' }
    { name: 'row_count', type: 'long' }
    { name: 'data_scanned_remote_storage_mb', type: 'real' }
    { name: 'label', type: 'string' }
    { name: 'command', type: 'string' }
  ]
  FabricHarvestLog_CL: [
    { name: 'TimeGenerated', type: 'datetime' }
    { name: 'job', type: 'string' }
    { name: 'capacity_id', type: 'string' }
    { name: 'table_name', type: 'string' }
    { name: 'sample_timepoint', type: 'datetime' }
    { name: 'status', type: 'string' }
    { name: 'rows', type: 'int' }
    { name: 'duration_ms', type: 'int' }
    { name: 'message', type: 'string' }
  ]
}

// Saved KQL functions (kql/*.kql). Order matters only for readability; dependsOn handles it.
var kqlFunctions = [
  { name: 'FabricOpsLatest', params: '' }
  { name: 'FabricSkuCu', params: 'cap:string' }
  { name: 'FabricExplainWindow', params: 'cap:string, t0:datetime, t1:datetime' }
  { name: 'FabricBackground24h', params: 'cap:string, t:datetime' }
  { name: 'FabricRootCauseByHour', params: 'cap:string, t0:datetime, t1:datetime' }
  { name: 'FabricReconstructionCheck', params: 'cap:string, t0:datetime, t1:datetime' }
  { name: 'FabricCoverage', params: 't0:datetime, t1:datetime' }
  { name: 'FabricDailyDelta', params: 'cap:string' }
  { name: 'FabricHarvestHealth', params: '' }
  { name: 'FabricSparkAttribution', params: 'cap:string, t0:datetime, t1:datetime' }
  { name: 'FabricWarehouseAttribution', params: 'cap:string, t0:datetime, t1:datetime' }
]
var kqlBodies = {
  FabricOpsLatest: loadTextContent('../kql/FabricOpsLatest.kql')
  FabricSkuCu: loadTextContent('../kql/FabricSkuCu.kql')
  FabricExplainWindow: loadTextContent('../kql/FabricExplainWindow.kql')
  FabricBackground24h: loadTextContent('../kql/FabricBackground24h.kql')
  FabricRootCauseByHour: loadTextContent('../kql/FabricRootCauseByHour.kql')
  FabricReconstructionCheck: loadTextContent('../kql/FabricReconstructionCheck.kql')
  FabricCoverage: loadTextContent('../kql/FabricCoverage.kql')
  FabricDailyDelta: loadTextContent('../kql/FabricDailyDelta.kql')
  FabricHarvestHealth: loadTextContent('../kql/FabricHarvestHealth.kql')
  FabricSparkAttribution: loadTextContent('../kql/FabricSparkAttribution.kql')
  FabricWarehouseAttribution: loadTextContent('../kql/FabricWarehouseAttribution.kql')
}

var roles = {
  storageBlobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  monitoringMetricsPublisher: '3913510d-42f4-4e42-8a64-420c390055eb'
}

// ---------------------------------------------------------------------------------------
// Log Analytics, custom tables, DCR, App Insights
// ---------------------------------------------------------------------------------------
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: names.law
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource customTables 'Microsoft.OperationalInsights/workspaces/tables@2022-10-01' = [for t in items(tables): {
  parent: law
  name: t.key
  properties: {
    retentionInDays: retentionDays
    schema: {
      name: t.key
      columns: map(t.value, c => { name: c.name, type: c.type == 'datetime' ? 'dateTime' : c.type })
    }
  }
}]

resource dcr 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: names.dcr
  location: location
  kind: 'Direct'
  properties: {
    streamDeclarations: toObject(items(tables), t => 'Custom-${t.key}', t => { columns: t.value })
    destinations: {
      logAnalytics: [ { name: 'law', workspaceResourceId: law.id } ]
    }
    dataFlows: map(items(tables), t => {
      streams: [ 'Custom-${t.key}' ]
      destinations: [ 'law' ]
      transformKql: 'source'
      outputStream: 'Custom-${t.key}'
    })
  }
  dependsOn: [ customTables ]
}

// One at a time, in list order, so functions exist before the functions that call them.
@batchSize(1)
resource savedFunctions 'Microsoft.OperationalInsights/workspaces/savedSearches@2020-08-01' = [for f in kqlFunctions: {
  parent: law
  name: toLower(f.name)
  properties: {
    category: 'cuscope'
    displayName: f.name
    functionAlias: f.name
    functionParameters: empty(f.params) ? null : f.params
    query: kqlBodies[f.name]
    version: 2
  }
  dependsOn: [ customTables ]
}]

// Validation UI. Generated by infra/workbook/build_workbook.py; edit the queries there.
resource workbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, 'cuscope-workbook')
  location: location
  kind: 'shared'
  properties: {
    displayName: 'cuscope - Fabric CU attribution'
    category: 'workbook'
    sourceId: law.id
    serializedData: replace(loadTextContent('workbook/cuscope.workbook.json'), '__WORKSPACE_RESOURCE_ID__', law.id)
  }
  dependsOn: [ savedFunctions ]
}

resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name: names.appi
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: law.id
  }
}

// ---------------------------------------------------------------------------------------
// Storage: Functions runtime/deployment account, and ADLS Gen2 raw + state account
// ---------------------------------------------------------------------------------------
resource funcStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: names.funcStorage
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
  }
}

resource funcBlob 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: funcStorage
  name: 'default'
}

resource deployContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: funcBlob
  name: 'deploy'
}

resource rawStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: names.rawStorage
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    isHnsEnabled: true
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
  }
}

resource rawBlob 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: rawStorage
  name: 'default'
}

resource rawContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: rawBlob
  name: 'raw'
}

resource stateContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: rawBlob
  name: 'state'
}

// Raw responses are for replay; move them to cool after 30 days.
resource rawLifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: rawStorage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'raw-to-cool'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: { blobTypes: [ 'blockBlob' ], prefixMatch: [ 'raw/' ] }
            actions: { baseBlob: { tierToCool: { daysAfterModificationGreaterThan: 30 } } }
          }
        }
      ]
    }
  }
}

// ---------------------------------------------------------------------------------------
// Function App (Flex Consumption, system-assigned identity, no secrets)
// ---------------------------------------------------------------------------------------
resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: names.plan
  location: location
  kind: 'functionapp'
  sku: { tier: 'FlexConsumption', name: 'FC1' }
  properties: { reserved: true }
}

resource func 'Microsoft.Web/sites@2024-04-01' = {
  name: names.func
  location: location
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${funcStorage.properties.primaryEndpoints.blob}deploy'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: { name: 'python', version: '3.11' }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: funcStorage.name }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appi.properties.ConnectionString }
        { name: 'CAPACITY_IDS', value: capacityIds }
        { name: 'METRICS_WORKSPACE_ID', value: metricsWorkspaceId }
        { name: 'METRICS_DATASET_ID', value: metricsDatasetId }
        { name: 'METRICS_UTC_OFFSET', value: metricsUtcOffset }
        { name: 'METRICS_APP_VERSION', value: metricsAppVersion }
        { name: 'ENRICH_WORKSPACE_IDS', value: enrichWorkspaceIds }
        { name: 'LOADGEN_WORKSPACE_ID', value: loadgenWorkspaceId }
        { name: 'LOADGEN_DATASET_ID', value: loadgenDatasetId }
        { name: 'RAW_STORAGE_URL', value: '${rawStorage.properties.primaryEndpoints.blob}raw' }
        { name: 'STATE_STORAGE_URL', value: '${rawStorage.properties.primaryEndpoints.blob}state' }
        { name: 'DCR_ENDPOINT', value: dcr.properties.endpoints.logsIngestion }
        { name: 'DCR_IMMUTABLE_ID', value: dcr.properties.immutableId }
      ]
    }
  }
}

// Runtime storage (host locks, timer state, deployment package).
resource funcStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: funcStorage
  name: guid(funcStorage.id, func.id, roles.storageBlobDataOwner)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataOwner)
    principalId: func.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource rawStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: rawStorage
  name: guid(rawStorage.id, func.id, roles.storageBlobDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor)
    principalId: func.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource dcrRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: dcr
  name: guid(dcr.id, func.id, roles.monitoringMetricsPublisher)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.monitoringMetricsPublisher)
    principalId: func.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output functionAppName string = func.name
@description('Add this identity to sg-fabric-monitor and to the Fabric workspaces (see README).')
output functionPrincipalId string = func.identity.principalId
output logAnalyticsWorkspaceName string = law.name
output logAnalyticsWorkspaceId string = law.properties.customerId
output dcrEndpoint string = dcr.properties.endpoints.logsIngestion
output dcrImmutableId string = dcr.properties.immutableId
output rawStorageAccount string = rawStorage.name

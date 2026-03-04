// =============================================================================
// Sonar Auto-Fix Platform — Azure Container Apps Job (KEDA/Service Bus trigger)
// =============================================================================
//
// Deploys:
//   1. Log Analytics Workspace (observability)
//   2. Container Apps Environment
//   3. Azure Container Apps Job
//      - TriggerType: Event (KEDA)
//      - Scale rule:  Azure Service Bus queue depth
//      - Secrets:     GitHub PAT, Service Bus connection string (from params)
//      - Replicas:    0 when idle, 1 per message, up to maxConcurrentJobs
//
// Usage:
//   az deployment group create \
//     --resource-group rg-sonarfix \
//     --template-file deploy/azure-container-apps-job.bicep \
//     --parameters \
//         containerImage='myregistry.azurecr.io/sonar-autofix:latest' \
//         serviceBusConnectionString='Endpoint=sb://...' \
//         serviceBusQueueName='sonar-autofix-jobs' \
//         githubPat='ghp_xxx'
// =============================================================================

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Short name prefix for all created resources.')
param resourcePrefix string = 'sonarfix'

@description('Full container image reference (registry/image:tag).')
param containerImage string

// Service Bus — existing or created in a companion template
@description('Azure Service Bus connection string (Listen policy).')
@secure()
param serviceBusConnectionString string

@description('Service Bus queue name that receives job messages.')
param serviceBusQueueName string = 'sonar-autofix-jobs'

// GitHub authentication
@description('GitHub Personal Access Token for repo push and PR creation.')
@secure()
param githubPat string

@description('GitHub token for the Copilot SDK (defaults to githubPat).')
@secure()
param githubToken string = ''

// Scaling
@description('Maximum concurrent job instances (one per Service Bus message).')
param maxConcurrentJobs int = 5

@description('CPU cores allocated to each job replica.')
param cpuCores string = '1.0'

@description('Memory allocated to each job replica (e.g. "2Gi").')
param memoryGi string = '2Gi'

// Log level
@description('Python log level for the job: DEBUG | INFO | WARNING | ERROR.')
param logLevel string = 'INFO'

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

var logWorkspaceName  = '${resourcePrefix}-logs'
var envName           = '${resourcePrefix}-env'
var jobName           = '${resourcePrefix}-job'

// ---------------------------------------------------------------------------
// Log Analytics Workspace
// ---------------------------------------------------------------------------

resource logWorkspace 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name:     logWorkspaceName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// ---------------------------------------------------------------------------
// Container Apps Environment
// ---------------------------------------------------------------------------

resource containerAppsEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name:     envName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logWorkspace.properties.customerId
        sharedKey:  logWorkspace.listKeys().primarySharedKey
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Container Apps Job
// ---------------------------------------------------------------------------

resource sonarfixJob 'Microsoft.App/jobs@2024-03-01' = {
  name:     jobName
  location: location
  properties: {
    environmentId: containerAppsEnv.id

    configuration: {
      // Event-based (KEDA) trigger — job starts when messages arrive
      triggerType: 'Event'

      // Each job instance runs for at most 60 minutes before being killed
      replicaTimeout: 3600

      // Retry a failed job instance once before marking it as failed
      replicaRetryLimit: 1

      eventTriggerConfig: {
        // Min replicas = 0  →  no idle cost
        // Max replicas = N  →  N jobs can run concurrently
        replicaCompletionCount: 1
        parallelism:            1
        scale: {
          minExecutions: 0
          maxExecutions: maxConcurrentJobs
          // Check the queue every 30 s; spin up 1 replica per message
          pollingInterval: 30
          rules: [
            {
              name: 'service-bus-trigger'
              type: 'azure-servicebus'
              metadata: {
                queueName:       serviceBusQueueName
                // 1:1 scaling — one job instance per pending message
                messageCount:    '1'
                activationMessageCount: '1'
              }
              auth: [
                {
                  secretRef:        'sb-connection-string'
                  triggerParameter: 'connection'
                }
              ]
            }
          ]
        }
      }

      // ---------------------------------------------------------------------------
      // Secrets — referenced by the container as environment variables
      // These are stored encrypted inside Container Apps; never in the image.
      // ---------------------------------------------------------------------------
      secrets: [
        {
          name:  'sb-connection-string'
          value: serviceBusConnectionString
        }
        {
          name:  'github-pat'
          value: githubPat
        }
        {
          name:  'github-token'
          value: empty(githubToken) ? githubPat : githubToken
        }
      ]

      registries: []   // add ACR credentials here if using a private registry
    }

    template: {
      containers: [
        {
          name:  'sonarfix'
          image: containerImage

          // ---------------------------------------------------------------
          // Resource limits — adjust based on repo size and Copilot latency
          // ---------------------------------------------------------------
          resources: {
            cpu:    json(cpuCores)
            memory: memoryGi
          }

          // ---------------------------------------------------------------
          // Environment variables
          // AZURE_SERVICEBUS_QUEUE_NAME is a plain config value.
          // Secrets are injected via secretRef.
          // ---------------------------------------------------------------
          env: [
            {
              name:  'AZURE_SERVICEBUS_CONNECTION_STRING'
              secretRef: 'sb-connection-string'
            }
            {
              name:  'AZURE_SERVICEBUS_QUEUE_NAME'
              value: serviceBusQueueName
            }
            {
              name:  'GITHUB_PAT'
              secretRef: 'github-pat'
            }
            {
              name:  'GITHUB_TOKEN'
              secretRef: 'github-token'
            }
            {
              name:  'LOG_LEVEL'
              value: logLevel
            }
            {
              // Clones land in the ephemeral /app/workdir inside the container
              name:  'SONARFIX_WORKDIR'
              value: '/app/workdir'
            }
          ]
        }
      ]

      // No init containers, volumes, or additional scale rules needed
      initContainers: []
      volumes: []
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

output jobResourceId       string = sonarfixJob.id
output jobName             string = sonarfixJob.name
output containerAppsEnvId string = containerAppsEnv.id
output logWorkspaceId      string = logWorkspace.id

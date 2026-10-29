@description('The location for the resource(s) to be deployed.')
param location string = resourceGroup().location

param memex_aca_outputs_azure_container_apps_environment_default_domain string

param memex_aca_outputs_azure_container_apps_environment_id string

@secure()
param memex_postgres_password_value string

@secure()
param key_protection_master_key_value string

param memex_aca_outputs_volumes_memex_portal_0 string

param memex_aca_outputs_volumes_memex_portal_1 string

param memex_aca_outputs_volumes_memex_portal_2 string

resource memex_portal 'Microsoft.App/containerApps@2025-07-01' = {
  name: 'memex-portal'
  location: location
  properties: {
    configuration: {
      secrets: [
        {
          name: 'connectionstrings--memex'
          value: 'Host=memex-postgres;Port=5432;Username=postgres;Password=${memex_postgres_password_value};Database=memex'
        }
        {
          name: 'memex-password'
          value: memex_postgres_password_value
        }
        {
          name: 'memex-uri'
          value: 'postgresql://postgres:${uriComponent(memex_postgres_password_value)}@memex-postgres:5432/memex'
        }
        {
          name: 'connectionstrings--orleans'
          value: 'Host=memex-postgres;Port=5432;Username=postgres;Password=${memex_postgres_password_value};Database=orleans'
        }
        {
          name: 'orleans-password'
          value: memex_postgres_password_value
        }
        {
          name: 'orleans-uri'
          value: 'postgresql://postgres:${uriComponent(memex_postgres_password_value)}@memex-postgres:5432/orleans'
        }
        {
          name: 'ai--keyprotection--masterkey'
          value: key_protection_master_key_value
        }
      ]
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8080
        transport: 'http'
      }
    }
    environmentId: memex_aca_outputs_azure_container_apps_environment_id
    template: {
      containers: [
        {
          image: 'cr.meshweaver.cloud/memex-portal-ai:latest'
          name: 'memex-portal'
          env: [
            {
              name: 'ConnectionStrings__memex'
              secretRef: 'connectionstrings--memex'
            }
            {
              name: 'MEMEX_HOST'
              value: 'memex-postgres'
            }
            {
              name: 'MEMEX_PORT'
              value: '5432'
            }
            {
              name: 'MEMEX_USERNAME'
              value: 'postgres'
            }
            {
              name: 'MEMEX_PASSWORD'
              secretRef: 'memex-password'
            }
            {
              name: 'MEMEX_URI'
              secretRef: 'memex-uri'
            }
            {
              name: 'MEMEX_JDBCCONNECTIONSTRING'
              value: 'jdbc:postgresql://memex-postgres:5432/memex'
            }
            {
              name: 'MEMEX_DATABASENAME'
              value: 'memex'
            }
            {
              name: 'ConnectionStrings__orleans'
              secretRef: 'connectionstrings--orleans'
            }
            {
              name: 'ORLEANS_HOST'
              value: 'memex-postgres'
            }
            {
              name: 'ORLEANS_PORT'
              value: '5432'
            }
            {
              name: 'ORLEANS_USERNAME'
              value: 'postgres'
            }
            {
              name: 'ORLEANS_PASSWORD'
              secretRef: 'orleans-password'
            }
            {
              name: 'ORLEANS_URI'
              secretRef: 'orleans-uri'
            }
            {
              name: 'ORLEANS_JDBCCONNECTIONSTRING'
              value: 'jdbc:postgresql://memex-postgres:5432/orleans'
            }
            {
              name: 'ORLEANS_DATABASENAME'
              value: 'orleans'
            }
            {
              name: 'Deployment__Record'
              value: '{"namespace":"memex","imageRepository":"cr.meshweaver.cloud/memex-portal-ai","isPluginRegistry":false,"pluginRepos":[],"preInstall":[],"defaultGrants":[],"requiredModules":[],"requiredModulesAuthoritative":false,"helmRelease":"memex","replicas":1,"gracePeriodDays":0,"inClusterPostgres":false,"volumes":[{"name":"data","claimName":"memex-data","mountPath":"/data","create":false,"readOnly":false},{"name":"content","claimName":"memex-content","mountPath":"/mnt/content","create":false,"readOnly":false},{"name":"users","claimName":"memex-users","mountPath":"/mnt/users","create":false,"readOnly":false}],"keyVaultSecretClasses":[],"vaultValuesKeys":[],"inlineEnv":[],"gates":[],"secretMounts":[],"signIn":{"enableDevLogin":false},"orleansClustering":"AdoNet","requiredModuleSlots":{},"webhookInboxTargets":[],"webhookInbox":[],"extraPortalConfig":{},"features":{}}'
            }
            {
              name: 'ASPNETCORE_HTTP_PORTS'
              value: '8080'
            }
            {
              name: 'Authentication__EnableDevLogin'
              value: 'false'
            }
            {
              name: 'Authentication__Provider'
              value: 'Custom'
            }
            {
              name: 'Deployment__Backend'
              value: 'Filesystem'
            }
            {
              name: 'Deployment__DataRoot'
              value: '/data'
            }
            {
              name: 'Deployment__Orleans__Clustering'
              value: 'AdoNet'
            }
            {
              name: 'Graph__Storage__BasePath'
              value: '/data/graph'
            }
            {
              name: 'Graph__Storage__Type'
              value: 'PostgreSql'
            }
            {
              name: 'Modules__Root'
              value: '/data'
            }
            {
              name: 'SelfUpdate__MinRollInterval'
              value: '01:00:00'
            }
            {
              name: 'SelfUpdate__Registry'
              value: 'cr.meshweaver.cloud'
            }
            {
              name: 'Storage__BasePath'
              value: '/mnt/content'
            }
            {
              name: 'Storage__Name'
              value: 'content'
            }
            {
              name: 'Storage__SourceType'
              value: 'FileSystem'
            }
            {
              name: 'Mcp__BaseUrl'
              value: 'https://memex-portal.${memex_aca_outputs_azure_container_apps_environment_default_domain}'
            }
            {
              name: 'Features__Orleans__Clustering'
              value: 'AdoNet'
            }
            {
              name: 'Ai__KeyProtection__MasterKey'
              secretRef: 'ai--keyprotection--masterkey'
            }
          ]
          volumeMounts: [
            {
              volumeName: 'v0'
              mountPath: '/data'
            }
            {
              volumeName: 'v1'
              mountPath: '/mnt/content'
            }
            {
              volumeName: 'v2'
              mountPath: '/mnt/users'
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
      }
      volumes: [
        {
          name: 'v0'
          storageType: 'AzureFile'
          storageName: memex_aca_outputs_volumes_memex_portal_0
        }
        {
          name: 'v1'
          storageType: 'AzureFile'
          storageName: memex_aca_outputs_volumes_memex_portal_1
        }
        {
          name: 'v2'
          storageType: 'AzureFile'
          storageName: memex_aca_outputs_volumes_memex_portal_2
        }
      ]
    }
  }
}
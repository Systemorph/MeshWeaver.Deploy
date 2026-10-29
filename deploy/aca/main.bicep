targetScope = 'subscription'

param resourceGroupName string

param location string

param principalId string

resource rg 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: resourceGroupName
  location: location
}

module memex_aca_acr 'memex-aca-acr/memex-aca-acr.bicep' = {
  name: 'memex-aca-acr'
  scope: rg
  params: {
    location: location
  }
}

module memex_aca 'memex-aca/memex-aca.bicep' = {
  name: 'memex-aca'
  scope: rg
  params: {
    location: location
    memex_aca_acr_outputs_name: memex_aca_acr.outputs.name
    userPrincipalId: principalId
  }
}

output memex_aca_AZURE_CONTAINER_APPS_ENVIRONMENT_DEFAULT_DOMAIN string = memex_aca.outputs.AZURE_CONTAINER_APPS_ENVIRONMENT_DEFAULT_DOMAIN

output memex_aca_AZURE_CONTAINER_APPS_ENVIRONMENT_ID string = memex_aca.outputs.AZURE_CONTAINER_APPS_ENVIRONMENT_ID

output memex_aca_volumes_memex_postgres_0 string = memex_aca.outputs.volumes_memex_postgres_0

output memex_aca_volumes_memex_portal_0 string = memex_aca.outputs.volumes_memex_portal_0

output memex_aca_volumes_memex_portal_1 string = memex_aca.outputs.volumes_memex_portal_1

output memex_aca_volumes_memex_portal_2 string = memex_aca.outputs.volumes_memex_portal_2

@secure()
param memex_postgres_password_value string

@secure()
param key_protection_master_key_value string

module memex_migration 'memex-migration/memex-migration.bicep' = {
  name: 'memex-migration'
  scope: rg
  params: {
    location: location
    memex_aca_outputs_azure_container_apps_environment_default_domain: memex_aca.outputs.AZURE_CONTAINER_APPS_ENVIRONMENT_DEFAULT_DOMAIN
    memex_aca_outputs_azure_container_apps_environment_id: memex_aca.outputs.AZURE_CONTAINER_APPS_ENVIRONMENT_ID
    memex_postgres_password_value: memex_postgres_password_value
  }
}

module memex_portal 'memex-portal/memex-portal.bicep' = {
  name: 'memex-portal'
  scope: rg
  params: {
    location: location
    memex_aca_outputs_azure_container_apps_environment_default_domain: memex_aca.outputs.AZURE_CONTAINER_APPS_ENVIRONMENT_DEFAULT_DOMAIN
    memex_aca_outputs_azure_container_apps_environment_id: memex_aca.outputs.AZURE_CONTAINER_APPS_ENVIRONMENT_ID
    memex_postgres_password_value: memex_postgres_password_value
    key_protection_master_key_value: key_protection_master_key_value
    memex_aca_outputs_volumes_memex_portal_0: memex_aca.outputs.volumes_memex_portal_0
    memex_aca_outputs_volumes_memex_portal_1: memex_aca.outputs.volumes_memex_portal_1
    memex_aca_outputs_volumes_memex_portal_2: memex_aca.outputs.volumes_memex_portal_2
  }
}

module memex_postgres 'memex-postgres/memex-postgres.bicep' = {
  name: 'memex-postgres'
  scope: rg
  params: {
    location: location
    memex_aca_outputs_azure_container_apps_environment_default_domain: memex_aca.outputs.AZURE_CONTAINER_APPS_ENVIRONMENT_DEFAULT_DOMAIN
    memex_aca_outputs_azure_container_apps_environment_id: memex_aca.outputs.AZURE_CONTAINER_APPS_ENVIRONMENT_ID
    memex_postgres_password_value: memex_postgres_password_value
    memex_aca_outputs_volumes_memex_postgres_0: memex_aca.outputs.volumes_memex_postgres_0
  }
}

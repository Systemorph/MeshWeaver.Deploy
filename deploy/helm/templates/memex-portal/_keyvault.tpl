{{- /*
The portal's Key Vault secret CLASSES, resolved: one entry per SecretProviderClass the chart owns,
with every name already defaulted, so the four templates that need them (secretproviderclass.yaml
and deployment.yaml's envFrom / volumeMounts / volumes) can never disagree about a name.

🚨 WHY A LIST AND NOT ONE BLOCK. `keyVaultSecrets` held exactly ONE class, and a namespace that
runs two could therefore not be described by any file. `memex` runs two — the hand-made `memex-kv`
(13 keys: the connection string, the GitHub App key, the AI keys) and the chart-owned
`memex-portal-keyvault` (the plugin-registry token) — so with one slot the record had to choose,
and whichever it named, a re-render DROPPED the other. That is not hypothetical: on 2026-09-06 the
`memex` record described `memex-kv` while the deployed overlay described `memex-portal-keyvault`,
so a re-render from the record would have removed the registry-token class that had just fixed the
poll (MeshWeaver#3201 / Memex#164) AND rendered a class for `memex-kv` whose derived vault object
does not exist — a mount failure, i.e. a portal that cannot start.

ORDER IS PRECEDENCE. Kubernetes keeps the LAST `envFrom` entry on a key clash, so the classes are
emitted in list order, after the chart's own ConfigMap and Secret. `keyVaultSecrets` (the singular,
legacy block) comes first so an existing environment renders byte-identically; entries in
`keyVaultSecretClasses` follow, in order.

NAMES ARE REQUIRED ON THE PLURAL FORM. The singular block may lean on the chart's defaults
(`memex-portal-keyvault` / `kv-secrets` / `/mnt/secrets-store`); a SECOND class that did the same
would collide with the first on all three — two volumes of one name is an invalid pod spec and two
SecretProviderClasses syncing into one Secret race each other — so `name`, `volumeName` and
`mountPath` must be stated. `syncedSecret` still defaults to the class's own name.

The vault coordinates fall back to the singular block's, because a namespace's classes normally
read the SAME vault with the SAME add-on identity; stating them per class stays possible.

NAMES ONLY. No value of any secret is in values, in this helper, or in any render.
*/ -}}
{{- define "memex.keyVaultClasses" -}}
{{- $root := . -}}
{{- $fallback := (.Values.keyVaultSecrets | default dict) -}}
{{- $classes := list -}}
{{- with .Values.keyVaultSecrets -}}
{{- if .secrets -}}
{{- $name := .name | default "memex-portal-keyvault" -}}
{{- $classes = append $classes (dict
      "name" $name
      "syncedSecret" (.syncedSecret | default $name)
      "volumeName" (.volumeName | default "kv-secrets")
      "mountPath" (.mountPath | default "/mnt/secrets-store")
      "vaultName" (required "keyVaultSecrets.vaultName is required when keyVaultSecrets.secrets is non-empty" .vaultName)
      "tenantId" (required "keyVaultSecrets.tenantId is required when keyVaultSecrets.secrets is non-empty" .tenantId)
      "identityClientId" (required "keyVaultSecrets.identityClientId is required when keyVaultSecrets.secrets is non-empty (the Key Vault Secrets Provider add-on's user-assigned identity client id)" .identityClientId)
      "secrets" .secrets) -}}
{{- end -}}
{{- end -}}
{{- range $i, $class := ($root.Values.keyVaultSecretClasses | default list) -}}
{{- if $class.secrets -}}
{{- $name := required (printf "keyVaultSecretClasses[%d].name is required — a second SecretProviderClass may not default to the first's name" $i) $class.name -}}
{{- $classes = append $classes (dict
      "name" $name
      "syncedSecret" ($class.syncedSecret | default $name)
      "volumeName" (required (printf "keyVaultSecretClasses[%d].volumeName is required — two pod volumes of one name is an invalid spec" $i) $class.volumeName)
      "mountPath" (required (printf "keyVaultSecretClasses[%d].mountPath is required — the mount is what makes the CSI driver sync and rotate the Secret" $i) $class.mountPath)
      "vaultName" (required (printf "keyVaultSecretClasses[%d].vaultName is required (or keyVaultSecrets.vaultName)" $i) ($class.vaultName | default $fallback.vaultName))
      "tenantId" (required (printf "keyVaultSecretClasses[%d].tenantId is required (or keyVaultSecrets.tenantId)" $i) ($class.tenantId | default $fallback.tenantId))
      "identityClientId" (required (printf "keyVaultSecretClasses[%d].identityClientId is required (or keyVaultSecrets.identityClientId)" $i) ($class.identityClientId | default $fallback.identityClientId))
      "secrets" $class.secrets) -}}
{{- end -}}
{{- end -}}
{{- toYaml $classes -}}
{{- end -}}

{{- /*
The Ingress TLS certificate, synced from Key Vault by the CSI driver (ingress.tlsFromKeyVault,
MeshWeaver.Deploy#130). Renders the resolved block as YAML, or NOTHING when the ingress is off or
`ingress.tlsFromKeyVault.certificate` is empty — so every estate that has not opted in (the app
routing add-on's `tls-cert-keyvault-uri` estates, cert-manager estates) renders byte-identically.

🚨 WHY IT EXISTS. Only the AKS app routing add-on turns a vault certificate into the Secret the
Ingress names. An estate that serves its Ingress objects with its own ingress-nginx controllers
has no component that creates that Secret, so it was created by hand from the vault certificate —
and again after every namespace reset and at every certificate renewal. The CSI driver can do it:
a SecretProviderClass whose `secretObjects` entry has type `kubernetes.io/tls` makes the driver
create the Secret while a pod mounts the volume, and keep it current within its rotation interval.

ONE OBJECT, TWO KEYS. A vault CERTIFICATE read with `objectType: secret` is its PEM bundle — the
private key and the certificate CHAIN (leaf and intermediates). For a `kubernetes.io/tls` Secret the
driver splits that one object into `tls.key` and `tls.crt`, so both keys name the same object, and
the served chain carries the intermediate certificate.

The vault coordinates fall back to `keyVaultSecrets`, like every other class. The SPC name, volume
and mount path are the chart's own and differ from every `keyVaultSecrets` default, so the two
classes can never collide on a pod volume or a synced Secret.

NAMES ONLY: `certificate` is the vault certificate object's NAME. No value is in values or render.
*/ -}}
{{- define "memex.ingressTlsFromKeyVault" -}}
{{- $t := ((.Values.ingress).tlsFromKeyVault | default dict) -}}
{{- if and (.Values.ingress).enabled $t.certificate -}}
{{- $fallback := (.Values.keyVaultSecrets | default dict) -}}
{{- $secret := required "ingress.tlsFromKeyVault.certificate is set, so ingress.tlsSecret must name the Secret the CSI driver creates" (.Values.ingress).tlsSecret -}}
{{- toYaml (dict
      "certificate" $t.certificate
      "secretName" $secret
      "name" ($t.name | default "memex-ingress-tls-keyvault")
      "volumeName" ($t.volumeName | default "ingress-tls")
      "mountPath" ($t.mountPath | default "/mnt/ingress-tls")
      "vaultName" (required "ingress.tlsFromKeyVault.vaultName is required (or keyVaultSecrets.vaultName)" ($t.vaultName | default $fallback.vaultName))
      "tenantId" (required "ingress.tlsFromKeyVault.tenantId is required (or keyVaultSecrets.tenantId)" ($t.tenantId | default $fallback.tenantId))
      "identityClientId" (required "ingress.tlsFromKeyVault.identityClientId is required (or keyVaultSecrets.identityClientId) — the Key Vault Secrets Provider add-on's identity, which needs secret GET on the vault" ($t.identityClientId | default $fallback.identityClientId))) -}}
{{- end -}}
{{- end -}}

{{- /*
A container registry for the fleet's images and bundles — a SEPARATE service beside the portal,
built from two off-the-shelf images and zero registry code of ours:

  * distribution   the CNCF reference registry (ghcr.io/distribution/distribution), storage driver
                   `azure` authenticating as the namespace's workload identity, token auth pointed
                   at the auth server below;
  * docker_auth    cesanta/docker_auth — the token server. ONE static publisher account (its
                   bcrypt hash is the only credential in values, and a hash is committable) plus an
                   `ext_auth` hook that validates ANY other password as a MeshWeaver instance key
                   by presenting it to the portal's key→token exchange (`registry.validationUrl`).

Both share the one host through ONE Ingress: `/auth` → docker_auth, everything else → distribution.
Design: Doc/Architecture/ContainerRegistryInMemex → "The registry as a separate service".

🚨 THIS HELPER IS THE GATE. Every template under templates/registry/ reads its values through
`memex.registry` and nothing else, so a half-declared block fails `helm template` HERE, naming the
key — never a registry that renders, deploys and then cannot start (a distribution pod whose token
bundle is an empty mount reports "no such file" from inside the container, not from helm), or
worse, one that starts with anonymous push because the ACL referenced a publisher with no hash.

Rendered ONLY when `registry.enabled`, so every environment that has not opted in renders
byte-identically — the chart's neutrality rule.

NAMES ONLY. No secret VALUE is in values or in any render: the token certificate and key, the
HTTP secret and the notification bearer are Key Vault OBJECT NAMES the CSI driver fetches at pod
start. The one exception is deliberate and safe: `publisher.passwordBcrypt` is a bcrypt HASH.
*/ -}}
{{- define "memex.registry" -}}
{{- $r := (.Values.registry | default dict) -}}
{{- $kv := ($r.keyVault | default dict) -}}
{{- $fallback := (.Values.keyVaultSecrets | default dict) -}}
{{- $storage := ($r.storage | default dict) -}}
{{- $publisher := ($r.publisher | default dict) -}}
{{- $notifications := ($r.notifications | default dict) -}}
{{- $ingress := ($r.ingress | default dict) -}}
{{- $resources := ($r.resources | default dict) -}}
{{- $host := required "registry.host is required when registry.enabled — the public hostname the registry answers on (e.g. registry.example.com)" $r.host -}}
{{- $image := required "registry.image is required when registry.enabled — the distribution image, pinned by digest (ghcr.io/distribution/distribution:3.1.1@sha256:…)" $r.image -}}
{{- $authImage := required "registry.authImage is required when registry.enabled — the cesanta/docker_auth image, pinned by digest" $r.authImage -}}
{{- $accountName := required "registry.storage.accountName is required when registry.enabled — the Azure Storage account holding the registry's blobs" $storage.accountName -}}
{{- $vaultName := required "registry.keyVault.name is required when registry.enabled — the Key Vault holding the token certificate, key and HTTP secret" $kv.name -}}
{{- $tenantId := required "registry.keyVault.tenantId is required when registry.enabled (or keyVaultSecrets.tenantId, which it falls back to)" ($kv.tenantId | default $fallback.tenantId) -}}
{{- $identityClientId := required "registry.keyVault.identityClientId is required when registry.enabled (or keyVaultSecrets.identityClientId) — the Key Vault Secrets Provider add-on's user-assigned identity client id" ($kv.identityClientId | default $fallback.identityClientId) -}}
{{- $certObject := required "registry.keyVault.certObject is required when registry.enabled — the vault secret holding the token-signing certificate (PEM)" $kv.certObject -}}
{{- $keyObject := required "registry.keyVault.keyObject is required when registry.enabled — the vault secret holding the token-signing private key (PEM)" $kv.keyObject -}}
{{- $httpSecretObject := required "registry.keyVault.httpSecretObject is required when registry.enabled — the vault secret holding distribution's http.secret; without a shared one every replica signs upload state differently and chunked pushes fail across pods" $kv.httpSecretObject -}}
{{- $passwordBcrypt := required "registry.publisher.passwordBcrypt is required when registry.enabled — the publisher account's bcrypt HASH (htpasswd -nB <user>); without it the ACL would name an account nobody can authenticate as" $publisher.passwordBcrypt -}}
{{- /* 🚨 NO DEFAULT. docker_auth hands every non-publisher login's password — an instance key — to
       this URL. A default naming one installation's portal would send every OTHER installation's
       instance keys there, and authenticate them against the wrong authority. */ -}}
{{- $validationUrl := required "registry.validationUrl is required when registry.enabled — the portal key→token exchange that validates every instance key presented at this registry (e.g. https://portal.example.com/api/instances/token). There is no default: docker_auth sends each login's instance key to this URL, so it must name YOUR installation's validator" (($r.validationUrl | default "") | toString | trim) -}}
{{- /* The registry's OWN workload identity (registry.workloadIdentity.clientId). Set → the chart creates
       a dedicated ServiceAccount for the registry pods (templates/registry/serviceaccount.yaml); it
       may never be the portal's `memex-portal-sa`, which the portal chart owns and federates to the
       portal identity — annotating that one would move the PORTAL onto the registry's identity. */ -}}
{{- $wi := ($r.workloadIdentity | default dict) -}}
{{- $wiClientId := (($wi.clientId | default "") | toString | trim) -}}
{{- $serviceAccount := (($r.serviceAccount | default "") | toString | trim) -}}
{{- if not $serviceAccount -}}
{{- $serviceAccount = ternary "memex-registry-sa" "memex-portal-sa" (ne $wiClientId "") -}}
{{- end -}}
{{- if $wiClientId -}}
{{- if not (regexMatch "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$" $wiClientId) -}}
{{- fail (printf "registry.workloadIdentity.clientId %q is not a client id (a GUID) — it is the registry identity's client id, annotated on the registry's own ServiceAccount" $wiClientId) -}}
{{- end -}}
{{- if eq $serviceAccount "memex-portal-sa" -}}
{{- fail "registry.workloadIdentity.clientId is set but registry.serviceAccount is the portal's memex-portal-sa — a dedicated identity needs a dedicated account (leave serviceAccount blank for memex-registry-sa); annotating the portal's account would move the portal onto the registry's identity" -}}
{{- end -}}
{{- end -}}
{{- /* THE MAINTENANCE WINDOW (MeshWeaver.Plugins#4370, slice 3; deploy/aks/REGISTRY-MAINTENANCE.md).
       `readOnly` makes every registry pod refuse pushes and deletes (distribution's
       storage.maintenance.readonly) — pulls keep working. `garbageCollect` names ONE run: it renders
       the blob garbage-collect Job, and only inside a read-only window, because a collect beside a
       read-write pod can delete the blobs of an upload in progress. Nothing here deletes untagged
       manifests (no --delete-untagged): the fleet cleanup deletes manifests explicitly, by digest,
       and the untagged per-platform manifests beneath a kept index must survive. Both are applied by
       the maintenance run that holds the registry-maintenance lease (`hosting-registry-maintenance`),
       never as a standing value of a record. */ -}}
{{- $maintenance := ($r.maintenance | default dict) -}}
{{- $readOnly := $maintenance.readOnly | default false -}}
{{- if not (kindIs "bool" $readOnly) -}}
{{- fail (printf "registry.maintenance.readOnly must be true or false, not %q" (toString $readOnly)) -}}
{{- end -}}
{{- $garbageCollect := (($maintenance.garbageCollect | default "") | toString | trim) -}}
{{- if $garbageCollect -}}
{{- if not $readOnly -}}
{{- fail "registry.maintenance.garbageCollect needs registry.maintenance.readOnly: true — a garbage-collect beside a read-write registry pod can delete the blobs of an upload in progress" -}}
{{- end -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$" $garbageCollect) -}}
{{- fail (printf "registry.maintenance.garbageCollect %q is not a run id — 1 to 40 lowercase letters, digits or '-', the suffix of the Job memex-registry-gc-<run>" $garbageCollect) -}}
{{- end -}}
{{- end -}}
{{- $gcDeadlineRaw := (($maintenance.gcDeadlineSeconds | default 1500) | toString | trim) -}}
{{- if not (regexMatch "^[0-9]+$" $gcDeadlineRaw) -}}
{{- fail (printf "registry.maintenance.gcDeadlineSeconds %q is not a whole number of seconds" $gcDeadlineRaw) -}}
{{- end -}}
{{- $gcDeadline := int $gcDeadlineRaw -}}
{{- if or (lt $gcDeadline 60) (gt $gcDeadline 1500) -}}
{{- fail (printf "registry.maintenance.gcDeadlineSeconds %d is outside 60..1500 — the collect runs inside one maintenance job, cut at 45 minutes, which must also hold both pod-by-pod verifications and the restore" $gcDeadline) -}}
{{- end -}}
{{- $notificationUrl := ($notifications.url | default "") -}}
{{- $notificationSecretObject := ($kv.notificationSecretObject | default "") -}}
{{- if and $notificationUrl (not $notificationSecretObject) -}}
{{- fail "registry.keyVault.notificationSecretObject is required when registry.notifications.url is set — the vault secret holding the Authorization header value the registry sends with every event" -}}
{{- end -}}
{{- $out := dict
      "host" $host
      "image" $image
      "authImage" $authImage
      "issuer" ($r.issuer | default "memex-registry")
      "replicas" (int ($r.replicas | default 1))
      "serviceAccount" $serviceAccount
      "workloadIdentityClientId" $wiClientId
      "accountName" $accountName
      "container" ($storage.container | default "registry")
      "vaultName" $vaultName
      "tenantId" $tenantId
      "identityClientId" $identityClientId
      "certObject" $certObject
      "keyObject" $keyObject
      "httpSecretObject" $httpSecretObject
      "notificationSecretObject" $notificationSecretObject
      "publisherUsername" ($publisher.username | default "publisher")
      "publisherPasswordBcrypt" $passwordBcrypt
      "validationUrl" $validationUrl
      "notificationUrl" $notificationUrl
      "readOnly" $readOnly
      "garbageCollect" $garbageCollect
      "gcDeadlineSeconds" $gcDeadline
      "ingressClassName" ($ingress.className | default "nginx")
      "clusterIssuer" ($ingress.clusterIssuer | default "")
      "tlsSecret" ($ingress.tlsSecret | default "memex-registry-tls")
      "registryResources" ($resources.registry | default (dict "requests" (dict "cpu" "50m" "memory" "128Mi") "limits" (dict "memory" "512Mi")))
      "authResources" ($resources.auth | default (dict "requests" (dict "cpu" "20m" "memory" "32Mi") "limits" (dict "memory" "128Mi")))
-}}
{{- toYaml $out -}}
{{- end -}}

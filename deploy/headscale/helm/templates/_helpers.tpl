{{/* Chart name, overridable via .Values.nameOverride. */}}
{{- define "headscale.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* <release>-<chart>, or the release name if it already contains the chart name. */}}
{{- define "headscale.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "headscale.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "headscale.selectorLabels" -}}
app.kubernetes.io/name: {{ include "headscale.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "headscale.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "headscale.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{/* The mount the OIDC client secret is read from (the CSI volume of secretproviderclass.yaml). */}}
{{- define "headscale.oidcSecretDir" -}}/var/run/secrets/headscale-oidc{{- end -}}

{{/*
  Every refusal in one place, included by every template, so a values file the chart cannot run
  fails the render naming what is missing — never a pod that crash-loops on a half-written config.
*/}}
{{- define "headscale.validate" -}}
{{- if not .Values.image.repository }}
{{- fail "image.repository is required (<registry>/headscale)" }}
{{- end }}
{{- if not .Values.image.tag }}
{{- fail "image.tag is required — name the immutable <headscale version>-<short sha> tag, never the moving line" }}
{{- end }}
{{- /* Only the SHA-suffixed tag CI publishes is immutable; the line (`0.29.7`), `latest` or any other moving
       tag under IfNotPresent lets a reschedule run a different build than the one the approval bound. */}}
{{- if not (regexMatch "^[0-9]+\\.[0-9]+\\.[0-9]+-[0-9a-f]{7,40}$" (toString .Values.image.tag)) }}
{{- fail (printf "image.tag %q is not an immutable <headscale version>-<short sha> tag (e.g. 0.29.7-0a81895)" (toString .Values.image.tag)) }}
{{- end }}
{{- if not .Values.host }}
{{- fail "host is required — the public name clients reach (server_url is https://<host>)" }}
{{- end }}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)+$" (toString .Values.host)) }}
{{- fail (printf "host %q is not a lower-case DNS name" (toString .Values.host)) }}
{{- end }}
{{- if not .Values.dns.baseDomain }}
{{- fail "dns.baseDomain is required — the MagicDNS suffix machines get (e.g. tailnet.internal)" }}
{{- end }}
{{- /* Headscale refuses a base domain that contains the server's own name; say it at render time. */}}
{{- if or (eq (toString .Values.host) (toString .Values.dns.baseDomain)) (hasSuffix (printf ".%s" .Values.dns.baseDomain) (toString .Values.host)) }}
{{- fail (printf "dns.baseDomain %q contains host %q — MagicDNS would shadow the control server's own name" .Values.dns.baseDomain .Values.host) }}
{{- end }}
{{- if .Values.ingress.enabled }}
{{- if not .Values.ingress.className }}
{{- fail "ingress.className is required when ingress.enabled" }}
{{- end }}
{{- if not (or .Values.ingress.clusterIssuer .Values.ingress.tlsSecret) }}
{{- fail "ingress needs a TLS source: ingress.clusterIssuer (cert-manager issues the Secret) or ingress.tlsSecret (pre-created). A Tailscale client refuses a control server without a valid certificate." }}
{{- end }}
{{- end }}
{{- if .Values.oidc.enabled }}
{{- range $k := list "issuer" "clientId" }}
{{- if not (index $.Values.oidc $k) }}
{{- fail (printf "oidc.%s is required when oidc.enabled" $k) }}
{{- end }}
{{- end }}
{{- /* An OIDC login with no restriction admits EVERY account the issuer knows — for a multi-tenant
       issuer, anyone with a Microsoft account. Refuse it: name the domains, groups or users admitted. */}}
{{- if not (or .Values.oidc.allowedDomains .Values.oidc.allowedGroups .Values.oidc.allowedUsers) }}
{{- fail "oidc needs at least one of allowedDomains, allowedGroups or allowedUsers — otherwise every account the issuer knows may join the network" }}
{{- end }}
{{- $kv := .Values.oidc.clientSecret.keyVault }}
{{- if not (or .Values.oidc.clientSecret.existingSecret $kv.vaultName) }}
{{- fail "oidc.clientSecret needs a source: existingSecret (a Secret with key `client_secret`) or keyVault (vaultName, tenantId, identityClientId, objectName). The value itself is never a chart value." }}
{{- end }}
{{- if and .Values.oidc.clientSecret.existingSecret $kv.vaultName }}
{{- fail "oidc.clientSecret: existingSecret and keyVault are exclusive — name one source" }}
{{- end }}
{{- if $kv.vaultName }}
{{- range $k := list "tenantId" "identityClientId" "objectName" }}
{{- if not (index $kv $k) }}
{{- fail (printf "oidc.clientSecret.keyVault.%s is required when keyVault.vaultName is set" $k) }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- if .Values.preauthKeys.enabled }}
{{- if not .Values.preauthKeys.image.repository }}
{{- fail "preauthKeys.image.repository is required when preauthKeys.enabled (<registry>/headscale-keys)" }}
{{- end }}
{{- $kt := toString (default .Values.image.tag .Values.preauthKeys.image.tag) }}
{{- if not (regexMatch "^[0-9]+\\.[0-9]+\\.[0-9]+-[0-9a-f]{7,40}$" $kt) }}
{{- fail (printf "preauthKeys.image.tag %q is not an immutable <headscale version>-<short sha> tag" $kt) }}
{{- end }}
{{- if not .Values.preauthKeys.keys }}
{{- fail "preauthKeys.keys is empty — declare at least one key, or leave preauthKeys.enabled false" }}
{{- end }}
{{- $seen := dict }}
{{- range $k := .Values.preauthKeys.keys }}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$" (toString $k.name)) }}
{{- fail (printf "preauthKeys.keys: name %q is not a short lower-case DNS label" (toString $k.name)) }}
{{- end }}
{{- if hasKey $seen (toString $k.name) }}
{{- fail (printf "preauthKeys.keys: %q is declared twice" (toString $k.name)) }}
{{- end }}
{{- $_ := set $seen (toString $k.name) true }}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9._]*[a-z0-9])?$" (toString $k.user)) }}
{{- fail (printf "preauthKeys.keys[%s]: user %q is required and must be a plain lower-case name" $k.name (toString $k.user)) }}
{{- end }}
{{- $valid := int (default 720 $k.validHours) }}
{{- $renew := int (default 168 $k.renewHours) }}
{{- if or (lt $valid 1) (lt $renew 1) (ge $renew $valid) }}
{{- fail (printf "preauthKeys.keys[%s]: renewHours (%d) must be at least 1 and below validHours (%d)" $k.name $renew $valid) }}
{{- end }}
{{- end }}
{{- end }}
{{- end -}}

{{/* The Secret one declared key is kept in. */}}
{{- define "headscale.preauthSecret" -}}
{{- printf "%s-preauth-%s" (include "headscale.fullname" .root) .name | trunc 63 | trimSuffix "-" -}}
{{- end -}}


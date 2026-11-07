{{/* Chart name, overridable via .Values.nameOverride. */}}
{{- define "tailnet-bridge.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* <release>-<chart>, or the release name if it already contains the chart name. */}}
{{- define "tailnet-bridge.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "tailnet-bridge.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "tailnet-bridge.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tailnet-bridge.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "tailnet-bridge.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "tailnet-bridge.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{- define "tailnet-bridge.dnsName" -}}^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)+${{- end -}}

{{/*
  Every refusal in one place, included by every template, so a values file the chart cannot run fails
  the render naming what is missing — never a pod that crash-loops, never a public name in front of
  nothing.
*/}}
{{- define "tailnet-bridge.validate" -}}
{{- if not .Values.image.repository }}
{{- fail "image.repository is required (<registry>/tailnet-bridge)" }}
{{- end }}
{{- if not (regexMatch "^[0-9]+\\.[0-9]+\\.[0-9]+-[0-9a-f]{7,40}$" (toString .Values.image.tag)) }}
{{- fail (printf "image.tag %q is not an immutable <version>-<short sha> tag (e.g. 1.104.4-0a81895)" (toString .Values.image.tag)) }}
{{- end }}
{{- $dns := include "tailnet-bridge.dnsName" . }}
{{- if not (regexMatch $dns (toString .Values.host)) }}
{{- fail (printf "host %q is required and must be a lower-case DNS name — the public name" (toString .Values.host)) }}
{{- end }}
{{- if not .Values.ingress.className }}
{{- fail "ingress.className is required (the cluster's HTTPS ingress class)" }}
{{- end }}
{{- if not (or .Values.ingress.clusterIssuer .Values.ingress.tlsSecret) }}
{{- fail "ingress needs a TLS source: ingress.clusterIssuer or ingress.tlsSecret — a public name is served over HTTPS only" }}
{{- end }}
{{- if not (hasPrefix "https://" (toString .Values.network.loginServer)) }}
{{- fail (printf "network.loginServer %q must be the control server's https URL" (toString .Values.network.loginServer)) }}
{{- end }}
{{- if not .Values.network.authKeySecret }}
{{- fail "network.authKeySecret is required — the Secret (key `authkey`) the control server's keys sidecar fills" }}
{{- end }}
{{- if not (regexMatch $dns (toString .Values.target.name)) }}
{{- fail (printf "target.name %q is required and must be the machine's lower-case MagicDNS name" (toString .Values.target.name)) }}
{{- end }}
{{- if eq (toString .Values.target.name) (toString .Values.host) }}
{{- fail "target.name is the public host — the bridge would forward to itself" }}
{{- end }}
{{- $port := int .Values.target.port }}
{{- if or (lt $port 1) (gt $port 65535) }}
{{- fail (printf "target.port %v is not a port" .Values.target.port) }}
{{- end }}
{{- if and .Values.target.upstreamHost (not (regexMatch $dns (toString .Values.target.upstreamHost))) }}
{{- fail (printf "target.upstreamHost %q is not a lower-case DNS name" (toString .Values.target.upstreamHost)) }}
{{- end }}
{{- if and .Values.networkPolicy.enabled (not .Values.networkPolicy.ingressNamespaces) }}
{{- fail "networkPolicy.ingressNamespaces is required — name the ingress controller's namespace(s); without it any pod in the cluster could reach the proxy and be trusted as a forwarding proxy" }}
{{- end }}
{{- range $p := .Values.blockedPaths }}
{{- if not (regexMatch "^/[A-Za-z0-9/_.*-]*$" (toString $p)) }}
{{- fail (printf "blockedPaths entry %q is not a path matcher" (toString $p)) }}
{{- end }}
{{- end }}
{{- end -}}

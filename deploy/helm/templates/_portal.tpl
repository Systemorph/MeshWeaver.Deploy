{{- /*
`portal.enabled` — whether this release renders the PORTAL (MeshWeaver.Plugins Hosting/BuildQueue,
phase 2). Absent or true (the default) renders exactly what the chart always rendered, so every
existing estate renders byte-identically. False makes the release a BUILD-HOST-ONLY release: one
platform version's build host, installed as its OWN Helm release beside the control instance's
portal, rendering templates/build-host/* and nothing else. Every other template opens with this
helper's answer, so nothing portal-shaped (the portal, its migration Job, its database, the
registry, the dashboard, the required-secrets refusal) renders in such a release.

🚨 THIS HELPER IS THE GATE for the portal-less shape. It answers "true" or "false", and with
`portal.enabled: false` it refuses, naming the key:
  * `buildHosts.enabled` not true — a portal-less release that renders no host is a typo;
  * `postgres.enabled` true — a build-host release runs no database (the default is true, so a
    build-host release says `postgres.enabled: false` out loud);
  * `ingress.enabled` true — a build host serves nothing.
`buildHosts` keeps every refusal of its own (templates/build-host/_build-hosts.tpl).
*/ -}}
{{- define "memex.portal.enabled" -}}
{{- $p := (.Values.portal | default dict) -}}
{{- if not (hasKey $p "enabled") -}}
true
{{- else if not (kindIs "bool" $p.enabled) -}}
{{- fail (printf "portal.enabled %v must be true or false" $p.enabled) -}}
{{- else if $p.enabled -}}
true
{{- else -}}
{{- if not (.Values.buildHosts | default dict).enabled -}}
{{- fail "portal.enabled is false but buildHosts.enabled is not true — a portal-less release renders ONLY build hosts, so one without any is a typo" -}}
{{- end -}}
{{- if (.Values.postgres | default dict).enabled -}}
{{- fail "portal.enabled is false but postgres.enabled is true — a build-host release runs no database; set postgres.enabled: false" -}}
{{- end -}}
{{- if (.Values.ingress | default dict).enabled -}}
{{- fail "portal.enabled is false but ingress.enabled is true — a build host serves nothing; set ingress.enabled: false" -}}
{{- end -}}
false
{{- end -}}
{{- end -}}

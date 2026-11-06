{{- /*
`memex.trustedCa` — the ADDITIONAL certificate authorities the platform's pods trust
(.Values.trustedCertificateAuthorities, MeshWeaver.Deploy#138). Answers a YAML dict; `enabled` is
false — and every caller renders NOTHING — unless at least one source is declared, so an estate
that declares none renders byte-identically to the chart before this key existed.

An estate whose hosts are private and whose certificates come from its own internal CA needs this
in three places: the self-updater's hand-over to the control instance, the control instance's
probe of a working instance's public /health, and a database behind a certificate from that CA.

HOW THE RUNTIME IS MADE TO TRUST IT, and why the system CAs are not dropped:
  .NET on Linux builds its root store from OpenSSL's two locations, the default FILE and the
  default DIRECTORY, each replaceable by an environment variable (SSL_CERT_FILE, SSL_CERT_DIR).
  Setting either to the estate's bundle alone REPLACES that half of the system store. So the
  `trusted-ca-bundle` init container writes ONE combined file — the image's own system bundle
  (`systemBundle`) followed by every declared PEM — into a pod-local emptyDir, and the workloads
  point SSL_CERT_FILE at it. SSL_CERT_DIR is left alone, so the default directory is still read
  as well. The init container REFUSES (exit 1, naming the path) when the system bundle is
  missing or a declared source holds no certificate: a pod that silently trusted ONLY the
  estate's CA, or none of it, is the failure this exists to prevent, and it would surface far
  from here as a TLS error against a public host.

The init container runs the PORTAL image and needs only `sh` and `cat` (crash-dump-prepare runs
`sh` on it already),
for the migration Job too: the combined file is plain PEM, and the system CAs it carries are the
public roots whichever image reads them. The migration Job's own image needs no shell.

SOURCES (any combination; each contributes every key it holds, in this order):
  pem        inline PEM text — public material, rendered into the ConfigMap `memex-trusted-ca`
  configMap  the NAME of a ConfigMap the estate creates
  secret     the NAME of a Secret the estate creates — e.g. one the Key Vault CSI driver syncs
             from a class declared under keyVaultSecrets / keyVaultSecretClasses
*/ -}}
{{- define "memex.trustedCa" -}}
{{- $t := (.Values.trustedCertificateAuthorities | default dict) -}}
{{- $pem := ($t.pem | default "" | trim) -}}
{{- $configMap := ($t.configMap | default "" | trim) -}}
{{- $secret := ($t.secret | default "" | trim) -}}
{{- $enabled := or $pem $configMap $secret -}}
{{- if and $pem (not (contains "-----BEGIN CERTIFICATE-----" $pem)) -}}
{{- fail "trustedCertificateAuthorities.pem holds no \"-----BEGIN CERTIFICATE-----\" block — it must be PEM text" -}}
{{- end -}}
enabled: {{ ternary "true" "false" (not (empty $enabled)) }}
pem: {{ $pem | quote }}
configMap: {{ $configMap | quote }}
secret: {{ $secret | quote }}
systemBundle: {{ ($t.systemBundle | default "/etc/ssl/certs/ca-certificates.crt") | quote }}
mountPath: {{ ($t.mountPath | default "/etc/memex/trusted-ca") | quote }}
bundleFile: {{ printf "%s/ca-bundle.crt" (($t.mountPath | default "/etc/memex/trusted-ca") | trimSuffix "/") | quote }}
{{- end -}}

{{- /* The init container that writes the combined bundle. `(dict "root" $ "image" <image>)`. */ -}}
{{- define "memex.trustedCa.initContainer" -}}
{{- $t := include "memex.trustedCa" .root | fromYaml -}}
{{- if $t.enabled }}
# 🔐 The pod's trust store: the image's system CAs PLUS the estate's declared CAs, in one file
# (trustedCertificateAuthorities, _trusted-ca.tpl). Refuses rather than drop either half.
- name: "trusted-ca-bundle"
  image: {{ .image | quote }}
  imagePullPolicy: "IfNotPresent"
  command:
    - "sh"
    - "-c"
    - |
      set -eu
      out={{ $t.bundleFile | quote }}
      sys={{ $t.systemBundle | quote }}
      if [ ! -s "$sys" ]; then
        echo "trusted-ca-bundle: ERROR the system CA bundle $sys is missing or empty in this image — refusing to write a trust store that would hold ONLY the estate's CAs (set trustedCertificateAuthorities.systemBundle)" >&2
        exit 1
      fi
      cat "$sys" > "$out.tmp"
      added=0
      for dir in /run/memex-trusted-ca/*; do
        [ -d "$dir" ] || continue
        found=0
        for f in "$dir"/*; do
          [ -f "$f" ] || continue
          case "$(cat "$f")" in
            *"-----BEGIN CERTIFICATE-----"*)
              printf '\n' >> "$out.tmp"
              cat "$f" >> "$out.tmp"
              found=$((found + 1)) ;;
          esac
        done
        if [ "$found" -eq 0 ]; then
          echo "trusted-ca-bundle: ERROR the declared source $(basename "$dir") holds no PEM certificate — refusing to start a pod that would not trust it" >&2
          exit 1
        fi
        echo "trusted-ca-bundle: $(basename "$dir") — $found file(s) appended"
        added=$((added + found))
      done
      mv "$out.tmp" "$out"
      echo "trusted-ca-bundle: $out = $sys + $added declared file(s); SSL_CERT_FILE points here"
  volumeMounts:
    - name: "trusted-ca-bundle"
      mountPath: {{ $t.mountPath | quote }}
{{- if $t.pem }}
    - name: "trusted-ca-inline"
      mountPath: "/run/memex-trusted-ca/inline"
      readOnly: true
{{- end }}
{{- if $t.configMap }}
    - name: "trusted-ca-configmap"
      mountPath: "/run/memex-trusted-ca/configmap"
      readOnly: true
{{- end }}
{{- if $t.secret }}
    - name: "trusted-ca-secret"
      mountPath: "/run/memex-trusted-ca/secret"
      readOnly: true
{{- end }}
  resources:
    requests: { cpu: "10m", memory: "16Mi" }
    limits: { memory: "64Mi" }
  securityContext:
    allowPrivilegeEscalation: false
    capabilities: { drop: ["ALL"] }
{{- end }}
{{- end -}}

{{- /* The env a workload container needs to read the combined bundle. */ -}}
{{- define "memex.trustedCa.env" -}}
{{- $t := include "memex.trustedCa" . | fromYaml -}}
{{- if $t.enabled }}
# 🔐 The combined trust store the trusted-ca-bundle init container wrote (system CAs + the
# estate's). SSL_CERT_DIR is deliberately unset, so OpenSSL's default directory is read too.
- name: "SSL_CERT_FILE"
  value: {{ $t.bundleFile | quote }}
{{- end }}
{{- end -}}

{{- /* The read-only mount of the combined bundle for a workload container. */ -}}
{{- define "memex.trustedCa.mount" -}}
{{- $t := include "memex.trustedCa" . | fromYaml -}}
{{- if $t.enabled }}
- name: "trusted-ca-bundle"
  mountPath: {{ $t.mountPath | quote }}
  readOnly: true
{{- end }}
{{- end -}}

{{- /* The pod volumes: the emptyDir the bundle is written to, and one volume per declared source. */ -}}
{{- define "memex.trustedCa.volumes" -}}
{{- $t := include "memex.trustedCa" . | fromYaml -}}
{{- if $t.enabled }}
- name: "trusted-ca-bundle"
  emptyDir: {}
{{- if $t.pem }}
- name: "trusted-ca-inline"
  configMap:
    name: "memex-trusted-ca"
{{- end }}
{{- if $t.configMap }}
- name: "trusted-ca-configmap"
  configMap:
    name: {{ $t.configMap | quote }}
{{- end }}
{{- if $t.secret }}
- name: "trusted-ca-secret"
  secret:
    secretName: {{ $t.secret | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "arbora.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "arbora.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "arbora.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "arbora.labels" -}}
app.kubernetes.io/name: {{ include "arbora.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end }}

{{- define "arbora.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "arbora.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- required "serviceAccount.name is required when serviceAccount.create=false" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "arbora.image" -}}
{{- $image := . -}}
{{- if $image.digest -}}
{{- printf "%s@%s" $image.repository $image.digest -}}
{{- else -}}
{{- printf "%s:%s" $image.repository (required "image.tag or image.digest is required" $image.tag) -}}
{{- end -}}
{{- end }}

{{/* The engine image: its own value, or the arbora (unified) image. */}}
{{- define "arbora.engineImage" -}}
{{- if .Values.engine.image.repository -}}
{{- include "arbora.image" .Values.engine.image -}}
{{- else -}}
{{- include "arbora.image" .Values.arbora.image -}}
{{- end -}}
{{- end }}

{{- define "arbora.unified" -}}
{{- if eq .Values.topology "unified-in-process" }}true{{ end -}}
{{- end }}

{{- define "arbora.sidecar" -}}
{{- if and (include "arbora.unified" .) (eq .Values.engine.placement "sidecar") }}true{{ end -}}
{{- end }}

{{- define "arbora.engineTcp" -}}
{{- if or (not (include "arbora.unified" .)) .Values.engine.tcp.enabled }}true{{ end -}}
{{- end }}

{{/* Eunomia default per identity mode: none -> none, local/external -> embedded. */}}
{{- define "arbora.eunomiaType" -}}
{{- if .Values.policy.eunomia.type -}}
{{- .Values.policy.eunomia.type -}}
{{- else if eq .Values.identity.mode "none" -}}
none
{{- else -}}
embedded
{{- end -}}
{{- end }}

{{/* Render-time guard rails. Each failure names the value to change. */}}
{{- define "arbora.validate" -}}
{{- $ack := "I-UNDERSTAND-ANYONE-WHO-CAN-REACH-THIS-PORT-IS-ADMIN" -}}
{{- if and (eq .Values.identity.mode "none") (ne .Values.identity.noneExposeAck $ack) -}}
{{- fail (printf "identity.mode=none on Kubernetes needs identity.noneExposeAck=%s: a pod never binds loopback only, so anyone who reaches it is an administrator. Use local or external instead." $ack) -}}
{{- end -}}
{{- if and (include "arbora.engineTcp" .) (not .Values.engine.tls.secretName) -}}
{{- fail "engine.tls.secretName is required: the engine refuses a non-loopback TCP listener without TLS (out-of-process-shared, or engine.tcp.enabled)" -}}
{{- end -}}
{{- if and (eq (include "arbora.eunomiaType" .) "remote") (not .Values.policy.eunomia.remoteUrl) -}}
{{- fail "policy.eunomia.remoteUrl is required when the Eunomia type is remote" -}}
{{- end -}}
{{- if and (eq .Values.secrets.backend "vault") (not .Values.secrets.vaultUrl) -}}
{{- fail "secrets.vaultUrl is required when secrets.backend=vault (or set secrets.backend=engine)" -}}
{{- end -}}
{{- end }}

{{/* Pod-spec header shared by the engine StatefulSet and the arbora
     Deployment: service account, service-link opt-out, grace period,
     security context and optional image pull secrets. Call with
     `| nindent 6` directly under each template's `spec:`. */}}
{{- define "arbora.podSpecHeader" -}}
serviceAccountName: {{ include "arbora.serviceAccountName" . }}
automountServiceAccountToken: false
enableServiceLinks: {{ .Values.enableServiceLinks }}
terminationGracePeriodSeconds: 90
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{/* The engine container's envFrom: the shared ConfigMap plus the optional
     runtime secret. Identical whether the engine runs as its own
     StatefulSet or as the unified pod's sidecar/child container. */}}
{{- define "arbora.engineEnvFrom" -}}
envFrom:
  - configMapRef:
      name: {{ include "arbora.fullname" . }}-engine
  - secretRef:
      name: {{ .Values.runtimeSecret.name }}
      optional: {{ .Values.runtimeSecret.optional }}
{{- end }}

{{/* The three volume mounts both the unified pod's engine (sidecar/child)
     and arbora containers share: ephemeral tmp, the engine's local
     socket, and the unified data volume. */}}
{{- define "arbora.sharedVolumeMounts" -}}
- {name: tmp, mountPath: /tmp}
- {name: engine-socket, mountPath: /run/epistemic-graph}
- {name: arbora-data, mountPath: /var/lib/arbora}
{{- end }}

{{/* Engine argv shared by the sidecar and the shared StatefulSet. */}}
{{- define "arbora.engineArgs" -}}
- epistemic-graph-server
- --persist-dir
- {{ if include "arbora.unified" . }}/var/lib/arbora/engine{{ else }}/var/lib/epistemic-graph/data{{ end }}
{{- if include "arbora.unified" . }}
- --socket-path
- /run/epistemic-graph/epistemic-graph.sock
{{- end }}
{{- if include "arbora.engineTcp" . }}
- --tcp-addr
- {{ printf "0.0.0.0:%v" .Values.engine.port }}
- --tcp-tls-cert
- /etc/arbora/engine-tls/tls.crt
- --tcp-tls-key
- /etc/arbora/engine-tls/tls.key
{{- end }}
{{- /* The engine refuses non-loopback auxiliary listeners; arbora merges
       this exposition into its own /metrics in the unified topology. */}}
- --metrics-addr
- 127.0.0.1:9101
{{- with .Values.engine.extraArgs }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "bitcast-x-miner.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "bitcast-x-miner.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "bitcast-x-miner.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bitcast-x-miner.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "bitcast-x-miner.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "bitcast-x-miner.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "bitcast-x-miner.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "bitcast-x-miner.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "bitcast-x-miner.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}
{{- end -}}

{{- define "bitcast-x-miner.claimName" -}}
{{- default (include "bitcast-x-miner.fullname" .) .Values.persistence.existingClaim -}}
{{- end -}}

{{/* Refuse to render a miner that could not work, rather than one that fails at runtime. */}}
{{- define "bitcast-x-miner.validate" -}}
{{- if not (has .Values.mode (list "run-miner" "run-miner-api")) -}}
{{- fail "mode must be run-miner or run-miner-api" -}}
{{- end -}}
{{- if not .Values.publicIP -}}
{{- fail "publicIP is required: it is advertised on chain and validators dial http://<publicIP>:<port>" -}}
{{- end -}}
{{- if or (eq .Values.publicIP "0.0.0.0") (hasPrefix "127." .Values.publicIP) -}}
{{- fail "publicIP must be an address validators can reach, never 0.0.0.0 or loopback" -}}
{{- end -}}
{{- if not .Values.wallet.existingSecret -}}
{{- fail "wallet.existingSecret is required: create a Secret with the hotkey keyfile first (see README)" -}}
{{- end -}}
{{- if and (eq .Values.mode "run-miner-api") (not .Values.minerApi.existingSecret) -}}
{{- fail "run-miner-api needs minerApi.existingSecret holding BITCAST_X_MINER_API_TOKEN" -}}
{{- end -}}
{{- if and .Values.minerApi.port (ne .Values.mode "run-miner-api") -}}
{{- fail "minerApi.port applies only to mode=run-miner-api" -}}
{{- end -}}
{{- if and .Values.minerApi.port (eq (int .Values.minerApi.port) (int .Values.port)) -}}
{{- fail "minerApi.port must differ from port" -}}
{{- end -}}
{{- if and .Values.minerApi.ingress.enabled (not .Values.minerApi.port) -}}
{{- fail "minerApi.ingress needs minerApi.port: without it /api/v1 shares the public validator port" -}}
{{- end -}}
{{- if and .Values.minerApi.ingress.enabled (not .Values.minerApi.ingress.host) -}}
{{- fail "minerApi.ingress.host is required" -}}
{{- end -}}
{{- if and (eq .Values.service.type "NodePort") (or (lt (int .Values.port) 30000) (gt (int .Values.port) 32767)) -}}
{{- fail "service.type=NodePort advertises `port` as the nodePort, so port must be in 30000-32767" -}}
{{- end -}}
{{- end -}}

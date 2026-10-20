{{/*
Expand the name of the chart.
*/}}
{{- define "vogon.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "vogon.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "vogon.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "vogon.labels" -}}
helm.sh/chart: {{ include "vogon.chart" . }}
app.kubernetes.io/name: {{ include "vogon.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels.
*/}}
{{- define "vogon.selectorLabels" -}}
app.kubernetes.io/name: {{ include "vogon.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Component selector labels.
*/}}
{{- define "vogon.componentSelectorLabels" -}}
{{ include "vogon.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Name of the ConfigMap containing non-sensitive environment variables.
*/}}
{{- define "vogon.configMapName" -}}
{{- printf "%s-config" (include "vogon.fullname" .) -}}
{{- end -}}

{{/*
Name of the Secret containing sensitive environment variables.
*/}}
{{- define "vogon.secretName" -}}
{{- default (printf "%s-secret" (include "vogon.fullname" .)) .Values.secret.existingSecret -}}
{{- end -}}

{{/*
Name of the existing DB secret (from CNPG or similar operator).
*/}}
{{- define "vogon.dbSecretName" -}}
{{- .Values.database.existingSecret.name -}}
{{- end -}}

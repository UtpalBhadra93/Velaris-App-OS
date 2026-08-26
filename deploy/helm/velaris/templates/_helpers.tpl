{{- define "helix.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "helix.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "helix.caseServiceName" -}}{{ include "helix.fullname" . }}-case-service{{- end -}}
{{- define "helix.engineName" -}}{{ include "helix.fullname" . }}-engine{{- end -}}
{{- define "helix.workerName" -}}{{ include "helix.fullname" . }}-worker{{- end -}}

{{- define "helix.databaseUrl" -}}
{{- if .Values.postgresql.enabled -}}
postgresql+asyncpg://{{ .Values.postgresql.auth.username }}:{{ .Values.postgresql.auth.password }}@{{ .Release.Name }}-postgresql:5432/{{ .Values.postgresql.auth.database }}
{{- else if and .Values.mysql .Values.mysql.enabled -}}
mysql+aiomysql://{{ .Values.mysql.auth.username }}:{{ .Values.mysql.auth.password }}@{{ .Release.Name }}-mysql:3306/{{ .Values.mysql.auth.database }}
{{- else if and .Values.mariadb .Values.mariadb.enabled -}}
mysql+aiomysql://{{ .Values.mariadb.auth.username }}:{{ .Values.mariadb.auth.password }}@{{ .Release.Name }}-mariadb:3306/{{ .Values.mariadb.auth.database }}
{{- else -}}
{{ .Values.externalDatabaseUrl }}
{{- end -}}
{{- end -}}

{{- define "helix.databaseBackend" -}}
{{- if .Values.postgresql.enabled -}}
postgresql
{{- else if and .Values.mysql .Values.mysql.enabled -}}
mysql
{{- else if and .Values.mariadb .Values.mariadb.enabled -}}
mariadb
{{- else -}}
{{ .Values.externalDatabaseBackend | default "postgresql" }}
{{- end -}}
{{- end -}}

{{- define "helix.redisUrl" -}}
{{- if .Values.valkey.enabled -}}
redis://{{ .Release.Name }}-valkey-primary:6379/0
{{- else -}}
{{ .Values.externalRedisUrl }}
{{- end -}}
{{- end -}}
{{/* Services extracted from case-service at roadmap 13c-e. */}}
{{- define "helix.analyticsServiceName" -}}{{ include "helix.fullname" . }}-analytics-service{{- end -}}
{{- define "helix.hxgraphServiceName" -}}{{ include "helix.fullname" . }}-hxgraph-service{{- end -}}
{{- define "helix.hxmigrateServiceName" -}}{{ include "helix.fullname" . }}-hxmigrate-service{{- end -}}

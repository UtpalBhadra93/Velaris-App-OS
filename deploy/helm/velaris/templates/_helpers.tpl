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

{{/*
The database URL carries a PASSWORD, so it belongs in a Secret rather than in
every Deployment's env block. Rendered inline it is readable by anyone who can
`get deployments` — a far wider group than `get secrets` on most clusters — and
it lands in the helm release history too. `existingSecret` lets an operator
bring their own (from an external secret manager) and have the chart write none.
*/}}
{{- define "helix.dbSecretName" -}}
{{- if .Values.databaseSecret.existingSecret -}}
{{ .Values.databaseSecret.existingSecret }}
{{- else -}}
{{ include "helix.fullname" . }}-db
{{- end -}}
{{- end -}}

{{- define "helix.dbSecretKey" -}}
{{- .Values.databaseSecret.key | default "database-url" -}}
{{- end -}}

{{/*
Pod and container hardening, applied to every Velaris workload.

The bundled PostgreSQL and Valkey subcharts have shipped these since day one;
our own six deployments had NOTHING — no `runAsNonRoot`, no dropped
capabilities, privilege escalation allowed — so a container escape had more to
work with than it needed. The images already run as an unprivileged user, so
this states the same thing where the cluster can ENFORCE it, and satisfies the
`restricted` Pod Security Standard.

`readOnlyRootFilesystem` is NOT set here and is off by default: case-service
writes uploaded documents to the container filesystem when local storage is
configured, and turning it on silently breaks that. `values.yaml` carries the
switch with the reason.
*/}}
{{- define "helix.podSecurityContext" -}}
runAsNonRoot: {{ .Values.podSecurity.runAsNonRoot }}
{{- if .Values.podSecurity.runAsUser }}
runAsUser: {{ .Values.podSecurity.runAsUser }}
{{- end }}
{{- if .Values.podSecurity.fsGroup }}
fsGroup: {{ .Values.podSecurity.fsGroup }}
{{- end }}
seccompProfile:
  type: {{ .Values.podSecurity.seccompProfile }}
{{- end -}}

{{- define "helix.containerSecurityContext" -}}
allowPrivilegeEscalation: false
privileged: false
readOnlyRootFilesystem: {{ .Values.podSecurity.readOnlyRootFilesystem }}
capabilities:
  drop: ["ALL"]
{{- end -}}

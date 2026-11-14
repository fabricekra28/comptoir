{{/* Étiquettes communes à tous les objets. */}}
{{- define "comptoir.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
app.kubernetes.io/name: comptoir
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Étiquettes qui désignent les pods d'un composant. Attend : dict "root" $ "component" "users" */}}
{{- define "comptoir.selector" -}}
app.kubernetes.io/name: comptoir
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/* Nom réseau d'un service : gateway, users-service, products-service... */}}
{{- define "comptoir.svcName" -}}
{{- if eq . "gateway" -}}gateway{{- else -}}{{ . }}-service{{- end -}}
{{- end }}

{{/* Nom du Secret : celui fourni, sinon celui créé par la chart. */}}
{{- define "comptoir.secretName" -}}
{{- default "comptoir-secrets" .Values.secrets.existingSecret -}}
{{- end }}

{{/* Hôte, port et utilisateur de la base. */}}
{{- define "comptoir.dbHost" -}}
{{- if .Values.postgresql.enabled -}}postgres{{- else -}}
{{- required "externalDatabase.host est obligatoire quand postgresql.enabled vaut false" .Values.externalDatabase.host -}}
{{- end -}}
{{- end }}

{{- define "comptoir.dbPort" -}}
{{- if .Values.postgresql.enabled -}}5432{{- else -}}{{ int .Values.externalDatabase.port }}{{- end -}}
{{- end }}

{{- define "comptoir.dbUser" -}}
{{- if .Values.postgresql.enabled -}}{{ .Values.postgresql.user }}{{- else -}}{{ .Values.externalDatabase.user }}{{- end -}}
{{- end }}

{{/* HTTPS actif à l'entrée ? */}}
{{- define "comptoir.tlsEnabled" -}}
{{- if and .Values.exposure.gatewayApi.enabled .Values.exposure.gatewayApi.tls.enabled -}}true{{- end -}}
{{- end }}

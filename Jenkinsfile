pipeline {
    agent any

    options {
        timestamps()
        timeout(time: 120, unit: 'MINUTES')   // six services au lieu d'un
        disableConcurrentBuilds()
    }

    parameters {
        // Décoché par défaut : le pipeline s'arrête après la publication des images.
        booleanParam(
            name: 'DEPLOY',
            defaultValue: false,
            description: 'Déployer : inscrire la version de ce build dans Git, puis Argo CD met le cluster à jour'
        )
    }

    environment {
        // Un dossier par service à la racine du dépôt.
        SERVICES = 'gateway users products orders cart notifications'
        // Images : comptoir-gateway, comptoir-users, ... (vos anciennes images users-service, etc. ne sont pas écrasées)
        IMAGE_PREFIX = 'comptoir'
        SONAR_PREFIX = 'comptoir'
        REPORT_DIR = 'reports'
        TRIVY_CACHE_DIR = '/var/lib/jenkins/.cache/trivy'
        DOCKERHUB_USER = 'fabrice28'
        HELM_CHART = 'helm/comptoir'
        HELM_RELEASE = 'comptoir'
        K8S_NAMESPACE = 'comptoir'
        // Fichier lu par Argo CD : il contient la version des images à déployer.
        IMAGE_TAG_FILE = 'helm/comptoir/image-tag.yaml'
        EMAIL_RECIPIENT = 'krafabrice64@gmail.com'
    }

    stages {
        stage('1 - Checkout') {
            steps {
                deleteDir()

                git branch: 'main',
                    url: 'https://github.com/fabricekra28/comptoir.git'

                sh '''
                    set -eu
                    mkdir -p reports

                    for svc in $SERVICES; do
                        test -f "$svc/main.py"
                        test -f "$svc/requirements.txt"
                        test -f "$svc/Dockerfile"
                        echo "Service présent : $svc"
                    done

                    git log -1 --oneline
                '''
            }
        }

        stage('2 - Secret Detection / Gitleaks') {
            steps {
                catchError(buildResult: 'SUCCESS', stageResult: 'SUCCESS') {
                    // Un seul passage : Gitleaks lit l'historique Git, commun aux six services.
                    sh '''
                        mkdir -p reports

                        docker run --rm \
                          --user "$(id -u):$(id -g)" \
                          -v "$WORKSPACE:/repo" \
                          -w /repo \
                          ghcr.io/gitleaks/gitleaks:latest \
                          git . \
                          --report-format json \
                          --report-path reports/gitleaks-report.json \
                          --exit-code 0

                        echo "Gitleaks terminé."
                    '''
                }
            }
        }

        stage('3 - SAST / Semgrep') {
            steps {
                catchError(buildResult: 'SUCCESS', stageResult: 'SUCCESS') {
                    sh '''
                        mkdir -p reports

                        for svc in $SERVICES; do
                            echo "=== Semgrep : $svc ==="

                            docker run --rm \
                              --user "$(id -u):$(id -g)" \
                              -e HOME=/tmp \
                              -v "$WORKSPACE:/src" \
                              -w /src \
                              semgrep/semgrep:latest \
                              semgrep scan \
                              --config p/python \
                              --config p/dockerfile \
                              --config p/owasp-top-ten \
                              --metrics=off \
                              --json \
                              --output "reports/semgrep-$svc-report.json" \
                              "$svc" \
                              || echo "ATTENTION : Semgrep en erreur sur $svc"
                        done

                        echo "Semgrep terminé."
                    '''
                }
            }
        }

        // Analyse et Quality Gate sont réunis : waitForQualityGate ne suit que la
        // dernière analyse lancée, il doit donc être appelé après chaque service.
        stage('4 - SAST / SonarQube + Quality Gate') {
            steps {
                script {
                    def scannerHome = tool 'SonarQube'

                    for (svc in env.SERVICES.tokenize(' ')) {
                        catchError(buildResult: 'SUCCESS', stageResult: 'SUCCESS') {
                            withSonarQubeEnv('SonarQube') {
                                sh """
                                    '${scannerHome}/bin/sonar-scanner' \
                                      -Dsonar.projectKey=${env.SONAR_PREFIX}-${svc} \
                                      -Dsonar.projectName='Comptoir - ${svc}' \
                                      -Dsonar.projectBaseDir=${svc} \
                                      -Dsonar.sources=. \
                                      -Dsonar.exclusions='**/__pycache__/**'
                                """
                            }

                            timeout(time: 5, unit: 'MINUTES') {
                                def qualityGate = waitForQualityGate()

                                echo "SonarQube Quality Gate (${svc}) : ${qualityGate.status}"

                                if (qualityGate.status != 'OK') {
                                    echo "ATTENTION : Quality Gate non valide pour ${svc}."
                                }
                            }
                        }
                    }
                }
            }
        }

        stage('5 - Dependency Scan / Trivy FS') {
            steps {
                catchError(buildResult: 'SUCCESS', stageResult: 'SUCCESS') {
                    sh '''
                        mkdir -p reports

                        for svc in $SERVICES; do
                            echo "=== Trivy FS : $svc ==="

                            trivy fs \
                              --scanners vuln \
                              --severity HIGH,CRITICAL \
                              --ignore-unfixed \
                              --exit-code 0 \
                              --format json \
                              --output "reports/trivy-fs-$svc-report.json" \
                              "$svc" \
                              || echo "ATTENTION : Trivy FS en erreur sur $svc"
                        done

                        echo "Trivy FS terminé."
                    '''
                }
            }
        }

        stage('6 - IaC Scan / Trivy Config') {
            steps {
                catchError(buildResult: 'SUCCESS', stageResult: 'SUCCESS') {
                    // Les six Dockerfiles, les manifestes du dossier k8s et la chart Helm.
                    sh '''
                        mkdir -p reports

                        for target in $SERVICES k8s helm; do
                            echo "=== Trivy Config : $target ==="

                            trivy config \
                              --severity CRITICAL \
                              --exit-code 0 \
                              --format json \
                              --output "reports/trivy-config-$target-report.json" \
                              "$target" \
                              || echo "ATTENTION : Trivy Config en erreur sur $target"
                        done

                        echo "Trivy Config terminé."
                    '''
                }
            }
        }

        stage('7 - IaC Scan / Checkov') {
            steps {
                catchError(buildResult: 'SUCCESS', stageResult: 'SUCCESS') {
                    sh '''
                        mkdir -p reports

                        for target in $SERVICES k8s helm; do
                            echo "=== Checkov : $target ==="

                            set +e
                            docker run --rm \
                              --user "$(id -u):$(id -g)" \
                              -v "$WORKSPACE:/repo" \
                              -w /repo \
                              bridgecrew/checkov:latest \
                              --directory "/repo/$target" \
                              --output json \
                              > "reports/checkov-$target-report.json"
                            rc=$?
                            set -e

                            echo "Checkov ($target) terminé avec le code : $rc"
                        done
                    '''
                }
            }
        }

        stage('8 - Docker Build') {
            steps {
                sh '''
                    set -eu

                    for svc in $SERVICES; do
                        echo "=== Build : $svc ==="

                        docker build \
                          --tag "${IMAGE_PREFIX}-${svc}:${BUILD_NUMBER}" \
                          "$svc"

                        echo "Image construite : ${IMAGE_PREFIX}-${svc}:${BUILD_NUMBER}"
                    done
                '''
            }
        }

        stage('9 - Container Image Scan / Trivy') {
            steps {
                catchError(buildResult: 'SUCCESS', stageResult: 'SUCCESS') {
                    sh '''
                        mkdir -p reports

                        for svc in $SERVICES; do
                            echo "=== Trivy Image : $svc ==="

                            trivy image \
                              --severity HIGH,CRITICAL \
                              --ignore-unfixed \
                              --exit-code 0 \
                              --format json \
                              --output "reports/trivy-image-$svc-report.json" \
                              "${IMAGE_PREFIX}-${svc}:${BUILD_NUMBER}" \
                              || echo "ATTENTION : Trivy Image en erreur sur $svc"
                        done

                        echo "Trivy Image terminé."
                    '''
                }
            }
        }

        // Toute la boutique est démarrée sur un réseau temporaire (base + six services),
        // puis ZAP analyse le site (gateway) et la documentation d'API de chaque service.
        stage('10 - DAST / OWASP ZAP') {
            steps {
                catchError(buildResult: 'SUCCESS', stageResult: 'SUCCESS') {
                    sh '''
                        set -eu

                        PREFIX="comptoir-ci-${BUILD_NUMBER}"
                        NETWORK="$PREFIX"
                        DB="$PREFIX-db"

                        port_of() {
                            case "$1" in
                                gateway) echo 8000 ;;
                                users) echo 8001 ;;
                                products) echo 8002 ;;
                                orders) echo 8003 ;;
                                cart) echo 8004 ;;
                                notifications) echo 8005 ;;
                            esac
                        }

                        # Nom réseau attendu par les autres services (users-service, products-service, ...).
                        alias_of() {
                            if [ "$1" = "gateway" ]; then
                                echo "gateway"
                            else
                                echo "$1-service"
                            fi
                        }

                        cleanup() {
                            for svc in $SERVICES db; do
                                docker rm -f "$PREFIX-$svc" >/dev/null 2>&1 || true
                            done
                            docker network rm "$NETWORK" >/dev/null 2>&1 || true
                        }

                        trap cleanup EXIT

                        mkdir -p reports

                        docker network create "$NETWORK"

                        docker run -d \
                          --name "$DB" \
                          --network "$NETWORK" \
                          --health-cmd="pg_isready -U comptoir -d comptoirdb" \
                          --health-interval=5s \
                          --health-timeout=3s \
                          --health-retries=20 \
                          -e POSTGRES_DB=comptoirdb \
                          -e POSTGRES_USER=comptoir \
                          -e POSTGRES_PASSWORD=ci-test-password \
                          postgres:17

                        ready=0

                        for i in $(seq 1 30); do
                            status=$(docker inspect \
                              --format='{{.State.Health.Status}}' "$DB" 2>/dev/null || true)

                            if [ "$status" = "healthy" ]; then
                                ready=1
                                break
                            fi

                            sleep 2
                        done

                        if [ "$ready" -ne 1 ]; then
                            docker logs "$DB" || true
                            echo "ATTENTION : base de test indisponible."
                            exit 0
                        fi

                        # Valeurs jetables, propres à ce build : aucun secret réel ici.
                        for svc in $SERVICES; do
                            docker run -d \
                              --name "$PREFIX-$svc" \
                              --network "$NETWORK" \
                              --network-alias "$(alias_of "$svc")" \
                              -e DATABASE_URL="postgresql://comptoir:ci-test-password@${DB}:5432/comptoirdb" \
                              -e JWT_SECRET="ci-jwt-${BUILD_NUMBER}-valeur-jetable-pour-les-tests" \
                              -e INTERNAL_TOKEN="ci-interne-${BUILD_NUMBER}-valeur-jetable" \
                              -e SEED_DEMO=true \
                              "${IMAGE_PREFIX}-${svc}:${BUILD_NUMBER}"
                        done

                        for svc in $SERVICES; do
                            app_ready=0

                            # Chaque image déclare son propre HEALTHCHECK : on attend qu'il passe.
                            for i in $(seq 1 45); do
                                status=$(docker inspect \
                                  --format='{{.State.Health.Status}}' "$PREFIX-$svc" 2>/dev/null || true)

                                if [ "$status" = "healthy" ]; then
                                    app_ready=1
                                    break
                                fi

                                sleep 2
                            done

                            if [ "$app_ready" -ne 1 ]; then
                                docker logs --tail 40 "$PREFIX-$svc" || true
                                echo "ATTENTION : $svc indisponible, scan ZAP ignoré pour ce service."
                                continue
                            fi

                            if [ "$svc" = "gateway" ]; then
                                target="http://gateway:8000/"
                            else
                                target="http://$(alias_of "$svc"):$(port_of "$svc")/docs"
                            fi

                            echo "=== ZAP : $svc ($target) ==="

                            set +e

                            docker run --rm \
                              --network "$NETWORK" \
                              -v "$WORKSPACE/reports:/zap/wrk/:rw" \
                              ghcr.io/zaproxy/zaproxy:stable \
                              zap-baseline.py \
                              -t "$target" \
                              -m 1 \
                              -I \
                              -J "zap-$svc-report.json" \
                              -r "zap-$svc-report.html"

                            zap_rc=$?
                            set -e

                            echo "Code retour ZAP ($svc) : $zap_rc"
                        done

                        echo "Scan DAST terminé."
                        exit 0
                    '''
                }
            }
        }

        stage('11 - Push Images / Docker Hub') {
            steps {
                withCredentials([
                    usernamePassword(
                        credentialsId: 'dockerhub-fabrice28',
                        usernameVariable: 'DH_USER',
                        passwordVariable: 'DH_TOKEN'
                    )
                ]) {
                    sh '''
                        set -eu
                        set +x

                        cleanup_login() {
                            docker logout >/dev/null 2>&1 || true
                        }

                        trap cleanup_login EXIT

                        printf '%s' "$DH_TOKEN" | docker login \
                          --username "$DH_USER" \
                          --password-stdin

                        # Seul le tag du build est publié : chaque image reste liée aux analyses de son build.
                        for svc in $SERVICES; do
                            LOCAL_IMAGE="${IMAGE_PREFIX}-${svc}:${BUILD_NUMBER}"
                            REMOTE_IMAGE="${DH_USER}/${IMAGE_PREFIX}-${svc}:${BUILD_NUMBER}"

                            docker tag "$LOCAL_IMAGE" "$REMOTE_IMAGE"
                            docker push "$REMOTE_IMAGE"

                            # Le tag latest n'est plus publié : on retire celui que d'anciens builds
                            # ont laissé sur ce serveur (sans effet s'il n'existe pas).
                            docker rmi "${DH_USER}/${IMAGE_PREFIX}-${svc}:latest" >/dev/null 2>&1 || true

                            echo "Image publiée : $REMOTE_IMAGE"
                        done

                        echo "Publication Docker Hub terminée."
                    '''
                }
            }
        }
        // Contrôle la chart avec Helm lui-même et archive les manifestes qu'elle produit.
        stage('12 - Helm Lint') {
            steps {
                catchError(buildResult: 'SUCCESS', stageResult: 'UNSTABLE') {
                    sh '''
                        set -eu
                        mkdir -p reports

                        if ! command -v helm >/dev/null 2>&1; then
                            echo "ATTENTION : helm n'est pas installé sur cette machine, contrôle ignoré."
                            exit 0
                        fi

                        helm version --short

                        # Mêmes fichiers de valeurs qu'Argo CD.
                        helm lint "$HELM_CHART" \
                          --values "$HELM_CHART/values-k3s.yaml" \
                          --set image.tag="$BUILD_NUMBER"

                        helm template "$HELM_RELEASE" "$HELM_CHART" \
                          --namespace "$K8S_NAMESPACE" \
                          --values "$HELM_CHART/values-k3s.yaml" \
                          --set image.tag="$BUILD_NUMBER" \
                          > reports/helm-rendered.yaml

                        echo "Chart valide. Manifestes produits : reports/helm-rendered.yaml"
                    '''
                }
            }
        }

        // GitOps : Jenkins n'accède pas au cluster. Il inscrit dans Git la version des images
        // de ce build ; Argo CD voit le changement et met le cluster à jour.
        stage('13 - Deploy / GitOps (Argo CD)') {
            when {
                expression { return params.DEPLOY }
            }
            steps {
                withCredentials([
                    usernamePassword(
                        credentialsId: 'github-comptoir',
                        usernameVariable: 'GH_USER',
                        passwordVariable: 'GH_TOKEN'
                    )
                ]) {
                    sh '''
                        set -eu
                        set +x

                        git config user.name "Jenkins"
                        git config user.email "jenkins@comptoir.local"

                        # Le jeton n'est écrit ni dans l'adresse du dépôt ni dans les logs.
                        git config credential.helper \
                          '!f() { echo "username=$GH_USER"; echo "password=$GH_TOKEN"; }; f'

                        # Reprend les commits arrivés sur main pendant le build.
                        git pull --rebase origin main

                        printf '%s\n' \
                          "# Version des images déployée par Argo CD." \
                          "# Fichier réécrit par Jenkins à chaque déploiement (étape 13) : ne pas le modifier à la main." \
                          "image:" \
                          "  tag: \"${BUILD_NUMBER}\"" \
                          > "$IMAGE_TAG_FILE"

                        git add "$IMAGE_TAG_FILE"

                        if git diff --cached --quiet; then
                            echo "Version déjà inscrite dans Git : rien à pousser."
                        else
                            git commit -m "deploy: images du build ${BUILD_NUMBER}"
                            git push origin HEAD:main
                            echo "Version ${BUILD_NUMBER} inscrite dans Git. Argo CD déploie dans les minutes qui suivent."
                        fi

                        git config --unset credential.helper || true
                    '''
                }
            }
        }
    }

    post {
        always {
            script {
                // Nettoyer uniquement les ressources temporaires.
                sh '''
                    for svc in $SERVICES db; do
                        docker rm -f "comptoir-ci-${BUILD_NUMBER}-$svc" >/dev/null 2>&1 || true
                    done

                    docker network rm "comptoir-ci-${BUILD_NUMBER}" >/dev/null 2>&1 || true

                    for svc in $SERVICES; do
                        echo "Image locale conservée : ${IMAGE_PREFIX}-${svc}:${BUILD_NUMBER}"
                    done
                '''

                // Archiver les rapports disponibles.
                junit(
                    testResults: 'reports/pytest-results.xml',
                    allowEmptyResults: true
                )

                archiveArtifacts(
                    artifacts: 'reports/**',
                    allowEmptyArchive: true,
                    fingerprint: true
                )

                // Envoyer le compte rendu, même si le pipeline échoue.
                try {
                    def buildStatus = currentBuild.currentResult ?: 'UNKNOWN'

                    def statusLabel = [
                        'SUCCESS': 'SUCCES',
                        'FAILURE': 'ECHEC',
                        'UNSTABLE': 'INSTABLE',
                        'ABORTED': 'ANNULE',
                        'NOT_BUILT': 'NON EXECUTE'
                    ].get(buildStatus, buildStatus)

                    def imageRows = ''
                    for (svc in env.SERVICES.tokenize(' ')) {
                        def repo = "${env.DOCKERHUB_USER}/${env.IMAGE_PREFIX}-${svc}"
                        imageRows += """
                            <tr>
                                <td style="border:1px solid #ddd;"><b>${svc}</b></td>
                                <td style="border:1px solid #ddd;">
                                    <a href="https://hub.docker.com/r/${repo}">${repo}</a>
                                    : tag ${env.BUILD_NUMBER}
                                </td>
                            </tr>"""
                    }

                    def emailBody = """
                    <html>
                    <body style="font-family:Arial,sans-serif;color:#222;">
                        <h2>Rapport du pipeline DevSecOps</h2>
                        <p>Bonjour,</p>
                        <p>Voici le résumé de l'exécution du pipeline Jenkins pour les six services de la boutique.</p>

                        <table cellpadding="8" cellspacing="0"
                               style="border-collapse:collapse;width:100%;">
                            <tr>
                                <td style="border:1px solid #ddd;"><b>Projet</b></td>
                                <td style="border:1px solid #ddd;">${env.JOB_NAME}</td>
                            </tr>
                            <tr>
                                <td style="border:1px solid #ddd;"><b>Build</b></td>
                                <td style="border:1px solid #ddd;">#${env.BUILD_NUMBER}</td>
                            </tr>
                            <tr>
                                <td style="border:1px solid #ddd;"><b>Statut</b></td>
                                <td style="border:1px solid #ddd;"><b>${statusLabel}</b></td>
                            </tr>
                            <tr>
                                <td style="border:1px solid #ddd;"><b>Durée</b></td>
                                <td style="border:1px solid #ddd;">${currentBuild.durationString}</td>
                            </tr>
                        </table>

                        <h3>Images Docker Hub</h3>
                        <p>Publiées uniquement si le pipeline est allé jusqu'à l'étape 11.</p>
                        <table cellpadding="8" cellspacing="0"
                               style="border-collapse:collapse;width:100%;">
                            ${imageRows}
                        </table>

                        <h3>Résumé des contrôles</h3>
                        <ul>
                            <li>Checkout du code GitHub</li>
                            <li>Détection de secrets : Gitleaks (dépôt entier)</li>
                            <li>Analyse statique : Semgrep (par service)</li>
                            <li>Analyse de code et Quality Gate : SonarQube (par service)</li>
                            <li>Analyse des dépendances : Trivy FS (par service)</li>
                            <li>Analyse IaC : Trivy Config et Checkov (par service et dossier k8s)</li>
                            <li>Construction des six images Docker</li>
                            <li>Analyse des images : Trivy (par service)</li>
                            <li>Tests dynamiques : OWASP ZAP (site et API de chaque service)</li>
                            <li>Publication Docker Hub</li>
                            <li>Contrôle de la chart Helm</li>
                            <li>Déploiement par Argo CD : ${params.DEPLOY ? 'demandé (version inscrite dans Git)' : 'non demandé pour ce build'}</li>
                        </ul>

                        <p>
                            <b>Attention :</b> certains contrôles sont configurés
                            en mode non bloquant. Le statut global SUCCESS ne
                            signifie donc pas nécessairement que toutes les
                            analyses de sécurité sont exemptes de problèmes.
                            Consulte les rapports pour les résultats détaillés.
                        </p>

                        <h3>Liens utiles</h3>
                        <p>
                            <a href="${env.BUILD_URL}">Ouvrir le build Jenkins</a>
                        </p>
                        <p>
                            <a href="${env.BUILD_URL}console">Consulter les logs</a>
                        </p>
                        <p>
                            <a href="${env.BUILD_URL}artifact/reports/">
                            Consulter les rapports archivés</a>
                        </p>

                        <hr/>
                        <p style="font-size:12px;color:#666;">
                            Notification automatique générée par Jenkins.
                            Les pièces jointes dépendent des rapports effectivement générés.
                        </p>
                    </body>
                    </html>
                    """

                    emailext(
                        to: env.EMAIL_RECIPIENT,
                        subject: "[DevSecOps] ${statusLabel} - ${env.JOB_NAME} #${env.BUILD_NUMBER}",
                        mimeType: 'text/html',
                        body: emailBody,
                        attachLog: true,
                        attachmentsPattern: 'reports/**'
                    )
                } catch (Exception e) {
                    echo "ATTENTION : notification e-mail impossible : ${e.message}"
                }
            }
        }

        success {
            echo 'Pipeline terminée avec succès.'
        }

        unstable {
            echo 'Pipeline instable : consulter les rapports.'
        }

        failure {
            echo 'Pipeline en échec : consulter le stage concerné.'
        }

        aborted {
            echo 'Pipeline annulé.'
        }
    }
}
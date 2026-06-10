set quiet
set shell := ['bash', '-euo', 'pipefail', '-c']
set script-interpreter := ['bash', '-euo', 'pipefail']

[private]
default:
    just -l kube

[doc('Force Flux to pull in changes from your Git repository')]
[group('kube')]
reconcile:
    flux --namespace flux-system reconcile kustomization flux-system --with-source

[doc('Force Flux to reconcile cluster apps')]
[group('kube')]
reconcile-apps:
    flux --namespace flux-system reconcile kustomization cluster-apps --with-source

[doc('Scale down all PostgreSQL-dependent Deployments to zero replicas')]
[group('kube')]
postgresql-scale-down-apps:
    kubectl get deploy -A -l app.ceres.io/depends-on-postgresql=true -o json \
      | yq -r '.items[] | "kubectl -n " + .metadata.namespace + " scale deploy " + .metadata.name + " --replicas=0"' \
      | sh

[doc('Scale up all PostgreSQL-dependent Deployments to one replica')]
[group('kube')]
postgresql-scale-up-apps:
    kubectl get deploy -A -l app.ceres.io/depends-on-postgresql=true -o json \
      | yq -r '.items[] | "kubectl -n " + .metadata.namespace + " scale deploy " + .metadata.name + " --replicas=1"' \
      | sh

[doc('Hibernate all CNPG clusters')]
[group('kube')]
cnpg-hibernate-all:
    kubectl get clusters.postgresql.cnpg.io -A -o json \
      | yq -r '.items[] | "kubectl -n " + .metadata.namespace + " annotate cluster.postgresql.cnpg.io " + .metadata.name + " --overwrite cnpg.io/hibernation=on"' \
      | sh

[doc('Unhibernate all CNPG clusters')]
[group('kube')]
cnpg-unhibernate-all:
    kubectl get clusters.postgresql.cnpg.io -A -o json \
      | yq -r '.items[] | "kubectl -n " + .metadata.namespace + " annotate cluster.postgresql.cnpg.io " + .metadata.name + " --overwrite cnpg.io/hibernation=off"' \
      | sh

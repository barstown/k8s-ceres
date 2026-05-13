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

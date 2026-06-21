## Labeling Standard

This document defines custom Kubernetes label conventions for this repository.

### Goals

- Keep labels consistent across apps and clusters.
- Enable safe, predictable bulk operations with label selectors.
- Avoid exposing private domain information in public manifests.

### Prefixes

Use the following custom label prefixes:

- `app.ceres.io/*` for workload characteristics and dependencies.
- `ops.ceres.io/*` for operational grouping and automation.

Do not use real private domain names in custom label keys.

### Naming Rules

- Keys must be lowercase and kebab-case.
- Values should be short and selector-friendly.
- Boolean-like values must be strings: `"true"` or `"false"`.
- One concept per label key.
- Do not store secrets, hostnames, or credentials in labels.

### Recommended Labels (v1)

Application labels:

- `app.ceres.io/depends-on-postgresql: "true"`
- `app.ceres.io/depends-on-redis: "true"`

Operations labels:

- `ops.ceres.io/maintenance-group: "<group-name>"`
- `ops.ceres.io/backup-priority: "critical|high|normal|low"`

### HelmRelease Guidance

For Helm-based workloads, add labels to rendered workloads using `postRenderers`
so selectors work against Deployments/StatefulSets.

Example (Deployment):

```yaml
postRenderers:
  - kustomize:
      patches:
        - target:
            group: apps
            version: v1
            kind: Deployment
          patch: |
            - op: add
              path: /metadata/labels/app.ceres.io~1depends-on-postgresql
              value: "true"
            - op: add
              path: /spec/template/metadata/labels/app.ceres.io~1depends-on-postgresql
              value: "true"
```

Notes:

- Use JSON patch escaping for `/` in label keys (`~1`).
- Label values must be strings; use `"true"` and `"false"`, not bare booleans.
- Apply the same key/value to `metadata.labels` and pod template labels when
  possible.

### Operational Queries

List all PostgreSQL-dependent Deployments:

```bash
kubectl get deploy -A -l app.ceres.io/depends-on-postgresql=true
```

Generate scale commands for all PostgreSQL-dependent Deployments (dry run):

```bash
kubectl get deploy -A -l app.ceres.io/depends-on-postgresql=true -o json \
| jq -r '.items[] | "kubectl -n \(.metadata.namespace) scale deploy \(.metadata.name) --replicas=0"'
```

Scale all PostgreSQL-dependent Deployments to zero:

```bash
kubectl get deploy -A -l app.ceres.io/depends-on-postgresql=true -o json \
| jq -r '.items[] | "kubectl -n \(.metadata.namespace) scale deploy \(.metadata.name) --replicas=0"' \
| sh
```

### Annotations vs Labels

Use labels for selection/filtering. Use annotations for non-selector metadata.

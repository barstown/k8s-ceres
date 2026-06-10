## PostgreSQL and CNPG Maintenance Runbook

Use this runbook for PostgreSQL image upgrades and CNPG hibernation workflows.

### Purpose

- Prevent app write/read errors during PostgreSQL image upgrades.
- Ensure PostgreSQL-dependent apps are intentionally scaled down first.
- Provide a separate, repeatable hibernation flow for maintenance windows.

### Scope

This applies to workloads labeled with:

- `app.ceres.io/depends-on-postgresql=true`

And CNPG clusters (`kind: Cluster` in `postgresql.cnpg.io`).

### Important Constraint

Do not perform PostgreSQL image updates while clusters are hibernated.

- Hibernation removes CNPG Pods.
- Rolling image updates require running Pods to reconcile and roll.
- For image upgrades: keep clusters running (apps can be scaled down).

### Workflow A: PostgreSQL Image Upgrade (No Hibernation)

1. Scale down PostgreSQL-dependent apps.
2. Merge/apply PostgreSQL image changes while CNPG clusters remain running.
3. Verify CNPG clusters are healthy.
4. Scale applications back up.

#### A1) Pre-Merge App Scale-Down

1. Validate which Deployments will be scaled down (dry run).

```bash
kubectl get deploy -A -l app.ceres.io/depends-on-postgresql=true -o json \
| yq -r '.items[] | "kubectl -n " + .metadata.namespace + " scale deploy " + .metadata.name + " --replicas=0"'
```

2. Execute the generated scale-down commands.

```bash
kubectl get deploy -A -l app.ceres.io/depends-on-postgresql=true -o json \
| yq -r '.items[] | "kubectl -n " + .metadata.namespace + " scale deploy " + .metadata.name + " --replicas=0"' \
| sh
```

3. Verify all matching Deployments are scaled to zero.

```bash
kubectl get deploy -A -l app.ceres.io/depends-on-postgresql=true \
  -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,REPLICAS:.spec.replicas,READY:.status.readyReplicas
```

Expected result: `REPLICAS` is `0` for all listed Deployments.

#### A2) Merge/Apply PostgreSQL Image Updates

Proceed with PostgreSQL image update merge once app scale-down is confirmed.

#### A3) Verify CNPG Clusters Are Healthy

```bash
kubectl get clusters.postgresql.cnpg.io -A
```

#### A4) Post-Upgrade App Scale-Up

Current default for these apps is one instance each.

1. Validate which Deployments will be scaled up (dry run).

```bash
kubectl get deploy -A -l app.ceres.io/depends-on-postgresql=true -o json \
| yq -r '.items[] | "kubectl -n " + .metadata.namespace + " scale deploy " + .metadata.name + " --replicas=1"'
```

2. Execute the generated scale-up commands.

```bash
kubectl get deploy -A -l app.ceres.io/depends-on-postgresql=true -o json \
| yq -r '.items[] | "kubectl -n " + .metadata.namespace + " scale deploy " + .metadata.name + " --replicas=1"' \
| sh
```

3. Verify Deployments are healthy.

```bash
kubectl get deploy -A -l app.ceres.io/depends-on-postgresql=true \
  -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,REPLICAS:.spec.replicas,READY:.status.readyReplicas
```

Expected result: `REPLICAS=1` and `READY=1` (or equal to desired replicas) for each Deployment.

### Workflow B: CNPG Hibernation for Other Maintenance

Use this when you intentionally want all CNPG database Pods stopped.

Recommended sequence:

1. Scale down PostgreSQL-dependent apps.
2. Hibernate CNPG clusters.
3. Perform maintenance that does not require running PostgreSQL Pods.
4. Unhibernate clusters and verify health.
5. Scale applications back up.

#### B1) Hibernate All CNPG Clusters

1. Dry run: print hibernation commands (no changes made).

```bash
kubectl get clusters.postgresql.cnpg.io -A -o json \
| yq -r '.items[] | "kubectl -n " + .metadata.namespace + " annotate cluster.postgresql.cnpg.io " + .metadata.name + " --overwrite cnpg.io/hibernation=on"'
```

2. Execute hibernation.

```bash
kubectl get clusters.postgresql.cnpg.io -A -o json \
| yq -r '.items[] | "kubectl -n " + .metadata.namespace + " annotate cluster.postgresql.cnpg.io " + .metadata.name + " --overwrite cnpg.io/hibernation=on"' \
| sh
```

3. Verify annotation:

```bash
kubectl get clusters.postgresql.cnpg.io -A \
  -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,HIBERNATION:.metadata.annotations.cnpg\.io/hibernation
```

#### B2) Unhibernate All CNPG Clusters

1. Dry run: print unhibernate commands.

```bash
kubectl get clusters.postgresql.cnpg.io -A -o json \
| yq -r '.items[] | "kubectl -n " + .metadata.namespace + " annotate cluster.postgresql.cnpg.io " + .metadata.name + " --overwrite cnpg.io/hibernation=off"'
```

2. Execute unhibernate.

```bash
kubectl get clusters.postgresql.cnpg.io -A -o json \
| yq -r '.items[] | "kubectl -n " + .metadata.namespace + " annotate cluster.postgresql.cnpg.io " + .metadata.name + " --overwrite cnpg.io/hibernation=off"' \
| sh
```

3. Verify cluster Pods return:

```bash
kubectl get pods -A -l cnpg.io/cluster
```

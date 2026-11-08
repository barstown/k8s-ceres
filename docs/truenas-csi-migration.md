# Migrating from democratic-csi to truenas-csi

How to replace the four democratic-csi `freenas-api-*` drivers with a single
[truenas-csi](https://github.com/truenas/truenas-csi) deployment, and how to move
existing volumes onto it.

democratic-csi's `freenas-api` driver reads `zfs.zvolBlocksize` from the
deployment config only, so each tuned class needs its own deployment. truenas-csi
takes tuning per StorageClass, so one release serves all four classes.

Volumes cannot be handed from one CSI driver to the other. Every volume is
**copied** to a new PV, so each workload has a short outage while its data moves.

## What is staged

| Path                                                              | Purpose                                                                                          |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `kubernetes/apps/truenas-csi/kustomization.yaml`                  | Creates the namespace. `./truenas-csi/ks.yaml` is commented out, so nothing deploys yet          |
| `kubernetes/apps/truenas-csi/truenas-csi/ks.yaml`                 | Flux Kustomization, depends on `snapshot-controller`                                             |
| `kubernetes/apps/truenas-csi/truenas-csi/app/helmrepository.yaml` | Chart source. Not published to OCI yet ([#83](https://github.com/truenas/truenas-csi/issues/83)) |
| `kubernetes/apps/truenas-csi/truenas-csi/app/helmrelease.yaml`    | Driver, StorageClasses, Talos patch, metrics                                                     |
| `kubernetes/apps/truenas-csi/truenas-csi/app/secret.sops.yaml`    | API key, currently a placeholder                                                                 |
| `.mise/config.toml`                                               | Adds `pv-migrate`                                                                                |

## Class mapping

| democratic-csi class           | truenas-csi class          | volblocksize | PVCs |
| ------------------------------ | -------------------------- | ------------ | ---- |
| `freenas-api-iscsi` (default)  | `truenas-iscsi`            | 16K          | 34   |
| `freenas-api-iscsi-postgresql` | `truenas-iscsi-postgresql` | 8K           | 4    |
| `freenas-api-iscsi-sqlite`     | `truenas-iscsi-sqlite`     | 4K           | 7    |
| `freenas-api-nfs`              | `truenas-nfs`              | n/a          | 1    |

The iSCSI classes keep the current behaviour: xfs, `noatime`, thin zvols
(`sparse`), 4096-byte extents, `Retain`, and online expansion. `truenas-nfs`
keeps `nfsvers=3,noatime,nolock` and uses `nfs.rootSquash: "false"`, which
matches the current `maproot root` share. No truenas-csi class is the default
until the end of the migration.

New volumes go under `storage/k8s/truenas-csi/iscsi` and
`storage/k8s/truenas-csi/nfs`, apart from democratic-csi's `storage/k8s/iscsi/v`
and `storage/k8s/nfs/v`.

Re-run the inventory before starting, since these counts are a point in time:

```bash
kubectl get pvc -A -o json | jq -r '.items[] | [
  .spec.storageClassName // "-",
  .metadata.namespace + "/" + .metadata.name,
  .spec.resources.requests.storage,
  (.metadata.labels["app.kubernetes.io/managed-by"] // "-")
] | @tsv' | sort | column -t
```

## Decide the TrueNAS address first

[truenas-storage-path-migration.md](truenas-storage-path-migration.md) moves
TrueNAS from `10.0.10.50` to `10.0.50.9`, and most of its work is recreating PVs
by hand because the address is stored in each PV. This migration creates a new PV
for every dynamic volume anyway.

If the new address is ready, point truenas-csi at it and this migration replaces
steps 1, 2, 5, and 6 of that doc. Steps 3 (DNS and S3) and 7 (static NFS PVs)
still apply, because the static PVs never belonged to democratic-csi.

The address is in four values in `helmrelease.yaml`:

```yaml
truenas:
    url: wss://10.0.10.50/api/current
    nfsServer: 10.0.10.50
    iscsiPortal: 10.0.10.50:3260
```

The staged files use the current address, `10.0.10.50`.

## Known issues

Check these upstream before starting, as some may be fixed by then.

- **[#80](https://github.com/truenas/truenas-csi/issues/80), `/etc/iscsi` on
  Talos.** The node DaemonSet's `iscsi-dir` hostPath fails its type check on
  Talos. The HelmRelease's `postRenderers` patch points it at `/var/iscsi` with no
  type check, which matches what democratic-csi uses here
  (`iscsiDirHostPath: /var/iscsi`). The driver runs `iscsiadm` in the host mount
  namespace, so nothing reads that mount. Drop the patch once upstream fixes it.
- **[#86](https://github.com/truenas/truenas-csi/issues/86), stage and unstage
  failures on Talos.** A stray regular file under `/var/lib/iscsi/nodes/`, or a
  stale lock in `/run/lock/iscsi`, makes every `NodeStageVolume` and
  `NodeUnstageVolume` fail with `exit status 6` (`ISCSI_ERR_IDBM`) or a timeout.
  Pods stick in `ContainerCreating` and stale `VolumeAttachment`s cause
  `Multi-Attach` errors. The canary in step 4 checks for this.
- **[#42](https://github.com/truenas/truenas-csi/issues/42), no detached
  snapshots or clones.** Snapshot restores and clones are ZFS clones that stay
  tied to their origin. Nothing here uses VolumeSnapshots today, so the
  VolumeSnapshotClass is left disabled.
- **[#12](https://github.com/truenas/truenas-csi/issues/12), API key scope.**
  TrueNAS cannot scope an API key to a dataset. Sharing Admin can create datasets
  and shares, but deleting datasets needs Full Admin. With `Retain` on every class
  the driver never deletes datasets, so Sharing Admin may be enough, but this has
  not been tested here.
- **No IPv6 iSCSI portals.** Not an issue on this network.

## Prerequisites

- [ ] TrueNAS SCALE 25.10 or later.
- [ ] Parent datasets created on TrueNAS. The driver does not create them:
      `storage/k8s/truenas-csi`, `storage/k8s/truenas-csi/iscsi`, and
      `storage/k8s/truenas-csi/nfs`. Set compression on the parent if you want it
      inherited, since the classes do not set `compression`.
- [ ] A dedicated TrueNAS user with an API key (see #12 above for the role).
- [ ] iSCSI portal listening on the address chosen above, and NFS enabled with
      NFSv3.
- [ ] `snapshot-controller` running. It lives in the `democratic-csi` namespace,
      and `ks.yaml` depends on it.
- [ ] `mise install`, for `pv-migrate`.
- [ ] `kubectl cnpg` plugin installed, for step 7.
- [ ] Recent ZFS snapshots of `storage/k8s/iscsi/v` and `storage/k8s/nfs/v`.

## 1. Add the API key

```bash
sops kubernetes/apps/truenas-csi/truenas-csi/app/secret.sops.yaml
```

Replace `REPLACE_WITH_TRUENAS_API_KEY` under `stringData.api-key`.

## 2. Enable the deployment

In `kubernetes/apps/truenas-csi/kustomization.yaml`, uncomment:

```yaml
- ./truenas-csi/ks.yaml
```

Commit, push, and reconcile:

```bash
flux reconcile ks truenas-csi -n truenas-csi --with-source
flux get hr -n truenas-csi
kubectl get pods -n truenas-csi -o wide
kubectl get csidriver csi.truenas.io
kubectl get sc
```

The node pods must reach `3/3` on every node. If they stay in
`ContainerCreating`, describe one: a `FailedMount` on `iscsi-dir` means the
postRenderer patch did not apply.

Confirm the controller connected to TrueNAS:

```bash
kubectl logs -n truenas-csi deploy/truenas-csi-controller -c csi-controller --tail=50
```

## 3. Canary

Provision one volume per class before moving anything. Nothing existing is
touched, so a failure here costs nothing.

```yaml
# canary.yaml
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
    name: tcsi-canary
    namespace: default
spec:
    accessModes: ["ReadWriteOnce"]
    storageClassName: truenas-iscsi-sqlite
    resources:
        requests:
            storage: 1Gi
---
apiVersion: v1
kind: Pod
metadata:
    name: tcsi-canary
    namespace: default
spec:
    containers:
        - name: canary
          image: busybox
          command: ["sh", "-c", "sleep 3600"]
          volumeMounts:
              - name: data
                mountPath: /data
    volumes:
        - name: data
          persistentVolumeClaim:
              claimName: tcsi-canary
```

```bash
kubectl apply -f canary.yaml
kubectl wait --for=condition=Ready pod/tcsi-canary -n default --timeout=180s
kubectl exec -n default tcsi-canary -- sh -c 'echo ok > /data/test && cat /data/test'
kubectl exec -n default tcsi-canary -- sh -c 'mount | grep /data'     # expect xfs, noatime
```

On TrueNAS, check that the zvol landed under `storage/k8s/truenas-csi/iscsi` with
the class's `volblocksize` (4K here).

Repeat with `truenas-iscsi`, `truenas-iscsi-postgresql`, and `truenas-nfs` (set
`accessModes: ["ReadWriteMany"]` for NFS).

### Exercise the stage and unstage path

#86 shows up on unstage, so a canary that only mounts does not prove much. With
the canary pod running:

```bash
# restart the node plugin under a mounted volume
NODE=$(kubectl get pod -n default tcsi-canary -o jsonpath='{.spec.nodeName}')
kubectl delete pod -n truenas-csi -l app=truenas-csi-node --field-selector spec.nodeName="$NODE"

# move the pod to another node
kubectl delete pod -n default tcsi-canary
kubectl cordon "$NODE"
kubectl apply -f canary.yaml
kubectl wait --for=condition=Ready pod/tcsi-canary -n default --timeout=180s
kubectl exec -n default tcsi-canary -- cat /data/test    # still "ok"
kubectl uncordon "$NODE"

# no failed unstages or stale attachments
kubectl logs -n truenas-csi -l app=truenas-csi-node -c csi-node --tail=200 | grep -iE 'unstage|exit status|deadline'
kubectl get volumeattachments | grep csi.truenas.io
```

### Clean up

Every class uses `Retain`, so the PV and the zvol survive PVC deletion:

```bash
kubectl delete -f canary.yaml
kubectl get pv | grep Released      # delete the canary PVs
```

Then delete the leftover zvols or datasets and their iSCSI targets, extents, or
NFS shares on TrueNAS.

## 4. Trial

Move one or two small, easily rebuilt apps first, such as `prowlarr` and
`gatus-sidecar` on `truenas-iscsi-sqlite`, using step 5. Leave them for at least
a week, and include a node drain and a Talos upgrade in that time, since those
are the events that exercise unstage. Continue only if nothing got stuck.

## 5. Migrate an app-template volume

This covers the PVCs that app-template creates from `persistence.<name>`, which is
most of them. A PVC's `storageClassName` is immutable, so the old PVC is replaced
by a new one with **the same name** on the new class. Keeping the name means the
git change is only the `storageClass` line, and Helm still owns the PVC.

The old PV is bound to a temporary `<pvc>-old` claim so that pv-migrate can read
it. It stays untouched until the app is verified, which makes it the rollback.

Migrate one namespace at a time, moving all of its volumes together.

### Stop the workload

```bash
NS=prowlarr
PVC=prowlarr
NEW_SC=truenas-iscsi-sqlite

flux suspend ks "$NS" -n "$NS"
flux suspend hr "$NS" -n "$NS"

kubectl get deploy,sts -n "$NS" -o json \
  | jq -r '.items[] | [.kind, .metadata.name, .spec.replicas] | @tsv' | tee "$NS-replicas.tsv"
kubectl scale deploy,sts -n "$NS" --all --replicas=0
```

The HelmRelease must stay suspended until step "Restart". A Helm upgrade
recreates a missing PVC as a new, empty volume.

Wait until no pods use the volume and its attachment is gone:

```bash
kubectl get pods -n "$NS"
OLD_PV=$(kubectl get pvc -n "$NS" "$PVC" -o jsonpath='{.spec.volumeName}')
kubectl get volumeattachments -o json \
  | jq -r --arg pv "$OLD_PV" '.items[] | select(.spec.source.persistentVolumeName == $pv) | .metadata.name'
```

### Export and recreate

```bash
kubectl get pv "$OLD_PV" -o jsonpath='{.spec.persistentVolumeReclaimPolicy}'   # must be Retain

kubectl get pvc -n "$NS" "$PVC" -o json > "pvc-$PVC.orig.json"

# the new PVC: same name, labels, and Helm annotations, on the new class
jq --arg sc "$NEW_SC" '
  del(.status, .spec.volumeName, .metadata.uid, .metadata.resourceVersion,
      .metadata.creationTimestamp, .metadata.managedFields, .metadata.finalizers,
      .metadata.annotations["pv.kubernetes.io/bind-completed"],
      .metadata.annotations["pv.kubernetes.io/bound-by-controller"],
      .metadata.annotations["volume.beta.kubernetes.io/storage-provisioner"],
      .metadata.annotations["volume.kubernetes.io/storage-provisioner"])
  | .spec.storageClassName = $sc
' "pvc-$PVC.orig.json" > "pvc-$PVC.new.json"

# a temporary claim bound to the old PV
jq --arg pv "$OLD_PV" '
  {apiVersion, kind,
   metadata: {name: (.metadata.name + "-old"), namespace: .metadata.namespace},
   spec: (.spec | del(.dataSource, .dataSourceRef) | .volumeName = $pv)}
' "pvc-$PVC.orig.json" > "pvc-$PVC.old.json"
```

Keep these files until the app is verified. They are the record of which PV held
the data.

```bash
kubectl delete pvc -n "$NS" "$PVC"
kubectl patch pv "$OLD_PV" --type json -p '[{"op":"remove","path":"/spec/claimRef"}]'

kubectl create -f "pvc-$PVC.old.json"
kubectl create -f "pvc-$PVC.new.json"

kubectl get pvc -n "$NS"
```

`$PVC-old` must be `Bound` to `$OLD_PV`, and `$PVC` must be `Bound` to a new PV
whose `spec.csi.driver` is `csi.truenas.io`. Stop if not.

### Copy

```bash
pv-migrate --source-namespace "$NS" --source "$PVC-old" \
           --dest-namespace "$NS" --dest "$PVC"
```

Both PVCs are in the same namespace, so pv-migrate uses its `mount` strategy: one
pod mounts both volumes and runs rsync locally, with no network path. Ownership
and permissions are preserved. Its pod adds only `SYS_CHROOT`, which the baseline
PodSecurity level allows.

pv-migrate refuses a destination smaller than the source. If a copy is
interrupted, re-running it resumes, because rsync only sends what differs.

### Point git at the new class

Change `storageClass: freenas-api-iscsi-sqlite` to `storageClass: truenas-iscsi-sqlite`
under the app's `persistence` in its `helmrelease.yaml`, then commit and push.

The live PVC already has that class, so the Helm upgrade finds nothing to change
on it.

### Restart

```bash
flux resume ks "$NS" -n "$NS"
flux resume hr "$NS" -n "$NS"
flux get hr -n "$NS"
```

The Helm upgrade normally restores the replica counts. If not, restore them from
`$NS-replicas.tsv` with `kubectl scale`. Confirm the app has its existing data,
not a first-run screen.

### Clean up

After the app has run correctly for a while:

```bash
kubectl delete pvc -n "$NS" "$PVC-old"
kubectl delete pv "$OLD_PV"
```

Then delete the old zvol, its target, and its extent on TrueNAS. The zvol name
matches `$OLD_PV` under `storage/k8s/iscsi/v`.

### Rollback

Before the clean-up, the old data is untouched:

```bash
flux suspend ks "$NS" -n "$NS"
flux suspend hr "$NS" -n "$NS"
kubectl scale deploy,sts -n "$NS" --all --replicas=0

kubectl delete pvc -n "$NS" "$PVC" "$PVC-old"
kubectl patch pv "$OLD_PV" --type json -p '[{"op":"remove","path":"/spec/claimRef"}]'
jq 'del(.status, .metadata.uid, .metadata.resourceVersion,
        .metadata.creationTimestamp, .metadata.managedFields)' \
  "pvc-$PVC.orig.json" | kubectl create -f -
kubectl get pvc -n "$NS" "$PVC"       # Bound to $OLD_PV
```

Revert the git change, then resume as in "Restart". The truenas-csi PV is left
`Released`; delete it and its zvol once the rollback is verified.

## 6. Special cases

### Operator-managed volumes

`grafana/grafana-pvc` and `victoria-metrics/vmsingle-victoria-metrics` follow
step 5, but their operators recreate PVCs, so stop them first as described in
[truenas-storage-path-migration.md](truenas-storage-path-migration.md#stop-the-workload).
Their classes are set in `kubernetes/apps/grafana/grafana/instance/grafana.yaml`
and `kubernetes/apps/victoria-metrics/victoria-metrics/app/helmrelease.yaml`.

`vmalertmanager` comes from a StatefulSet `volumeClaimTemplate`, which cannot be
changed in place. It only holds silences and notification state, so the simplest
path is to let it start empty: change the class in git, then delete the
StatefulSet and its PVC so the operator recreates both. Re-create any silences
you still need.

Do these last, so monitoring stays up while the other apps move.

### Flux-managed PVCs

`immich/immich-library-pvc` (`truenas-nfs`, 100Gi RWX) is defined in
`kubernetes/apps/immich/immich/app/persistentvolumeclaim.yaml`, not by Helm.
Follow step 5, with these differences:

- Suspend the `immich` Kustomization before deleting the PVC, as Flux would
  recreate it on the old class.
- The new PVC has no Helm annotations to carry over, which is fine.
- Change `storageClassName` in that file instead of a HelmRelease.

It is the largest copy, so let it run in the cluster instead of tying it to your
terminal:

```bash
pv-migrate --source-namespace immich --source immich-library-pvc-old \
           --dest-namespace immich --dest immich-library-pvc \
           --id immich-library --detach
pv-migrate status immich-library
pv-migrate cleanup immich-library     # after it completes
```

### Renovate

Its PVC is used by a CronJob. Suspend it first with
`kubectl patch cronjob renovate -n renovate -p '{"spec":{"suspend":true}}'`, wait
for any running Job to finish, and resume it at the end.

## 7. Migrate CloudNativePG clusters

Do not copy these with pv-migrate. Have CNPG rebuild each instance from the
primary instead: a changed `spec.storage.storageClass` only applies to PVCs that
CNPG creates afterwards, so replacing instances moves them.

| Cluster                                           | Old class                      | New class                  |
| ------------------------------------------------- | ------------------------------ | -------------------------- |
| `audiomuse-ai`, `immich`                          | `freenas-api-iscsi-postgresql` | `truenas-iscsi-postgresql` |
| `authentik`, `homebox`, `lubelogger`, `semaphore` | `freenas-api-iscsi`            | `truenas-iscsi`            |

All six run two instances, so each has no replica while a clone runs.

1. Change `storage.storageClass` in the app's `cluster.yaml`, commit, and push.
   If Flux reports that the CNPG webhook rejected the change, stop and check the
   CNPG docs for the installed version.
2. Replace the instances, using `immich` as the example:

    ```bash
    kubectl cnpg status -n immich immich-postgres        # note the primary; must be healthy

    kubectl cnpg destroy -n immich immich-postgres 2     # replica; replaced on the new class
    kubectl cnpg status -n immich immich-postgres        # wait for 2/2 and streaming

    kubectl cnpg promote -n immich immich-postgres immich-postgres-3   # brief write outage
    kubectl cnpg destroy -n immich immich-postgres 1
    ```

    Instance numbers vary; use the ones `status` shows.

3. Confirm both PVCs are on the new class:

    ```bash
    kubectl get pvc -n immich -l cnpg.io/cluster=immich-postgres
    ```

The old PVs stay `Released`. Delete them, then their zvols on TrueNAS.

## 8. Switch the default class

Once every app is on truenas-csi, change the default in one commit, so the
cluster never has two:

- `kubernetes/apps/democratic-csi/freenas-api-iscsi/app/helmrelease.yaml`:
  `defaultClass: false`
- `kubernetes/apps/truenas-csi/truenas-csi/app/helmrelease.yaml`:
  `isDefault: true` on `truenas-iscsi`

```bash
kubectl get sc    # exactly one (default)
```

## 9. Remove democratic-csi

Confirm nothing still uses it:

```bash
kubectl get pv -o json | jq -r '.items[]
  | select(.spec.csi.driver != null and (.spec.csi.driver | startswith("freenas-api")))
  | [.metadata.name, .status.phase, (.spec.claimRef.namespace // "-") + "/" + (.spec.claimRef.name // "-")] | @tsv'
```

Only `Released` PVs you have not cleaned up yet should be listed. Then:

1. Move `snapshot-controller` out of the `democratic-csi` namespace, for example
   to `kube-system`, and update `dependsOn` in
   `kubernetes/apps/truenas-csi/truenas-csi/ks.yaml`. truenas-csi still needs it.
2. Remove the four `freenas-api-*` entries from
   `kubernetes/apps/democratic-csi/kustomization.yaml`, then the directories.
3. The namespace has `prune: disabled`, so delete it by hand once empty.
4. Delete the remaining zvols, datasets, targets, extents, and shares under
   `storage/k8s/iscsi` and `storage/k8s/nfs` on TrueNAS.
5. The "Process registry/tag dependencies" custom manager in `.renovaterc.json5`
   only matches the democratic-csi HelmReleases today. Remove it if nothing else
   has started using `registry:` and `tag:` pairs.

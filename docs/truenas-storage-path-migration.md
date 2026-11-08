# Repointing democratic-csi at a new TrueNAS address

How to move existing iSCSI and NFS volumes to a different TrueNAS address.

The target address is recorded **per PV**, not read from the driver config at
attach time, so changing the driver config alone only affects newly provisioned
volumes and controller operations. Existing PVs cannot be patched, so each one
has to be deleted and recreated.

Worked example below moves storage from `10.0.10.50` to `10.0.50.9`.

No Talos or topf changes are needed as long as the workers already have a route
to the new address. They carry `10.0.50.0/24` as a connected route on `bond0`,
so nothing on the node side has to change.

## Choose a path

- **Keep the old address as an alias.** Do the prerequisites, steps 1 and 2, and
  optionally step 3. Existing volumes keep using the old address and new volumes
  use the new one. There is little downtime, but the old address stays in service.
- **Retire the old address.** Do every step. Each workload has downtime while its
  volumes move, but this can be spread over time because both addresses work
  throughout.

## What holds the address

| Location | Count | Change |
|---|---|---|
| Driver configs | 4 secrets | Edit, then restart controllers (step 1) |
| `truenas.lab.internal` DNS record | 1 | Update the record (step 3) |
| CSI PVs: `spec.csi.volumeAttributes.portal` (iSCSI), `.server` (NFS) | 43 + 1 | Recreate or re-clone (steps 5, 6) |
| Static NFS PVs: `spec.nfs.server` in git | 10 | Edit git and recreate (step 7) |

The 54 PVCs fall into these groups. The ownership decides the procedure:

| Owner | PVCs | Procedure |
|---|---|---|
| Helm (app-template) | 30 | Step 5 |
| Flux, dynamic (`immich/immich-library-pvc`) | 1 | Step 5 |
| Operators (`grafana-pvc`, `vmsingle-victoria-metrics`, `vmalertmanager-...-0`) | 3 | Step 5, after stopping the operator |
| CloudNativePG (`authentik`, `homebox`, `immich`, `lubelogger`, `semaphore`) | 10 | Step 6 |
| Flux, static NFS (`*-media`, `syncthing-backups-kyle`) | 10 | Step 7 |

Re-run the inventory before starting, since these counts are a point in time:

```bash
kubectl get pvc -A -o json | jq -r '.items[] | [
  .metadata.namespace + "/" + .metadata.name,
  (.metadata.labels["app.kubernetes.io/managed-by"] // "-"),
  (.metadata.labels["kustomize.toolkit.fluxcd.io/name"] // "-"),
  ((.metadata.ownerReferences // []) | map(.kind) | join(",") | if . == "" then "-" else . end),
  .spec.volumeName
] | @tsv' | column -t
```

## Prerequisites

- [ ] New address reachable and outside the DHCP scope.
- [ ] Added to the iSCSI **portal** binding in TrueNAS. Portals bind to specific
      addresses; if the portal does not listen on the new one it will not answer
      on 3260. This is the most common cause of a failed cutover.
- [ ] Added to the NFS share's allowed hosts/networks.
- [ ] Permitted in any authorized-networks or initiator-group restriction.
- [ ] No second default gateway on TrueNAS — the new interface needs a connected
      route only.
- [ ] S3 on port 30292 answers on the new address, and its TLS certificate is
      still valid for `truenas.lab.${INTERNAL_DOMAIN}`.
- [ ] Recent ZFS snapshots of the zvols and datasets being moved.
- [ ] `kubectl cnpg` plugin installed, for step 6.

## 1. Driver configs

Four sops-encrypted configs hold the target address, each as a single
`driver-config-file.yaml` blob:

```
kubernetes/apps/democratic-csi/freenas-api-iscsi/app/secret.sops.yaml
kubernetes/apps/democratic-csi/freenas-api-iscsi-postgresql/app/secret.sops.yaml
kubernetes/apps/democratic-csi/freenas-api-iscsi-sqlite/app/secret.sops.yaml
kubernetes/apps/democratic-csi/freenas-api-nfs/app/secret.sops.yaml
```

Edit each with `sops` and repoint:

- `iscsi.targetPortal` (and `targetPortals` if populated) for the iSCSI drivers.
- `nfs.shareHost` for the NFS driver.
- `httpConnection.host` for all four, if it uses the IP rather than a hostname.

Confirm the key names on decrypt rather than assuming them.

Reloader is deployed but these releases carry no reloader annotation, so the
secret change restarts nothing on its own:

```bash
kubectl rollout restart deploy -n democratic-csi \
  freenas-api-iscsi-democratic-csi-controller \
  freenas-api-iscsi-postgresql-democratic-csi-controller \
  freenas-api-iscsi-sqlite-democratic-csi-controller \
  freenas-api-nfs-democratic-csi-controller
```

## 2. Canary

Provision one volume against the new address before moving anything. At this
point existing volumes still point at the old address, so a failure here costs
nothing and rollback is reverting the secrets.

Each driver has its own config, so repeat this for all four storage classes. For
`freenas-api-nfs`, set `accessModes` to suit and read `.server` instead of
`.portal` below.

```yaml
# canary.yaml
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: portal-canary
  namespace: default
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: freenas-api-iscsi
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: portal-canary
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
        claimName: portal-canary
```

```bash
kubectl apply -f canary.yaml
kubectl wait --for=condition=Ready pod/portal-canary -n default --timeout=180s

# write and read back
kubectl exec -n default portal-canary -- sh -c 'echo ok > /data/test && cat /data/test'

# confirm the PV was provisioned against the new address
kubectl get pv -o json \
  | jq -r '.items[] | select(.spec.claimRef.name=="portal-canary") | .spec.csi.volumeAttributes.portal'

# confirm the connection actually went there
NODE=$(kubectl get pod -n default portal-canary -o jsonpath='{.spec.nodeName}')
IP=$(kubectl get node "$NODE" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
talosctl -n "$IP" netstat | awk '$6 ~ /:(3260|2049)$/ {print $6, $7}' | sort | uniq -c
```

Cleanup — note all storage classes use `reclaimPolicy: Retain`, so the PV and
the backing zvol survive PVC deletion and must be removed by hand:

```bash
kubectl delete -f canary.yaml
kubectl get pv | grep Released          # delete the released PV
```

Then delete the leftover zvol and extent on TrueNAS.

## 3. Other consumers of the TrueNAS address

`truenas.lab.internal` resolves to the old address. These use that name rather
than a PV, so they are unaffected by the PV steps:

- CloudNativePG backups (the five `objectstore.yaml` files, port 30292)
- `talos-backup` (S3 endpoint, port 30292)
- `blackbox-exporter` probes (DNS, 3260, and 2049)

Update the DNS record once the new address serves S3. After that, the blackbox
probes on 3260 and 2049 test the new address while unmigrated volumes still use
the old one, so they will not show a problem with the old path.

Then run one CloudNativePG backup and one Talos backup to confirm both still work.

## 4. PV recreation rules

**Existing PVs cannot be patched.** `spec.csi.volumeAttributes` and `spec.nfs` are
part of `spec.persistentVolumeSource`, which the API server rejects as immutable:

```
spec.persistentvolumesource: Forbidden: spec.persistentvolumesource is immutable after creation
```

Each volume has to be deleted and recreated. All PVs use
`persistentVolumeReclaimPolicy: Retain`, so the backing zvol or dataset survives
deletion of the Kubernetes objects and the data is preserved. Confirm that first,
because deleting a dynamically provisioned PV under `Delete` would destroy its data:

```bash
kubectl get pv -o json \
  | jq -r '.items[] | select(.spec.persistentVolumeReclaimPolicy != "Retain") | .metadata.name'
```

**Nothing may recreate a dynamic PVC before its PV is back.** A PVC created without
`volumeName` while the PV is missing makes the driver provision a **new, empty
volume**, and the app starts on it as though it were freshly installed. Flux,
Helm, and the operators can all recreate PVCs, so stop whichever owns the PVC, and
restore the PV before the PVC.

Flux suspension is also required to hold workloads at zero replicas. Resuming does
not restore replicas here, because no HelmRelease has drift detection enabled, so
scale back up explicitly.

## 5. Migrate Helm, Flux dynamic, and operator volumes

Migrate one namespace at a time, moving all of its volumes together. Leave
`victoria-metrics` and `grafana` until last, so that monitoring stays available
while the other apps move.

### Stop the workload

```bash
NS=radarr
flux suspend ks "$NS" -n "$NS"
flux suspend hr "$NS" -n "$NS"

# record replica counts, then scale down
kubectl get deploy,sts -n "$NS" -o json \
  | jq -r '.items[] | [.kind, .metadata.name, .spec.replicas] | @tsv' | tee "$NS-replicas.tsv"
kubectl scale deploy,sts -n "$NS" --all --replicas=0
```

The Flux object names usually match the namespace, but not always. Check them with
`flux get ks -n "$NS"` and `flux get hr -n "$NS"`.

These need extra steps:

- **`renovate`** runs as a CronJob. Suspend it with
  `kubectl patch cronjob renovate -n renovate -p '{"spec":{"suspend":true}}'` and
  wait for any running Job to finish.
- **`grafana`** is reconciled by its operator, which would recreate its PVC and
  undo the scale-down. Suspend ks `grafana` and ks `grafana-instance`, and hr
  `grafana-operator`, then
  `kubectl scale deploy grafana-operator -n grafana --replicas=0` before stopping
  the workload.
- **`victoria-metrics`** has the same problem. Suspend ks `victoria-metrics` and hr
  `victoria-metrics-k8s-stack`, then
  `kubectl scale deploy victoria-metrics-k8s-stack-victoria-metrics-operator -n victoria-metrics --replicas=0`.
  Alerting is down until this finishes. `vmalertmanager` is the only StatefulSet
  with `volumeClaimTemplates`.

Wait until no pods use the volumes and their attachments are gone. Deleting a PV
with a live attachment is what this wait is there to prevent.

```bash
kubectl get pods -n "$NS"
kubectl get volumeattachments -o json | jq -r --arg ns "$NS" --argjson pvs \
  "$(kubectl get pvc -n "$NS" -o json | jq '[.items[].spec.volumeName]')" \
  '.items[] | select(.spec.source.persistentVolumeName as $p | $pvs | index($p)) | .metadata.name'
```

### Export the manifests

Keep these files until the workload is verified, because they are the only
record of which PV belongs to which claim.

```bash
OLD=10.0.10.50 NEW=10.0.50.9
PVC=radarr
PV=$(kubectl get pvc -n "$NS" "$PVC" -o jsonpath='{.spec.volumeName}')

kubectl get pv "$PV" -o json | jq --arg old "$OLD" --arg new "$NEW" '
  del(.status, .metadata.uid, .metadata.resourceVersion,
      .metadata.creationTimestamp, .metadata.managedFields,
      .metadata.annotations["pv.kubernetes.io/bound-by-controller"],
      .spec.claimRef.uid, .spec.claimRef.resourceVersion)
  | .spec.csi.volumeAttributes |= with_entries(
      if .key == "portal" or .key == "server" then .value |= sub($old; $new) else . end)
' > "pv-$PVC.json"

kubectl get pvc -n "$NS" "$PVC" -o json | jq '
  del(.status, .metadata.uid, .metadata.resourceVersion,
      .metadata.creationTimestamp, .metadata.managedFields,
      .metadata.annotations["pv.kubernetes.io/bind-completed"],
      .metadata.annotations["pv.kubernetes.io/bound-by-controller"])
' > "pvc-$PVC.json"

# review: new address, claimRef name and namespace, and the same volumeHandle
jq '{attrs: .spec.csi.volumeAttributes, claim: .spec.claimRef, handle: .spec.csi.volumeHandle}' "pv-$PVC.json"
```

If `portals` is populated, update it by hand. It is empty on all current PVs.

Keeping `spec.volumeName` and the Helm labels and annotations in the PVC means it
binds to the same PV and stays owned by its release.

### Recreate

Delete the PVC, then the PV. The order matters: the `pv-protection` finalizer
blocks PV deletion while it is still bound. Create them in the reverse order.

```bash
kubectl delete pvc -n "$NS" "$PVC"
kubectl delete pv "$PV"

kubectl create -f "pv-$PVC.json"
kubectl create -f "pvc-$PVC.json"
kubectl get pvc -n "$NS" "$PVC"      # must be Bound to the same $PV
```

Stop if the PVC binds to a different PV: the workload would start on an empty
volume.

### Restart

```bash
# restore the counts recorded in $NS-replicas.tsv
kubectl scale deploy/radarr -n "$NS" --replicas=1

flux resume hr "$NS" -n "$NS"
flux resume ks "$NS" -n "$NS"
```

Restart any operator you stopped and resume its Flux objects. Confirm the app has
its existing data, not a first-run screen, before moving on.

## 6. Migrate CloudNativePG volumes

Do **not** hand-delete these PVCs or recreate these PVs. Have each instance
re-clone from the primary instead, which needs no PV surgery. All five clusters
have two instances, so each one has no replica while the clone runs.

Step 1 must be complete, since the new instance gets a volume from the driver
config. Do this for each cluster, using `immich` as the example:

```bash
kubectl cnpg status -n immich immich-postgres       # note the primary; must be healthy

# 1. replace the replica; the new instance gets a new number and the new address
kubectl cnpg destroy -n immich immich-postgres 2

# wait for 2/2 ready and streaming replication
kubectl cnpg status -n immich immich-postgres

# 2. move the primary to the new instance (a brief write outage)
kubectl cnpg promote -n immich immich-postgres immich-postgres-3

# 3. replace the old primary, now a replica
kubectl cnpg destroy -n immich immich-postgres 1
```

Instance numbers vary: `semaphore`, for example, currently runs instances 1 and 3.

The old PVs stay behind as `Released`. Once the cluster is healthy on the new
address, delete them and then the matching zvols and extents on TrueNAS.

## 7. Migrate static NFS volumes

These are in git, so the manifests carry the address:

```
kubernetes/apps/{immich,kapowarr,kavita,lidarr,navidrome,radarr,sabnzbd,sonarr,syncthing}/*/app/persistentvolume.yaml
```

`syncthing` has two PVs. All other apps have one.

Flux cannot apply a changed `server` to an existing PV: the apply fails as immutable,
and that blocks the app's whole Kustomization until the PV is gone. No Kustomization
sets `force`, and it would not help here because `pv-protection` still holds the PV.
So remove the PV first, and let Flux recreate both objects. These PVCs set
`volumeName` in git, so they cannot bind to a newly provisioned volume.

For apps that also have iSCSI volumes, do this during the same outage as step 5.

```bash
NS=radarr
flux suspend ks "$NS" -n "$NS"
flux suspend hr "$NS" -n "$NS"
kubectl scale deploy,sts -n "$NS" --all --replicas=0

kubectl delete pvc -n "$NS" radarr-media
kubectl delete pv radarr-media
```

Change `server` in that app's `persistentvolume.yaml`, then commit, push, and:

```bash
flux resume ks "$NS" -n "$NS"
kubectl get pvc -n "$NS" radarr-media       # must be Bound
kubectl scale deploy/radarr -n "$NS" --replicas=1
flux resume hr "$NS" -n "$NS"
```

Push the manifests one app at a time, or suspend every app in the batch before
pushing, so that no unsuspended Kustomization hits the immutable error.

## 8. Verify and retire the old address

Volumes not yet migrated keep using the old address and continue to work. Once a
workload is scaled down, its iSCSI session or NFS mount ends and the restart
connects to the new address, so no drain or reboot is needed.

Mixed state during the migration is fine: TrueNAS serves the same targets on both
addresses, and each iSCSI volume is attached to one node at a time.

Stale records under `/var/lib/iscsi/nodes/` pointing at the old address may be left
behind but are unused. No manual `iscsiadm` cleanup is needed.

### Before removing the old address

```bash
# no PV references the old address
kubectl get pv -o json | jq -r '.items[]
  | select([.spec.csi.volumeAttributes.portal, .spec.csi.volumeAttributes.server, .spec.nfs.server]
           | any(. != null and startswith("10.0.10.50")))
  | .metadata.name'

# no connections on the old address, on each of ceres-w1 (10.0.50.16),
# ceres-w2 (10.0.50.20), and ceres-w3 (10.0.50.18)
talosctl -n <ip> netstat | awk '$6 ~ /:(3260|2049)$/ {print $6, $7}' | sort | uniq -c

# no NFS mounts from the old address
talosctl -n <ip> read /proc/mounts | awk '$3 ~ /^nfs/ {print $1}' | sort -u

# no iSCSI errors
talosctl -n <ip> dmesg | grep -iE 'ping timeout|conn error|recovery timed out'
```

The old PV list must be empty apart from `Released` PVs you have not cleaned up
yet. If a connection persists with no PV behind it, drain and reboot that worker:

```bash
kubectl drain ceres-w3 --ignore-daemonsets --delete-emptydir-data
talosctl -n 10.0.50.18 reboot
# wait for Ready, verify, then
kubectl uncordon ceres-w3
```

Also confirm that all PVCs are bound, that no `KubePodNotReady` or
`KubeContainerWaiting` alerts are firing, and that backups still succeed.

Remove the old address from the iSCSI portal, the share configuration, and the
TrueNAS interface, then keep watching for a day.

## Rollback

While the old address stays live, rollback is repointing, and no data is at risk:

- **Before step 5:** revert the four driver configs and restart the controllers.
  Revert the DNS record if you changed it.
- **Migrated CSI volumes:** repeat step 5 with `OLD` and `NEW` swapped.
- **Migrated static NFS volumes:** repeat step 7 with the git change reverted.
- **CloudNativePG:** no action is needed while the new address is up. To move
  back, revert the driver configs and repeat step 6.

Volumes provisioned on the new address, including re-cloned CloudNativePG instances,
need the same treatment if you are abandoning the new address entirely.

Once the old address has been removed from TrueNAS, re-add it before attempting a
rollback.

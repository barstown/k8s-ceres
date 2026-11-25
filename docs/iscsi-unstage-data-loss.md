# iSCSI Volumes Losing Recent Writes on Unstage

Investigated 2026-11-24, during the Talos v1.14.6 ➔ v1.14.7 rollout, and updated 2026-11-25. Root cause not fully found (see Open Questions).

### What Happened

After the rollout, zigbee2mqtt crash-looped on start:

```
[BACKUP] Coordinator backup is corrupted ... (SyntaxError: Unexpected end of JSON input)
```

`/config/coordinator_backup.json` was 0 bytes. The fix was to move it aside and restart. zigbee2mqtt rebuilt it from the coordinator ("Adapter network matches config", "resumed"), and no devices needed re-pairing.

### Cause

Three things combined:

1. **The app wrote without `fsync`.** zigbee-herdsman writes the backup to a `.tmp` file and renames it over the real one. It never calls `fsync`, so the new data sits in the node's page cache for up to about 30 seconds before reaching disk. `database.db` is written with `fsync` and survived.
2. **democratic-csi logged out while XFS was still mounted.** `NodeUnstageVolume` (v1.9.5) runs plain `umount` on the staging path, then `iscsiadm ... -u` (logout). The gap between the two has been between 0.14s (vmsingle) and 3.2s (zigbee2mqtt). It doesn't flush the filesystem or check that the device is unused. Sometimes `umount` returns while something still holds the filesystem open in the kernel. The logout then removes the disk under a live XFS, and any data not yet written back is lost.
3. **XFS kept the rename but not the data.** The rename reached the XFS journal, but the file contents were still in the page cache. On the next mount XFS replayed its log, which left the renamed file at 0 bytes.

TrueNAS was not at fault. The lost data never left the Kubernetes node, so zvol settings like `sync=always` can't help.

### Timeline (UTC, 2026-11-24)

CSI times come from democratic-csi node logs in VictoriaLogs. Kernel times come from `talosctl dmesg`, which ran about 0.93s behind the CSI logs on ceres-w3.

PV: `pvc-79539cfa-654c-4845-9104-8594973bb281` (zigbee2mqtt `config`, `freenas-api-iscsi`, XFS)

| Time | Node | Event |
| --- | --- | --- |
| 20:59:29 | ceres-w3 | tuppr starts the first drain of w3 |
| 20:59:33.9 | ceres-w3 | zigbee2mqtt writes `database.db` (with `fsync`, survived) |
| 20:59:34.2 | ceres-w3 | zigbee2mqtt writes `coordinator_backup.json` (no `fsync`, lost) |
| 20:59:49.770 | ceres-w3 | democratic-csi `umount .../7370127b.../globalmount` |
| 20:59:52.943 | ceres-w3 | democratic-csi `iscsiadm ... -u` (logout). **The data is lost here.** |
| 21:09:29 | ceres-w3 | First drain fails: the `authentik/authentik-postgres-2` PDB blocks eviction. tuppr uncordons the node |
| 21:09:32 | ceres-w3 | tuppr starts a second drain |
| 21:09:49 | ceres-w3 | tuppr: "Waiting for volumes to detach before reboot" (18 volumes), clear within about 1s |
| 21:09:50 | ceres-w3 | tuppr creates the upgrade job; the reboot follows |
| 21:11:59 | ceres-w3 | Next mount after the reboot: `XFS (sdc): Starting recovery` |
| 21:27:56 | ceres-w3 | Reproduced on scale-to-0: kernel logs `Block device removal (0x20) detected ... Shutting down filesystem` before `Unmounting Filesystem` |
| 21:31:03 | ceres-w3 | Clean unstage of the same PV for comparison: `Unmounting Filesystem` logged as soon as `umount` ran |

Volumes moved off w3 during the 20:59 drain also needed recovery when mounted on w2 at 20:59:45 and 20:59:52. That was before w3 rebooted, so the power-cycle didn't cause this.

**tuppr was ruled out.** The data was lost at the 20:59:52 logout, about 10 minutes before the reboot. The scale-to-0 at 21:27 reproduced it with no tuppr involved. tuppr only moves the most pods at once, and every pod move is an unstage. `policy.waitForVolumeDetach` (which tuppr already applied here) waits for detaches to finish, but can't make one safe.

**The PDB stall made it worse.** The first drain evicted pods (and unstaged their volumes) for 10 minutes, then gave up on the authentik Postgres PDB and uncordoned the node, so the second drain moved pods again. That doubled the unstages without making progress. The TalosUpgrade now has a CNPG health check that waits until every cluster is healthy and spread across nodes before moving on (#1838).

### Second Incident: vmsingle (2026-11-25)

This happened during the rollout of resource requests (#1839), when vmsingle was rescheduled from ceres-w3 to ceres-w2.

PV: `pvc-25577ce3-4c79-4334-b4be-d956b01145fa` (victoria-metrics/`vmsingle-victoria-metrics`, 50Gi, `freenas-api-iscsi`, XFS). It was attached to w3 as `sdh` at 15:10:07.

| Time (UTC) | Source | Event |
| --- | --- | --- |
| 16:41:48.492 | CSI | `NodeUnpublishVolume` |
| 16:41:48.718 | CSI | `NodeUnstageVolume` call |
| 16:41:48.856 | kernel | `device offline error, dev sdh ... op 0x1:(WRITE)` |
| 16:41:48.857 | kernel | `sdh: writeback error on inode 67240474` and `67240476` (offsets 0 to about 11 MiB) |
| 16:41:48.860 | kernel | `XFS (sdh): log I/O error -19`, then `Filesystem has been shut down due to log error` |
| 16:41:49.213 | kernel | `XFS (sdh): Unmounting Filesystem` (after the device was already gone) |
| 16:41:50.068 | CSI | `NodeUnstageVolume` response |
| 16:42:11 | vmsingle (w2) | `invalid cache at /victoria-metrics-data/cache/*: cannot read maxBucketChunks from ".../metadata.bin": EOF` for 4 caches |

This is the strongest evidence so far. Unlike the zigbee2mqtt case, the kernel itself reported that it had dirty pages it could not write because the device was already offline. The cache metadata files were left empty, the same 0-byte result as `coordinator_backup.json`. VictoriaMetrics rebuilds those caches on its own, and its data parts are written with `fsync`, so no metrics were lost. A data file written without `fsync` would have been lost the same way.

In the same minute w2 shows the same signature on other volumes: `sdb` and `sdo` hit `log I/O error -19`, and `sds` logged `Block device removal`.

### How Common

Each mount that needs XFS log recovery (`XFS (...): Starting recovery`) is a volume that was cut off while still mounted.

| When | Event | Mounts needing recovery |
| --- | --- | --- |
| 2026-11-24 | Talos v1.14.7 rollout (since the workers' last boot) | 13 of 53 |
| 2026-11-25 | Worker drains for the RAM upgrade | 19 of 34 (4/12 and 15/22) |
| 2026-11-25 | Rollout of resource requests (#1839) | 12 of 31 (w2: 9 of 9) |

Roughly a third of all pod moves cut a volume off while it was still mounted. Apps that `fsync` (Postgres, SQLite) survive this. Apps that don't, like the zigbee2mqtt backup, lose whatever was still unwritten: up to about 35 seconds of writes with the kernel defaults, about 6 seconds with the sysctls below.

To check a node:

```sh
talosctl -n <node-ip> dmesg | grep -E 'XFS.*(Starting recovery|Block device removal|Unmounting)'
```

A healthy unstage logs `Unmounting Filesystem` with no `Block device removal` line before it.

### Mitigations

**Write data back to disk sooner (applied 2026-11-25, `talos/all/40-sysctls.yaml`).** Linux holds written data in memory for up to about 35s by default. Shortening that shrinks the loss window for every volume, with no PVC changes:

```yaml
vm.dirty_expire_centisecs: "500"       # write back data older than 5s (default 30s)
vm.dirty_writeback_centisecs: "100"    # flusher wakes every 1s (default 5s)
vm.dirty_background_bytes: "67108864"  # 64Mi: start background writeback (replaces the 10% ratio)
vm.dirty_bytes: "268435456"            # 256Mi: throttle writers (replaces the 20% ratio)
```

The byte limits are sized for the current 1GbE storage link: 64Mi is about 0.6s of transfer and 256Mi about 2.3s. After moving to 10GbE, raise them to 128Mi / 1Gi. The worst-case age of unwritten data drops from about 35s to about 6s. That would have saved the zigbee2mqtt backup, which was written about 18 seconds before the logout. It doesn't help when the logout follows a write within a second or two. vmsingle flushes its caches on shutdown, so its lost cache files were probably that recent.

**`sync` mount option (small, important apps only).** Every write waits until TrueNAS has it, which fully closes the gap. Writes get much slower. Add `sync` to an existing PV's `spec.mountOptions`, or set `node.mount.mount_flags` for a whole democratic-csi release. It takes effect the next time the volume is staged, with no rebuild.

**Not helpful:**
- XFS `wsync` forces metadata such as renames to disk, not file contents.
- TrueNAS zvol settings only protect data TrueNAS has already received.
- Switching to [truenas-csi](truenas-csi-migration.md): its iSCSI unstage also unmounts and logs out straight away, with no flush.

### Follow-ups

**SELinux labels on CSI volumes (needs more testing).** SELinux is permissive, so nothing is blocked. But pods writing to PVs log a denial for every directory write: `pod_t` → `unlabeled_t` on iSCSI volumes, and `network_fs_t` on NFS. On the workers that is roughly 0.4–25 denials per second, enough to overflow the kernel audit backlog (`audit: rate limit exceeded`, about 52k records lost on w1). The plan is to mount volumes with a fixed label, which needs no relabeling or PV migration:

```
context=system_u:object_r:kubelet_state_t:s0
```

`kubelet_state_t` is the label Talos gives emptyDir volumes, and pods write to those without denials. Tested on `grafana/grafana-pvc` on 2026-11-25. democratic-csi passed the option to `mount`, but libmount in the driver container stripped it before calling `mount(2)`. libmount drops SELinux options when libselinux reports SELinux as disabled, and libselinux requires `/etc/selinux/config` to exist. The democratic-csi image has only `semanage.conf`, and Talos has no config file either. The option was removed from the PV again afterwards.

Next attempt:

1. Mount a ConfigMap containing `SELINUX=permissive` and `SELINUXTYPE=targeted` at `/etc/selinux/config` in the `csi-driver` container of all four democratic-csi releases (`node.extraVolumes` plus `node.driver.extraVolumeMounts`).
2. Check with a dry run in the container (`LIBMOUNT_DEBUG=all mount -f -t xfs -o context=... <dev> <dir>`): the "fixed options" line should keep `context=`.
3. Re-add the option to the Grafana PV, remount it, and confirm the mount shows `kubelet_state_t` and Grafana's denials stop.
4. Add the option to the StorageClasses (their `mountOptions` can't be changed in place, so each StorageClass is deleted and recreated) and to existing PVs. Existing PVs pick it up on their next mount, so let that happen during a planned drain instead of forcing remounts. Every remount is an unstage.

The chart also renders `csiDriver.seLinuxMount`, but it is undocumented and relies on the kubelet working out a label from each pod's `seLinuxOptions` and the OS's `lxc_contexts`. Talos doesn't ship `lxc_contexts`, so it isn't a fit here.

**Older PVs without `noatime`.** 27 of the 47 democratic-csi PVs (all `freenas-api-iscsi`, created up to 2026-05-31) have no `mountOptions`. They were provisioned before the StorageClass got `noatime`. Review whether to patch it in. A PV's `spec.mountOptions` can be changed in place and applies on the next mount. To list them:

```sh
kubectl get pv -o json | jq -r '.items[]
  | select(.spec.csi.driver // "" | startswith("freenas"))
  | select((.spec.mountOptions // []) | index("noatime") | not)
  | [.metadata.creationTimestamp[:10], .spec.storageClassName, .spec.claimRef.namespace + "/" + .spec.claimRef.name] | @tsv'
```

### Open Questions

- **What keeps XFS alive after `umount`?** Every current process on w3 sees the volume through shared or slave mounts (none hold a private copy), so a dying container's mount namespace is the leading suspect. To catch it, look for `/dev/sdX` in `/proc/*/mountinfo` at the moment of unstage.
- **Upstream fix.** Worth reporting to democratic-csi with the 21:27 vs 21:31 evidence. The fix is to run `sync -f` on the staging path before `umount`, and to refuse to log out while the block device still has a live filesystem.

# iSCSI Volumes Losing Recent Writes on Unstage

Investigated 2026-11-24, during the Talos v1.14.6 ➔ v1.14.7 rollout. Root cause not fully found (see Open Questions).

### What Happened

After the rollout, zigbee2mqtt crash-looped on start:

```
[BACKUP] Coordinator backup is corrupted ... (SyntaxError: Unexpected end of JSON input)
```

`/config/coordinator_backup.json` was 0 bytes. The fix was to move it aside and restart. zigbee2mqtt rebuilt it from the coordinator ("Adapter network matches config", "resumed"), and no devices needed re-pairing.

### Cause

Three things combined:

1. **The app wrote without `fsync`.** zigbee-herdsman writes the backup to a `.tmp` file and renames it over the real one. It never calls `fsync`, so the new data sits in the node's page cache for up to about 30 seconds before reaching disk. `database.db` is written with `fsync` and survived.
2. **democratic-csi logged out while XFS was still mounted.** `NodeUnstageVolume` (v1.9.5) runs plain `umount` on the staging path, then `iscsiadm ... -u` (logout) about 0.15 seconds later. It doesn't flush the filesystem or check that the device is unused. Sometimes `umount` returns while something still holds the filesystem open in the kernel. The logout then removes the disk under a live XFS, and any data not yet written back is lost.
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
| 20:59:52.943 | ceres-w3 | democratic-csi `iscsiadm ... -u` (logout) |
| 21:11:59 | ceres-w3 | Next mount after the reboot: `XFS (sdc): Starting recovery` |
| 21:27:56 | ceres-w3 | Reproduced on scale-to-0: kernel logs `Block device removal (0x20) detected ... Shutting down filesystem` before `Unmounting Filesystem` |
| 21:31:03 | ceres-w3 | Clean unstage of the same PV for comparison: `Unmounting Filesystem` logged as soon as `umount` ran |

Volumes moved off w3 during the 20:59 drain also needed recovery when mounted on w2 at 20:59:45 and 20:59:52. That was before w3 rebooted, so the power-cycle didn't cause this.

### How Common

Since the workers' last boot, 13 of 53 PVC mounts needed XFS log recovery (`XFS (...): Starting recovery`). Each of those volumes was cut off while still mounted. Apps that `fsync` (Postgres, SQLite) survive this. Apps that don't, like the zigbee2mqtt backup, can lose writes from the last 30 seconds.

To check a node:

```sh
talosctl -n <node-ip> dmesg | grep -E 'XFS.*(Starting recovery|Block device removal|Unmounting)'
```

A healthy unstage logs `Unmounting Filesystem` with no `Block device removal` line before it.

### Mitigations

**Write data back to disk sooner (recommended).** Linux holds written data in memory for up to 30s by default. Shortening that shrinks the loss window for every volume, with no PVC changes. Add to the Talos machine config:

```yaml
machine:
  sysctls:
    vm.dirty_expire_centisecs: "300"     # write out data older than 3s (default 3000)
    vm.dirty_writeback_centisecs: "100"  # check every 1s (default 500)
```

This would have saved the zigbee2mqtt backup, which was written about 18 seconds before the logout. It doesn't help when the logout follows a write within a second or two.

**`sync` mount option (small, important apps only).** Every write waits until TrueNAS has it, which fully closes the gap. Writes get much slower. Add `sync` to an existing PV's `spec.mountOptions`, or set `node.mount.mount_flags` for a whole democratic-csi release. It takes effect the next time the volume is staged, with no rebuild.

**Not helpful:**
- XFS `wsync` forces metadata such as renames to disk, not file contents.
- TrueNAS zvol settings only protect data TrueNAS has already received.
- Switching to [truenas-csi](truenas-csi-migration.md): its iSCSI unstage also unmounts and logs out straight away, with no flush.

### Open Questions

- **What keeps XFS alive after `umount`?** Every current process on w3 sees the volume through shared or slave mounts (none hold a private copy), so a dying container's mount namespace is the leading suspect. To catch it, look for `/dev/sdX` in `/proc/*/mountinfo` at the moment of unstage.
- **Upstream fix.** Worth reporting to democratic-csi with the 21:27 vs 21:31 evidence. The fix is to run `sync -f` on the staging path before `umount`, and to refuse to log out while the block device still has a live filesystem.

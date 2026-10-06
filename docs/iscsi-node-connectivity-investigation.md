## iSCSI Worker Connectivity Investigation Runbook

This runbook adds continuous, per-node probing so we can prove whether iSCSI
mount failures are caused by network path instability, TrueNAS iSCSI service
issues, or node-local behavior.

### What was added

A standalone monitoring app at:

- `kubernetes/apps/blackbox-exporter/`

with a dedicated namespace:

- `blackbox-exporter`

It deploys a Blackbox Exporter as a **DaemonSet** on `ceres-w*` nodes and
scrapes two checks from each node:

- TCP connect to TrueNAS iSCSI target (`:3260`)
- ICMP reachability to the same TrueNAS host

It also defines alerting rules that classify likely failure types.

### Why this helps isolate root cause

By checking from each worker node continuously, we get per-node history of
loss/latency on the exact iSCSI path.

- **TCP 3260 fails + ICMP succeeds (same node):** likely TrueNAS iSCSI daemon,
  firewall/L4, or session-level issue (not pure L3 loss).
- **TCP + ICMP both fail on one worker:** likely node path/routing/switching issue.
- **TCP + ICMP both fail on all workers:** likely TrueNAS host/network outage.

### Before enabling

1. Confirm worker hostnames in:

   - `kubernetes/apps/blackbox-exporter/iscsi-node-probe/app/helmrelease.yaml`

2. Confirm TrueNAS endpoint in:

   - `kubernetes/apps/blackbox-exporter/iscsi-node-probe/app/podmonitor.yaml`

   Current defaults use `10.0.10.50` and `10.0.10.50:3260`
   (with `truenas.lab.internal` comments as FQDN alternatives).

### Enabling (when you are ready)

Merge/push this app and reconcile Flux.

### Key metrics and labels

- `probe_success{check="truenas-iscsi-tcp", node="ceres-wX"}`
- `probe_success{check="truenas-icmp", node="ceres-wX"}`
- `probe_duration_seconds{check="truenas-iscsi-tcp", node="ceres-wX"}`

### Suggested PromQL during incidents

Per-node iSCSI success ratio over 15 minutes:

```promql
avg_over_time(probe_success{check="truenas-iscsi-tcp"}[15m])
```

Compare TCP vs ICMP by node:

```promql
avg_over_time(probe_success{check=~"truenas-(iscsi-tcp|icmp)"}[15m])
```

Per-node iSCSI probe latency:

```promql
avg_over_time(probe_duration_seconds{check="truenas-iscsi-tcp"}[15m])
```

Global availability signal (all monitored workers):

```promql
sum(avg_over_time(probe_success{check="truenas-iscsi-tcp"}[5m]))
```

### Notes

- This change set is design/implementation only. Nothing is applied until you
  merge and reconcile.
- Alerting rules are intentionally conservative to reduce noise while still
  capturing long enough failures to correlate with failed mounts.

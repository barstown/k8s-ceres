## Cluster Hardening

Plan for hardening the cluster, in priority order. As of 2026-11-14.

### Current State

The biggest gap is that any pod can reach any other pod: the repo has no network policies. The cluster is 6 Talos v1.14.6 nodes (3 control plane, 3 workers) on 10.0.50.0/24, running Cilium in native routing mode with Envoy Gateway in front.

| Area | Today | Gap |
| --- | --- | --- |
| Pod-to-pod access | No `NetworkPolicy` or `CiliumNetworkPolicy` | Unrestricted lateral movement |
| Traffic between nodes | Cilium, no encryption | Plain text on the node LAN |
| App login | Each app's own auth; authentik deployed but not in front of apps | No SSO, uneven login strength |
| Edge | CrowdSec bouncer on `envoy-external` | Covers the external gateway only |
| Pod security | Talos PodSecurity admission; `kube-system` privileged | Not reviewed per namespace |
| Visibility | Hubble disabled | Hard to write and debug policies |

### 1. Cilium: WireGuard Encryption and Hubble (Easy Win)

This encrypts all pod traffic between nodes, using one Helm values change and no app changes. It keeps traffic private from anything else on the node network. It does not prove which workload is talking (see mutual auth below).

Change in `kubernetes/apps/kube-system/cilium/app/helmrelease.yaml`:

```yaml
encryption:
  enabled: true
  type: wireguard
```

Before rolling out:

- Checked for Cilium 1.20.4: DSR with Geneve dispatch is supported with WireGuard; only non-Geneve DSR leaves redirected north-south traffic unencrypted. Current setting: `loadBalancer.mode: dsr` with `dsrDispatch: geneve`.
- Not now: `encryption.nodeEncryption: true` would also encrypt host-network traffic, but Cilium still marks it beta and skips control-plane nodes by default, so 3 of 6 nodes would stay plain text. Revisit when it leaves beta.
- Expect a short interruption to traffic between nodes as the agents restart; apply at a quiet time.
- Verify with `cilium encrypt status` in an agent pod. Talos includes WireGuard in its kernel.

#### Hubble (Same Change)

Hubble shows every flow Cilium handles: source and destination pod, port, DNS and HTTP details, and whether policy allowed or dropped it. It lets you write network policies from real traffic and see what a new policy broke.

```yaml
hubble:
  enabled: true
  relay:
    enabled: true
  ui:
    enabled: true
  metrics:
    enabled: [dns, drop, tcp, flow, port-distribution, icmp, httpV2]
    serviceMonitor:
      enabled: true
```

- It needs the same agent restart as WireGuard, so one maintenance window covers both.
- After rollout, `hubble observe --verdict DROPPED` shows whether anything broke.
- Hubble UI shows all cluster traffic: route it on `envoy-internal` only and put it behind authentik.
- Cost: a relay pod, a UI pod and some agent memory. Flows are kept in a small in-memory buffer, not stored.

### 2. Network Policies (Biggest Impact)

Move each namespace to default deny, then allow only the flows it needs. This contains a compromised app to its own namespace.

1. Use Hubble (section 1) to see each namespace's real traffic before writing any rules.
2. Write a reusable kustomize component in `kubernetes/components/`: default deny, plus allows for DNS, Envoy gateway ingress and Prometheus scraping.
3. Add per-app allows for its database, outbound internet and the APIs it calls (for example Prowlarr → Sonarr).
4. Roll out one namespace at a time, starting with `authentik` (Postgres reachable only from authentik pods).
5. Last, add cluster-wide rules: block pod access to the node LAN and the Talos/Kubernetes APIs except where needed.

`CiliumNetworkPolicy` can also filter by HTTP path and method where a single port needs finer control.

### 3. SSO with authentik

Put authentik in front of apps so there is one login, with MFA and group-based access.

- **Apps with OIDC** (Grafana, Immich, Semaphore, Homebox, Kavita): use an authentik OIDC provider.
- **Apps without OIDC** (Sonarr, Radarr, Lidarr, Prowlarr, SABnzbd, Tautulli, Changedetection): use a proxy provider in forward auth mode, plus an Envoy Gateway `SecurityPolicy` with `extAuth` on the app's HTTPRoute. No Istio needed.
- Keep `/api` paths that use API keys outside the proxy, and set each app's own auth to External.
- Fail closed: no `failOpen` on these policies.
- Package the policy as a kustomize component so enabling it is one line per app.

### Deferred: Cilium Mutual Authentication

Deferred while it is beta. It is the closest match to mesh-style mTLS. Cilium uses SPIRE to give each workload a cryptographic identity, and a policy with `authentication.mode: required` makes both ends prove it before traffic flows.

When to revisit:

- It leaves beta in a Cilium release, or its known limitations no longer affect this cluster.
- Network policies (section 2) are in place, since mutual auth is enforced through them.
- The SPIFFE/SPIRE review below is done.

### To Discuss: SPIFFE and SPIRE

This section only lists questions; we'll review them separately. SPIFFE is the standard for workload identity (IDs such as `spiffe://ceres/ns/sonarr/sa/sonarr` and the certificates that prove them). SPIRE is the server and agent that issue them.

- [ ] How SPIRE proves a workload's identity (node and workload attestation) and what it trusts
- [ ] Running SPIRE as Cilium's identity backend versus on its own
- [ ] Where its root CA and data live, and how backup and recovery work
- [ ] What else could use the identities (authentik, Postgres client certs, external services)
- [ ] Running and upgrade cost on Talos

### Checklist

- [ ] Enable Cilium WireGuard encryption and Hubble in one change; verify with `cilium encrypt status` and `hubble observe`
- [ ] Write the default-deny network policy component
- [ ] Lock down the `authentik` namespace first, then the remaining apps
- [ ] Put the first app (Changedetection) behind authentik forward auth
- [ ] Move apps with OIDC support to authentik OIDC
- [ ] Review PodSecurity levels per namespace
- [ ] Review SPIFFE/SPIRE
- [ ] Revisit Cilium mutual auth once it is out of beta

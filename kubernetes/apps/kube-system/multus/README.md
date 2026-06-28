# Multus Macvlan Configuration

## Overview

This configuration uses Multus with macvlan to provide pods with a secondary
network interface on a dedicated VLAN. This is useful for pods that need to be
reachable on a specific subnet without requiring the node itself to have an IP
on that network.

## How It Works

Macvlan creates a virtual network interface with its own MAC and IP address,
independent of the node's primary interface. Traffic flows directly between the
pod's macvlan interface and the network — the node's IP stack is bypassed.

## Requirements

- Multus CNI installed on the cluster
- Switch port configured to allow the target VLAN
- Parent interface operational (no IP needed on the VLAN)

## NetworkAttachmentDefinition

```yaml
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: iot
  namespace: kube-system
spec:
  config: |-
    {
      "cniVersion": "0.3.1",
      "name": "iot",
      "plugins": [
        {
          "type": "macvlan",
          "master": "ethSel0",
          "mode": "bridge",
          "ipam": {
            "type": "static",
            "routes": [
              {"dst": "0.0.0.0/0", "gw": "172.16.100.1"}
            ]
          }
        },
        {
          "type": "sbr"
        }
      ]
    }
```

- master: Uses a Talos LinkAlias for deterministic interface naming
- mode: bridge: Allows macvlan interfaces to communicate with each other
- sbr: Source-based routing for proper multi-homed traffic flow

## Pod Annotation

```yaml
annotations:
  k8s.v1.cni.cncf.io/networks: |-
    [{
      "name": "iot",
      "namespace": "kube-system",
      "ips": ["172.16.100.5/24"],
      "mac": "66:44:5c:b1:83:a6"
    }]
```

Both IP and MAC are specified to ensure consistent addressing across pod
restarts. Generating a Unicast LAA MAC Address Use this command to generate a
random Locally Administered Address (LAA) unicast MAC:

```bash
printf '%02x:%02x:%02x:%02x:%02x:%02x\n' \
  $(( ((RANDOM % 256) | 0x02) & 0xFE )) \
  $(( RANDOM % 256 )) \
  $(( RANDOM % 256 )) \
  $(( RANDOM % 256 )) \
  $(( RANDOM % 256 )) \
  $(( RANDOM % 256 ))
```

The first octet is constrained to have:

- Bit 0 = 0 (unicast)
- Bit 1 = 1 (locally administered)

This produces valid MAC addresses like 66:44:5c:b1:83:a6 that can be freely used
without an OUI from IEEE.

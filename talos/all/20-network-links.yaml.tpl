---
apiVersion: v1alpha1
kind: LinkAliasConfig
name: ethSel0
selector:
  match: glob("{{ .Node.Data.macAddr }}", mac(link.hardware_addr))
---
apiVersion: v1alpha1
kind: BondConfig
name: bond0
links:
  - ethSel0
bondMode: active-backup
miimon: 100
mtu: {{ .Node.Data.mtu }}
addresses:
  - address: "{{ .Node.IP }}/24"
routes:
  - gateway: "10.0.50.1"
{{- if eq .Node.Role "control-plane" }}
---
apiVersion: v1alpha1
kind: Layer2VIPConfig
link: bond0
name: "10.0.50.2"
{{- end }}
{{- if .Node.Data.macAddr2 }}
---
apiVersion: v1alpha1
kind: LinkAliasConfig
name: ethSel1
selector:
  match: glob("{{ .Node.Data.macAddr2 }}", mac(link.hardware_addr))
---
apiVersion: v1alpha1
kind: BondConfig
name: bond1
links:
  - ethSel1
bondMode: active-backup
miimon: 100
mtu: {{ .Node.Data.mtu }}
---
apiVersion: v1alpha1
kind: VLANConfig
name: bond1.100
vlanID: 100
parent: bond1
{{- end }}

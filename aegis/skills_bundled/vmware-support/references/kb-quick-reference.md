# KB Quick Reference

A starter set of widely used public Broadcom KB articles, ranked by how many separate case
conversations each appeared in. URL pattern: `https://knowledge.broadcom.com/external/article/<ID>`
Legacy 4–7 digit IDs resolve via `knowledge.broadcom.com/external/article?legacyId=<id>`.

## Certificates, SSO and identity

| KB | Subject |
|---|---|
| 342206 | Generate valid default self-signed certificates (vRLI ↔ vCenter connectivity) |
| 320837 | Using the lsdoctor tool |
| 83276 | Manual Machine SSL certificate replacement (legacy) |
| 373385 | Certificate management / vCenter login failure from expired SSL cert |
| 322249 | Replace certificates on vCenter Server |
| 369576 | vCenter lookupsvc may fail to start |
| 318946 | How to use vSphere Certificate Manager |
| 344201 | Verify and resolve expired vCenter certificates |
| 318968 | Checking expiration of the STS certificate |
| 385107 | vCert — scripted vCenter expired certificate recovery |
| 326288 | Removing CA certificates from the trusted store |
| 76719 | STS certificate recovery (legacy) |
| 2097936 | vSphere Certificate Manager usage (legacy) |

## Patching, updates and upgrades

| KB | Subject |
|---|---|
| 86447 | Upgrading vCenter Server 7.0 fails during precheck (legacy) |
| 370882 | Patching vCenter Server to 8.0 U3 fails |
| 370115 | Patching vCenter Server Appliance via CLI |
| 373968 | vLCM Config Manager is enabled on this cluster |
| 87258 | Troubleshooting vCenter upgrade stuck issues (legacy) |
| 345514 | Service startup sequence for vCenter patching |
| 372863 | Quick guide to upgrade vCenter Server |
| 381723 | Component downgrade error during ESXi host remediation |
| 326316 | Build numbers and versions of VMware vCenter |
| 313460 | Upgrading vCenter or ESXi 8.0 fails — weak certificate signature |
| 320702 | VAMI returns "update installation in progress" (source vCenter only) |
| 390098 / 390121 | Depot token and entitlement failures |

## The update/precheck rule catalogue

`VCU-001…011` cover the patch path: staging loops (376278), stage-path/package discrepancy (318581),
token entitlement 403s (391459), microservice timeout (345487), wrong ISO (443189), **partial-RPM
"all packages already installed" (382874 — critical hard stop)**, false disk-space (422619),
**VAMI INSTALL_IN_PROGRESS source-only (320702 — critical)**, Remote Console ISO (407105), real
storage exhaustion (378466), and 9.0.1 missing EULA/manifest (431697).

`VCP-001…008` cover upgrade precheck: SSO cert retrieval (371912), weak SHA-1 signatures (313460),
expired BACKUP_STORE certs (425134), EAM URL/trust (344775), legacy STS_INTERNAL_SSL_CERT (320656),
IP-based PNID blocking 9.0 GA (414238), broken trusted-root chain (433350), and leaf-published-as-CA
(414687).

## vSAN, storage and hosts

| KB | Subject |
|---|---|
| 319977 | Troubleshooting vSAN network issues |
| 344893 | vCLS VMs do not deploy following a vSAN shutdown |
| 316577 | Datastore conflicts with an existing datastore |
| 313077 | vCenter storage.log is full or low |
| 344682 | Troubleshooting an ESXi host in a Not Responding state |
| 318647 | ESXi host disconnects intermittently from vCenter |
| 323612 | ESXi host disconnects — UDP 902 heartbeat blockage |
| 317904 | PSOD backtrace troubleshooting |
| 370008 | Understanding vSAN performance issues |
| 314365 | Investigating virtual machine file locks |
| 339691 | Determining why a VM was powered off or restarted |
| 374025 | vApp VMs in inconsistent state |

## Diagnostics and tooling

| KB | Subject |
|---|---|
| 344917 | Using the VCF Diagnostic Tool (VDT) for vSphere |
| 345059 | Skyline Health Diagnostics for vSphere |
| 330178 | Collecting diagnostic information for VMs |
| 326299 | Collecting diagnostic information for ESXi |
| 312194 | Location of vCenter Server log files |
| 320280 | Restarting the management agents on ESXi |
| 320871 | Increasing heap memory — vCenter memory exhaustion |
| 318571 | Appliance running low on memory |
| 291090 | Workload plugin error: no healthy upstream |

## Broadcom search mechanics

| KB | Subject |
|---|---|
| 200997 | Advanced search options on the Broadcom portal |
| 328937 | Using advanced search features for knowledge |
| 142527 | Escalating Broadcom support cases (one concern per 24h per case) |

Search pattern that works:
`site:knowledge.broadcom.com/external/article "exact error" vCenter <version> <log filename>`

## Horizon / Omnissa boundary

Post-split, Broadcom owns vSphere/vSAN/NSX and Omnissa owns Horizon/Workspace ONE — separate
support orgs, separate portals, **separate interoperability matrices**
(`interopmatrix.broadcom.com` vs `interopmatrix.omnissa.com`). Boundary KBs: 426172 (deploying VMs
from Horizon View), 424991 (invalid cert in Horizon Console), 422830 (NSX/Horizon interop matrix),
439672 (9.x licence unavailable on VVF for VDI), 404632 (AD identity source invalid credentials).

The Connection Server is a **client of vCenter over TCP 443** with a service account and a pinned
certificate thumbprint — so expired credentials, broken trust chains, full `/storage` or an
unreachable vCenter all surface to a Horizon admin as "provisioning is broken".


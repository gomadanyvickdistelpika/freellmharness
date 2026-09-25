---
name: vmware-support
description: >
  This skill should be used for anything involving VMware, Broadcom, vCenter, VCSA, ESXi, VCF,
  SDDC Manager, vSAN, NSX, Aria, SRM, vLCM, vCLS, HCX, Tanzu or Horizon — including "analyse this
  log", "what does this error mean", "find the KB for", "triage this support bundle", "write a
  customer update", "draft a Neo", "escalate to Tier 3", or any question about certificates,
  patching, upgrades, PSODs, APD/PDL, snapshots or the Broadcom knowledge base. Load my-profile first.
metadata:
  version: "1.0.0"
---

# VMware Support

A senior support engineer's method for VMware by Broadcom products. Treat the user as a peer
who may be anywhere from new to expert — check `me__about('career')` for their level.

## Non-negotiable method

**1. Evidence before interpretation.** Run the deterministic analysis first, present its structured findings, *then* interpret. Never invent evidence not in the report.

**2. Fail closed.** If retrieval cannot verify a KB, emit `kb_confidence: none` plus an escalation path. **Empty is honest; a fabricated KB number or URL is a critical failure.** Never cite an adjacent KB as if it were an exact match.

**3. Verify before recommending.** Re-open the actual Broadcom article before presenting any change command. Search by exact redacted error text plus product and version. `knowledge.broadcom.com` is the source of truth; blogs, forums and AI summaries are not.

**4. Orchestrate and hand off.** vCert owns certificates. lsdoctor owns Lookup Service. VDT owns health correlation. Detect, classify and route — never reimplement a vendor-maintained repair engine.

**5. Separate detection from mutation.** Scanning is read-only. Repair is a separate path behind explicit flags, an acknowledgement token, and re-validation at execution time. **Archive, never delete.**

**6. State the failure locus first** in any root cause: vmkernel signatures → host-side. vpxd/sso → vCenter/SSO-side. nsx-proxy → NSX-side. sddc-manager → SDDC-side. APD/PDL without vmkernel PSOD → storage-array-side.

## Resolution quality bar

Every resolution must contain **one of**: an exact build (`ESXi 8.0.3e / 24674464`), an advisory ID (`VMSA-2025-0013`), a named command or script (`vCert.py`, `esxcli storage core path set`), or the exact sentence *"No specific fix build or procedure identified — engineering escalation required."*

Banned: "apply the next patch", "upgrade to a fixed version", "implement the vendor-recommended workaround if one exists".

Any `.vmx` setting, script, certificate or account operation needs an **explicit action verb** (add / remove / set to TRUE / run / revert / regenerate / restart) **plus one sentence on why that direction fixes the symptom.** Direction errors are the classic failure — the `disable_apichv` workaround is to **ADD** the flag, and writing "remove" would be confidently wrong.

For no-KB cases, name the artefacts: host-side → `/var/core` zdumps + `vm-support -w` + vmkernel.log + mboot.log. NSX → Edge tech-support bundle. SDDC → sddc-manager bundle from LCM logs. vCenter → vc-support + vpxd_logs + sso_logs. "Collect logs and escalate" is not acceptable.

## Log triage — always first

If logs or a support bundle appear, triage before analysing. **Extract ZIP bundles first** — pointing a scanner at a `.zip` returns zero matches and looks like a clean system. `Matches: 0` almost always means the extraction step was skipped, not that the logs are clean.

Present per module: name · alert level · matches and **unique days** · exact evidence lines · KB focus. Open with *"The triage report shows…"*. Use qualifiers — *"the signature fired X times over Y unique days"* — never *"this defect absolutely happened on <date>"*.

## Customer-facing artifacts

Six modes, each with a fixed shape: **filled Neo template · outbound customer email · inbound response summary · internal case comment · resolution/soft-close · Tier 3 help request.**

The Broadcom StandardReport skeleton, mined from 82 real case studies:

```
Status · What we reviewed · What we observed · Current assessment
Blocking items · Next action · KB mapping · Closure condition
```

Hard rules: **maximum 2 KBs** in any customer-facing resolution · no invented KBs · strip tokens, passwords, cookies, session IDs, customer names and internal-only comments · never say "root cause confirmed" unless evidence *and* KB cause both align.

**EOGS handling:** vSphere 6.x/7.x/8.x must not get the same answer. Out-of-support means *"best-effort triage only; recommended path is upgrade to a supported target version"* — and that language must **accompany** the technical fix, never replace it.

## Reference

`references/kb-quick-reference.md` holds a starter set of widely used public KB articles.
Always re-open the live article before relying on it.

## Related

`job-search` when the question is really about positioning VMware experience.
`building-and-projects` for tooling built on top of this work.

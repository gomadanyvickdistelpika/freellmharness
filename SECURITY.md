# Security policy

AEGIS runs on people's own PCs, can use their browser and desktop (with approval), and stores
API keys. Security reports are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue.** Use GitHub's private reporting instead:
[Report a vulnerability](https://github.com/gomadanyvickdistelpika/freellmharness/security/advisories/new).

Include what you found, how to reproduce it, and what an attacker could do. You'll get a reply
within 7 days.

## In scope (examples)

- Bypassing the approval gate (a tool that writes, clicks, sends or shares without asking)
- A web page, file or model reply that makes the agent act on its own instructions
  (prompt injection leading to real actions)
- Private "About me" notes or API keys leaking to a model, a log or the network
- The local API being reachable from other machines (it must bind to 127.0.0.1 only)
- File tools escaping their allowed folders

## Supported versions

Only the latest `main` is supported while AEGIS is in early testing.

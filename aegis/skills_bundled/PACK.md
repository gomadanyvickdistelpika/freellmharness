# The skill pack

One profile layer plus specialist skills, so the assistant starts every chat knowing how to
route a request and how to behave.

`my-profile` loads first. It holds the ground rules and the routing table; the facts about
**you** live in Settings → **About me**, which you fill in yourself. Every other skill assumes
it.

| Skill | Covers |
|---|---|
| **my-profile** | Ground rules, privacy tiers, routing — reads your About me notes |
| **coding** | Writing, fixing and running code; Codex-style plan → edit → verify |
| **building-and-projects** | Agents, automation, local LLMs, where work should run |
| **research** | Current facts with sources and dates |
| **planner** | Plans, schedules, reminders, recurring tasks |
| **media-studio** | Images, voice, video, Suno song packs |
| **mcp-builder** | Connecting new services, writing MCP servers, creating agents |
| **job-search** | CVs, cover letters, interviews, outreach — never invents experience |
| **money-and-benefits** | Budgets, tax, benefits — explains, never advises trades |
| **health-and-medical** | Appointment prep and plain-language explanations — never diagnoses |
| **family-and-school** | School letters, meetings, homework help |
| **immigration** | Visas and residence rules for your country, from official sources |
| **forms-and-admin** | Forms and official letters — prepares, you submit |
| **home-car-and-diy** | Repairs, purchases, "is it worth fixing" |
| **creative-music** | Songs, lyrics, creative prompts |
| **fitness** | Training plans that respect injuries |
| **vmware-support** | A senior engineer's VMware/vSphere/VCF triage method |

## Make them yours

- **Change a skill:** copy its folder from `aegis\skills_bundled\` into
  `%LOCALAPPDATA%\Aegis\skills\` and edit the copy. Your copy wins over the bundled one and
  survives updates.
- **Add a skill:** make a new folder there with a `SKILL.md` (same frontmatter: `name`,
  `description`). Or ask the assistant to write one — it lands in *pending* until you approve it
  on the Skills tab.
- **Private skills** (family, health, money, immigration, forms, my-profile) are only sent to a
  cloud model after you approve it, unless you choose "Full profile" in Settings.

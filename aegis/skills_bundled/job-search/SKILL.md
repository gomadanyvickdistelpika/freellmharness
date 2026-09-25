---
name: job-search
description: >
  This skill should be used for anything about the user's career — "tailor my CV", "review my
  LinkedIn", "help me prepare for an interview", "write a cover letter", "should I apply for
  this", "draft a follow-up email", "find companies to approach", freelance outreach, recruiter
  messages, or salary and role questions. Load my-profile first.
metadata:
  version: "1.0.0"
---

# Job search and career

## Before anything

Call `me__about('career')` and `me__about('job-search')`. They hold the user's real history,
target roles, locations and standing rules. If they are empty templates, ask for the CV
(or a paste of it) and the target role before writing anything.

## Non-negotiable honesty

- **Never invent experience, employers, dates, numbers or certifications.** Only use what
  the user's notes or CV say. If a job asks for something they don't have, say so plainly.
- One honest gap paragraph beats three bluffs. Offer the closest real experience instead.
- Quantify only with numbers the user gave you.

## Finding roles

1. Search with the user's target titles, location/remote rules and a recent date window.
2. Open the employer's own careers page or ATS to confirm the role is live — aggregator
   listings go stale.
3. For each role: link, why it fits (evidence from their notes), gaps, verdict
   (apply / stretch / skip). Say how many you checked and how many were rejected, and why.

## Tailoring a CV

- Mirror the job's language only where the user's experience genuinely matches.
- Lead with the 3-5 strongest, most relevant achievements. Cut what doesn't serve this role.
- Keep formatting ATS-friendly: plain headings, no tables or text boxes, standard fonts.

## Cover letters

Three short paragraphs: why this role at this company (specific), the best evidence they can do
it, one honest gap with the adjacent strength. Under 300 words. No clichés ("fast learner",
"passionate team player") without a concrete example.

## Interviews

Prepare: likely questions for this role, a STAR story per core requirement (from the user's
real notes), two questions to ask them, and the honest answer to the hardest gap.

## Outreach

Short, specific, no attachment in the first message. Connect first, pitch later. Track who
was contacted and when, and suggest a follow-up date.

## Salary

Research the range for the role and location with sources and dates. Present it; the decision
is the user's.

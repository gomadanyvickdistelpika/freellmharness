---
name: my-profile
description: >
  This skill should be used at the START of essentially any personal task — it tells the assistant
  who the user is, how they like to work, and which specialist skill to route to. Load it when the
  user says "help me with", "draft", "what should I do about", "explain", or asks anything
  personal, technical, financial, medical, family, legal or career related.
metadata:
  version: "1.0.0"
---

# My profile

This is the user's profile layer. Every other life skill assumes it.

## Where the facts live

The facts about the user are **not in this file**. They live in the **About me** tab
(Settings → About me), as small Markdown notes the user edits themselves:

| Note | Tier | What it holds |
|---|---|---|
| about | public | name, location, languages, one-paragraph summary |
| working-style | public | how they like answers: length, tone, step-by-step or not |
| career | personal | work history and evidence for CVs |
| job-search | personal | target roles, locations, standing rules |
| projects-and-builds | public | what they are building |
| tech-setup | public | their PC, OS, tools |
| interests | public | hobbies and creative work |
| family | private | household, children, school |
| health-fitness | private | health notes, training constraints |
| money-admin | private | budgets, benefits, admin |

Read them with `me__about(topic)`; private ones with `me__private(topic, reason)` — the
user approves before private notes go to a cloud model.

## If the profile is still empty

Notes that still contain `{{placeholders}}` have not been filled in. Do not invent facts
to fill the gaps. Instead:

1. Answer the question as well as you can with sensible, clearly stated defaults.
2. Ask **one** short question for the single most useful missing fact.
3. Suggest once: *"Tip: fill in Settings → About me so I remember this next time."*

## Ground rules (apply to every task)

- Plain language first, detail after. Match the user's language.
- Evidence over guesses. Never invent numbers, links, names, experience or results.
- Ask before consequential actions: sending, paying, deleting, submitting.
- Money, medical and legal: explain, organise and prepare questions — never give a
  trade recommendation, a diagnosis or legal advice.
- Keep private details private: use them only for the task at hand.

## Routing

| The request is about | Load |
|---|---|
| code, scripts, apps, repos | coding |
| agents, automation, local LLMs, tools | building-and-projects |
| CV, cover letter, interviews, jobs | job-search |
| money, tax, benefits, budgets | money-and-benefits |
| symptoms, appointments, health | health-and-medical |
| children, school, homework | family-and-school |
| visas, residence permits, citizenship | immigration |
| forms, official letters | forms-and-admin |
| car, home, repairs, purchases | home-car-and-diy |
| songs, lyrics, creative prompts | creative-music |
| workouts, training, weight | fitness |
| VMware / vSphere / VCF | vmware-support |
| current facts, prices, news | research |
| images, video, voice | media-studio |
| schedules, plans, reminders | planner |
| new MCP servers, new agents | mcp-builder |

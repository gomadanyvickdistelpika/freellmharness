---
name: planner
description: >
  Use for organising work over time — "create a project", "set up a schedule", "remind me
  every", "daily/weekly task", "plan my week", "break this down", roadmaps, to-do lists,
  deadlines. Uses project__ tools for projects and scheduled tasks.
---

# Planner — projects and schedules

## Projects (`project__create`, `project__list`)
Create a project when work will span several sessions (an app, a job-search campaign, a
proposal, an album). Give it a clear name, a one-line goal, and standing instructions. Keep its
`PROJECT.md` current: update **Decisions** and **Next steps** with `files__edit` when they change.

## Scheduled tasks (`project__schedule`, `project__list_schedules`)
- Write the prompt as a complete standalone instruction — the run starts with no memory of this
  chat. Include where to save output and what "done" looks like.
- Cron is local time: `0 8 * * 1-5` weekdays 08:00 · `0 9 * * 1` Mondays 09:00 ·
  `30 7 * * *` daily 07:30 · `0 */4 * * *` every 4 hours.
- Scheduled runs are unattended: tools needing approval are declined unless the task is marked
  trusted. Tell the user this, and that "Register with Windows" runs it while AEGIS is closed.

## Plans
Break goals into steps with an owner-free, verb-first list, rough time per step and the first
action for today. Keep it realistic for a busy parent who is also job-hunting.

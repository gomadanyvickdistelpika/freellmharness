---
name: money-and-benefits
description: >
  This skill should be used when the user asks about money — budgets, tax, benefits and
  allowances, payslips, invoices, freelance rates, shares or pensions, or "can I afford", "how
  much will I take home", "what can I claim". Load my-profile first.
metadata:
  version: "1.0.0"
---

# Money and benefits

## Boundaries

- **Not a financial adviser.** Explain, calculate, organise and prepare questions. Never tell the
  user to buy, sell or hold an investment, and say so if asked.
- Tax and benefit rules change every year and differ by country. **Search for the current
  official rules** (the government or tax authority site for the user's country, from
  `me__about('about')`) and cite them with the year.

## How to help

1. Get the facts: amounts, dates, country, household situation. Private details come from
   `me__private('money-admin', reason)` — only what the task needs.
2. Show the working: a small table of inputs → steps → result. Round sensibly and state
   assumptions.
3. Flag deadlines and documents needed.
4. End with what to confirm with the official body or an adviser.

## Budgets

Income, fixed costs, variable costs, savings — monthly. Highlight the two or three biggest
levers, not a lecture.

## Freelance rates

Day rate = target annual income ÷ realistic billable days (often 150-200), plus costs and tax.
Compare with market ranges found by search, with sources.

## Benefits and allowances

Identify which schemes might apply, the eligibility tests, how to apply, and what evidence is
needed. Link to the official page. Never promise the user will qualify.

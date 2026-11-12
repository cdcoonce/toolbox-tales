# Newcomer test protocol (episode 1)

The pilot's pass condition: someone who has never seen the tooling can say what the tool does after one viewing.

## Who

One newcomer, chosen by Charles and named only privately (never in this repo). They must never have seen the vault, the workshop plugin, or any earlier episode.

## What to show

- `media/episode/episode.mp4`, once, start to finish, with sound.
- No briefing before it beyond "this is a short video; afterwards I'll ask two questions."
- No pausing, rewinding or second viewing. No explanation until both answers are recorded.

## The two questions

Ask in this order, in these words:

1. "What does the tool in the video do?"
2. "What goes wrong without it?"

## Recording

- Write each answer down verbatim, in the newcomer's own words, as they say it. No paraphrase, no cleanup.
- Do not prompt, hint or ask follow-ups before both answers are written. Clarifying questions from the newcomer get "answer however you understood it."

## Judging

Charles judges each answer against the intended meaning:

1. **Tool:** it deliberately breaks things to check that the checks can actually catch them (a check that never goes red is useless).
2. **Without it:** checks can stay green while the problem is really there, so passing means nothing.

The test passes only if both answers match. Partial credit is recorded as a fail with a note.

## Result

Recorded in `newcomer-result.md` next to this file: pass or fail, the two verbatim answers with no name attached, the total spend from `ledger/spend.csv`, and what to change for episode 2.

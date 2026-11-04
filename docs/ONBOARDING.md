# What Kandu does for you

[فارسی](ONBOARDING.fa.md) · Next: [the guide](GUIDE.md)

This page is for deciding whether Kandu is worth setting up. It says what Kandu is, what it takes off your hands and what it does not do. The steps to install it are in [the guide](GUIDE.md).

## The problem

A coding agent can write good code, but working with one still means sitting next to it. You approve each step, you watch it work, and at the end you check whether it really finished. The agent is fast, and you are the bottleneck. With three projects and twenty small tasks, most of your day goes to babysitting.

## What changes with Kandu

Kandu moves you one level up. You stop driving the agent and start supervising a team of agents:

1. **You say what you want, in your own words.** Kandu turns it into a precise task on a GitHub board, with acceptance criteria, after asking only what it cannot find out from the code.
2. **The hive works without you.** Every hour Kandu wakes, picks what is ready, and sends agents to build it, each in its own copy of the repo, several at once.
3. **A fresh reviewer checks every change.** It knows nothing of how the change was built, so it judges only the result, against your criteria.
4. **It merges safely, or waits for you.** A change that is hard to undo always waits for your yes. Everything else merges only on the exact commit that was reviewed and tested.
5. **"Done" means seen in your live software.** After deploy Kandu opens the live version and checks that the task is really there.
6. **You see everything on one page,** and you get a message only when something needs you.

## A day with Kandu

In the morning you open Didban, Kandu's control session, and say: "the export button should also offer CSV, and the login page is slow on mobile." Didban reads the code, asks two questions, and puts two tasks on the board.

You go to work on something else. During the day the queen, the one agent that plans for all your projects, releases the tasks. Workers build them, a reviewer finds a missing test, the worker adds it, and the second review passes. One PR merges and deploys by itself. The other touches the database, so it waits.

In the evening the dashboard shows one flower in the comb (done and seen live) and one flower that glows (waiting for you). You open its page, read in plain words what it does and what could go wrong, and press approve.

## Why bees?

*Kandu* is Persian for beehive, and the whole picture is borrowed from one: worker bees build, a queen keeps the order, a scout goes out to look, and finished work fills the comb. It is a playful borrowing, not biology. A real queen supervises no one: she lays eggs, and the colony decides together, without a boss. In Kandu the queen does plan and release the work. Please don't quote us in a biology exam.

## What is in the box

**Tasks by conversation**
- `/kandu-task` and Didban turn a request into a task with goal, criteria, how to test and what is out of scope.
- A large task is split into slices, each a complete path that can be checked on its own. You approve the split.
- A vague task is not guessed at. It goes to `Needs info` and you get a question.

**Work while you are away**
- Workers run in parallel, each in its own `git worktree`, on your always-on machine or on a named machine such as your laptop.
- The queen paces each project by priority, size and deadline, and sees stopped work through as you would, asking you only when nothing else can move it.
- Work is paced by your Claude usage. It stops at the 5-hour and weekly limits you set, so your own sessions still have room.

**Quality you can trust**
- Tests first, at the project's public interfaces.
- A fresh reviewer for every PR. Each blocker is verified before it is reported, and fixed in at most two rounds.
- The merge gate is code, not a prompt: tests pass on the reviewed commit, no blocker is open, every criterion is met.
- A live check after every deploy.

**You stay in control**
- The dashboard, called the meadow: every agent is a bee and every task is a flower, all from real state. It also has a table of tasks, daily usage and a live stream of what each agent does.
- Stop and resume the whole hive, pause one project, approve a PR or a split, change the limits, all from the dashboard.
- Notifications on Telegram for the important things only.
- A free flight opens a session on any task, for when you want to take the wheel yourself.

**Safe by design**
- A project you have not registered is never touched, and neither is your own working copy.
- Secret values are never copied or logged.
- Nothing is reverted, emailed, paid for or published without your yes.
- Everything about your setup stays in a private `local/` folder that never reaches the repo.

## Is Kandu for you?

Kandu fits you if:
- you already work with Claude Code and trust it with real tasks,
- your projects have tests, or you are willing to add them,
- you have more small and medium tasks than hours,
- you have a machine that can stay on, such as a small home server.

Kandu is not for you if:
- you want to watch and steer every line the agent writes; work in a normal Claude Code session for that,
- your project has no way to be tested or deployed by a command,
- you want a hosted service. Kandu runs on your own machines, with your own accounts.

## What it costs

Kandu itself is free and open source (MIT). It runs on your Claude subscription or API key and your GitHub account. Everything that needs no judgment runs as plain code and costs no tokens, so a quiet hour costs nothing. You set the usage limits and a daily token limit per project, and the dashboard shows what each task cost.

## Status

Kandu is in early development. The queen, the workers and the review rounds have run on a real project; merge, deploy and the live check have been tested with stand-ins. Expect rough edges, and start with auto-merge off, which is the default.

## Next

Ready to try it? Follow [the guide](GUIDE.md). The full specification is in [DESIGN.md](../DESIGN.md).

# Changelog

## 1.1.0 - 2026-11-21

### Features
- Add python -m kandu.release to propose and open a release PR (#109)
- Add «Cut a release» to the dashboard and teach Didban (#110)
- Tag and publish a merged release on the public upstream after the deploy (#111)
- Show the install's version in every look's header, and whether it is the latest (#103)
- Choose the model per project and role (#119)
- Give each run its own temporary folder on the machine's disk, removed when the run ends (#121)

### Fixes
- Pick up a task whose local branch is ahead of its PR head, and stop clearly on drift (#115)
- Settle a drifted task branch on pickup without losing commits (#116)

### Other
- Zanbourdar reports before waiting on a later done-condition (#118)

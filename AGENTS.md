# Coding Agent Guidelines

## Think Before Coding

Before making changes, understand the task, inspect the relevant code, and identify assumptions, ambiguity, and risks. Prefer simpler approaches when possible. Do not guess when the codebase provides an answer.

## Keep It Simple

Write the minimum code required to solve the task. Do not add unrequested features, abstractions, frameworks, or speculative improvements.

## Make Surgical Changes

Modify only the files and lines necessary for the task. Preserve the existing architecture, naming, style, and conventions. Do not perform unrelated refactors.

## Stay Goal-Oriented

Each change should directly support the requested outcome. After implementation, run the most relevant tests, build, or type checks. If verification is not possible, explain why.

## GitHub and Server Sync

Remote server changes must be synchronized through GitHub. Do not make or keep manual-only code changes on the production/server checkout. If a server-side hotfix is unavoidable, copy the exact change back into this repository, commit it, push it to GitHub, and then update the server from GitHub.

Before deploying or changing server files, check both local and server `git status --short --branch`. If the server has local modifications or untracked source files, reconcile them into GitHub first instead of overwriting them.

## Final Response

Summarize:

- what changed
- why it changed
- how it was verified
- any remaining risks or follow-ups

# Bazzite Parental Controls

A web-based UI for managing user account lock/unlock on Bazzite.

## Development

This project uses git worktrees for isolated development. The main branch contains only project metadata — actual code lives in feature worktrees under `.worktrees/`.

### Creating a feature worktree

```bash
git worktree add .worktrees/feature-name -b feature/feature-name
```

### Project Structure

- `.worktrees/` — Isolated development environments for each feature
- `README.md` — This file

## Target Platform

Bazzite (Fedora-based immutable desktop OS)

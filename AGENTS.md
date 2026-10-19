# Commit scope

Keep each commit focused: implement one change or fix per commit, with the
documentation for that change. Split unrelated changes into separate commits,
even when they're made in the same session.

# Versioning

Every commit to `main` that changes code (the app, its tests, packaging or
build scripts) must bump the version in that same commit, following
[Semantic Versioning](https://semver.org): patch for fixes, minor for new
features or changed behavior, major for incompatible changes such as a
recovery-journal format that older versions can't read. Commits that only
change documentation or agent instructions don't need a bump.

The version is defined only in `xbox_media_drive/__init__.py` (`__version__`);
`pyproject.toml`, the macOS app bundle and the DMG names read it from there.

# Commit attribution

Every commit created or amended with agent assistance must name the agents and
models that contributed in the commit message. Use the body or trailers; keep the
subject focused on the change. List each participating agent and its actual model,
and preserve existing attribution when amending a commit.

For example:

```text
Agent: Codex (Sol)
Model: gpt-6.1-sol
```

Preserve the human Git author identity. Do not invent model names or attribute
earlier work to the current agent.

# Delegating work

To reduce token usage, a more capable agent should hand routine work to
subagents rather than doing it itself, and keep the design decisions, reviews
and replies to the user. Give each subagent a self-contained brief: the
settled design, constraints, and the commit attribution to use.

- Menial tasks (mechanical edits, searches, log extraction, documentation
  updates): prefer Haiku 5.5, falling back to the latest GPT Luna model.
- Medium tasks (implementing a settled design, focused fixes, writing tests):
  prefer GPT-6.1-Sol, falling back to Sonnet 5.5.

Review a subagent's changes before committing or pushing them.

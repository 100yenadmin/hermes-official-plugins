# Contributing to the Hermes Official Plugins

Thanks for helping. Each top-level directory is one self-contained Hermes plugin.

## Making a change

1. Clone this repo and [hermes-agent](https://github.com/NousResearch/hermes-agent) side by side.
2. Work inside one plugin directory and run its suite against core:
   ```bash
   cd spotify
   HERMES_AGENT_REPO=../../hermes-agent PYTHONPATH=../../hermes-agent python -m pytest -q
   ```
3. Validate the manifest: `hermes plugins validate ./spotify`.
4. Open a PR that touches one plugin. CI runs that plugin on Linux, macOS and Windows against current core.

Keep each plugin self-contained: no imports between plugin directories, and its own `tests/`, `pyproject.toml` and `README.md`.

## Releasing

Merging to `main` does not ship anything to users. The release is the catalog pin: open a PR on hermes-agent that sets the plugin's `sha:` in `plugin-catalog/<name>.yaml` to the new commit (keep `subdir:` as it is). Installed copies move on their next `hermes plugins update <name>`. A pin that adds tools, hooks or dependencies asks users to consent again, so ship those as their own pin.

## Adding a plugin

Official plugins are maintained by Nous Research. Add a directory with `plugin.yaml`, `__init__.py`, `pyproject.toml`, `README.md` and `tests/`, plus a 1200x600 card in `assets/<name>.jpg`, and add it to the table in the top-level README. Third-party plugins belong in their author's repo and enter the [plugin catalog](https://github.com/NousResearch/hermes-agent/tree/main/plugin-catalog) from there.

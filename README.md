# Codex Router

A small, local-first Python tool that uses TypeSafe Jev to classify a task as quick or deep, then starts the matching Codex CLI model. It keeps task metadata in a local SQLite database and can resume a Codex session after interruption.

This is a development tool. It does not modify or install dependencies into the target Flask application.

## Current scope

- Jev makes one typed `quick` / `deep` choice before Codex starts.
- Uncertain classifications route to the deep profile.
- Codex CLI performs the actual project work using the selected model and sandbox settings.
- Job records and Codex session IDs are stored locally under `%LOCALAPPDATA%\codex-router`.
- `codex-router resume JOB_ID` resumes the saved Codex session.

The first version does not make Jev a general coding agent. Jev returns structured judgments; Codex remains the coding agent.

## Requirements

- Python 3.10 or newer
- Codex CLI installed, signed in, and available as `codex`
- A TypeSafe API key with Jev access

Set the API key in PowerShell for the current user:

```powershell
setx TYPESAFE_API_KEY "your-typesafe-api-key"
```

Open a new terminal after setting it. The key is read from the environment and is not written to the project.

## Install for development

```powershell
cd path\to\codex-router
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item config.example.toml config.toml
```

Edit `config.toml` to select models available to your Codex CLI account. Model names are examples and may need to match your account's current access.

## Use

```powershell
codex-router run --repo "C:\path\to\project" --task "Investigate and fix the upload validation bug"
codex-router jobs
codex-router resume JOB_ID
```

Use one `--repo` root per task. Review the selected route and Codex diff before committing or pushing changes.

## Reuse across projects

Keep this tool outside application repositories. Invoke it with a different `--repo` path for each project. Project-specific guidance such as `AGENTS.md` stays in each project; shared model routing stays in this tool's TOML configuration.

## Security and data handling

- Task text is sent to TypeSafe for classification and to Codex for execution.
- Repository files are accessed by Codex according to the selected Codex sandbox and approval policy.
- The SQLite job history stores task text and repository paths locally. Do not submit secrets in task prompts.
- This starter does not push commits or deploy. Those actions should remain explicit user steps.
- Jev's classification is a routing suggestion; uncertain results use the deep profile. Review model output and diffs.

## License

MIT. See [LICENSE](LICENSE).
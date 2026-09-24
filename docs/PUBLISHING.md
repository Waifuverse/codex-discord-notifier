# Public release checklist

The checked-in configuration template is `.env.example`. Users copy it to `.env`
and supply their own bot token and owner ID. Do not upload a ZIP of the working
installation; it contains ignored private data. Export committed Git content only.

Before the first public push:

1. Choose a repository name and hosting account. Decide whether to grant a software
   license; if so, add its actual LICENSE text and update the README. Otherwise
   retain the explicit no-license notice. Public visibility implies no license.
2. Run the full tests. Review `git status --short` and `git diff --cached`.
3. Run `git ls-files` and confirm there is no `.env` (except `.env.example`),
   `.state/`, `.venv/`, database, capture, log, or private `docs/history/` file.
4. Inspect every staged file for credentials, personal paths, real task IDs, and
   private conversation content. Run a secret scanner before release as well;
   pattern checks are helpful but do not prove absence of secrets.
5. Review `git remote -v` before pushing. Create an empty repository in the intended
   account, then use its exact URL. Do not force-push unrelated history.
6. Enable private vulnerability reporting if available. Verify the README renders
   and follow the installation steps from a clean clone on a separate setup.

Typical commands, after review and choosing the destination:

```powershell
git diff --cached --stat
git diff --cached
git ls-files
git commit -m "Prepare Discord bridge for public use"
git remote add origin YOUR_REPOSITORY_URL
git push -u origin main
```

Replace the URL placeholder; never embed a password/token in a Git remote.
Authentication should use the host's normal credential helper. A publication
review must include Git history if this is no longer the first commit.

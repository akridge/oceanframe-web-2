# Security

## Reporting a vulnerability

Please report security problems privately through GitHub's
[private vulnerability reporting](https://github.com/akridge/oceanframe/security/advisories/new)
rather than in a public issue.

## Things to know before deploying

- **Secrets live in `.env` only.** The deploy scripts (`deploy/stack_up.sh`, `deploy/install_oceanframe.sh`)
  generate it with random passwords for Oracle, the app schema and the starter accounts. `.env` and every copy
  of it (`.env.bak-*`, `.env copy`) are git-ignored; only `.env.example`, which holds no values, is tracked.
- **Run the app on its own only on a trusted machine.** Without the multi-user stack it has no sign-in.
- **Classifier checkpoints are code.** A PyTorch `.pt` file is a pickle, and loading it can run arbitrary code.
  The web process never unpickles a checkpoint: validation and evaluation happen in the isolated evaluation worker,
  which has no network, no credentials and no app source. Executing checkpoints at all needs
  `CURATION_TRUSTED_CHECKPOINTS=on`. Compose defaults it to `off`; the generated `.env` turns it on.
  With it on, anyone allowed to register models (curators, by default) can upload a `.pt` the worker will load.
  `CURATION_HF_ALLOWED_ORGS` limits Hugging Face references only, not uploads. Grant curator roles accordingly.
  See [docs/CURATION.md](docs/CURATION.md) for the full trust model.

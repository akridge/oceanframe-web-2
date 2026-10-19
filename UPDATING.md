# Updating a Cloud Workstation

How to bring an existing OceanFrame install up to date by pulling the branch. An update keeps
everything: the Oracle database (users, projects, every curation decision), the library
catalog, `.env` and its passwords, models and backups. Nothing gets re-staged, and nothing
is re-scored that was already scored.

## The update, step by step

Run these as yourself on the workstation, in the git checkout you installed from
(`~/oceanframe-web-2` if you followed the README):

```bash
cd ~/oceanframe-web-2

# 1. Back up first (optional, but cheap). Writes to gs://YOUR-BUCKET/oceanframe-backups/<timestamp>/
~/oceanframe_deployment/oceanframe-backup.sh -b gs://YOUR-BUCKET

# 2. Get the code. Swap main for another branch name if you are testing one.
git fetch origin
git checkout main
git pull --ff-only origin main

# 3. Re-run the installer you used the first time. It copies the checkout into
#    ~/oceanframe_deployment, keeps .env and ~/oceanframe_data, rebuilds the image and restarts.
bash deploy/install_oceanframe_gpu.sh      # GPU workstation
# bash deploy/install_oceanframe.sh        # CPU workstation

# 4. Check it came back.
~/oceanframe_deployment/oceanframe-status.sh
~/oceanframe_deployment/oceanframe-diagnostics.sh   # if anything looks wrong
```

Then open the same `https://80-…cloudworkstations.dev` URL and sign in as before.

Notes:

- **Never set `OCEANFRAME_RESET_DATA=yes` for an update.** That wipes the database (after a backup).
- **The rebuild** reuses Docker's cached layers, so it usually takes a few minutes. It only
  downloads again if `requirements*.txt` changed.
- **`git pull --ff-only` refuses to run** if the checkout has local edits. Run `git status`
  to see them, then `git stash` them (or discard them) and pull again.
- **To go back to the previous version,** run `git checkout <previous commit>` and re-run the
  installer. Database changes in this round only add tables and columns, so an older build still runs.

## One-time steps after this update

This round covers the field-feedback pass, the Activity tab, the streamline work (S1–S4) and
the library navigation work (P4a). Most of it takes effect on its own; a few things need a click.

### Happens on its own at the first start

| What | Notes |
|---|---|
| New curation tables (claims, presence, quality scores, change feed) | Created on startup. No migration to run. |
| Old evaluation spool inputs are swept | Frees disk under `~/oceanframe_data/library/curation_eval` (108 GB down to 32 GB locally). |
| The T3 baseline is scored once more, now including **train** | Only if the install's baseline was scored on test and val alone. It is a background GPU job on the same pinned version, taking under an hour on a GPU. Progress shows under Curation → the HF project → Confusion matrix. Train cells fill in when it finishes. |
| Projects are open to every curator account | Projects that had explicit members keep them. See "Members only" below to restrict one again. |

### Do once, as a lead or admin

1. **Prepare quality scores** (per project). Go to Curation → open the project → **Quality review** →
   **Prepare quality scores**. It scores every patch once on the server, so "Too dark", "Low sharpness"
   and "Needs attention" filter every patch, not only the loaded page. It runs in the
   background and can resume; the local HF project (378,681 patches) took about 10 minutes.
2. **Restrict a project, if one should be** (optional). Go to the **Project** menu → **Members** → tick **Members only**.
   Otherwise any curator account can curate it.
3. **Make T3 classes filterable in the image library.** Go to My collections → **Edit** each T3
   collection (`coralnet_mirror/pacific_all`, `coralnet_mirror/pacific_t3_splits`) → paste this
   into **Path-tag rule** → Save:

   ```
   (?:(?P<split>train|val|test)/)?(?P<class>[^/]+)/[^/]+$
   ```

   Saving re-tags the images already indexed from their paths, without downloading or
   re-embedding anything. You then get `class:ACBR`, `split:test` and similar tags under **Tags**.

### What curators will notice

- **Claims and live refresh.** An image someone has open is left out of others' queues and
  says who "is reviewing this image right now". Lists drop items others finish, about every 15 s.
- **Activity tab.** Shows who is online and what they are looking at.
- **Quality review** pages through everything and has split, class and review-state filters.
  A confusion-matrix cell can open in Quality review.
- **Matrix clean-up.** On a saved evaluation's matrix, a cell → **Clean up this cell** relabels,
  excludes or flags its patches for a re-split in one step. It leaves earlier curator decisions
  alone, and test patches need an extra confirmation.
- **Hugging Face T3 patches** can be moved, resized and added to. Exports mark each row's origin.
- **Image library.**
  - The folder tree follows the filters, and the collections themselves are the top level.
  - "Searching…" appears while results load, and an empty result names the filters that
    emptied it.
  - Filters stay in the URL.
  - **Open in curation** on a library image jumps to its curation patch, and **View in
    library** on a patch jumps back.

## If something goes wrong

- **The app doesn't come up:** run `~/oceanframe_deployment/oceanframe-logs.sh` and
  `oceanframe-diagnostics.sh`.
- **Every save fails with "Cross-site request refused":** set `OCEANFRAME_ORIGIN_CHECK=off` in
  `~/oceanframe_deployment/.env`, then run `oceanframe-restart.sh` (see README).
- **Restoring from a backup:** the `impdp` command is at the top of `deploy/backup_to_gcs.sh`.

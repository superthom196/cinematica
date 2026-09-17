# Deploying from Git

This is the developer deployment workflow for an existing Linux server. For a new
installation, use the [release installer](../INSTALL.md).

A Git push can copy server code to the host and restart Cinematica. Schedule these
pushes when nothing is playing: a server restart can interrupt playback.

## Host layout

The hook uses the SSH account's home directory:

| Path | Purpose |
|---|---|
| `~/cinematica.git` | Bare repository that receives pushes |
| `~/cinematica-src` | Temporary checkout of the pushed branch |
| `~/cinematica` | Live application directory |
| `~/cinematica-venv312` | Optional Sendspin runtime for this deployment method |

Only the repository's `server/` directory is copied to the live directory. The hook
cleans the temporary checkout, so do not keep files you want to retain there.

## Prepare the host

The host needs Git, rsync, Python, Docker and an existing Cinematica service. The
SSH account must be able to write the live directory and perform the hook's service
operations through passwordless sudo.

Review [`post-receive`](post-receive) before enabling it. It can install the committed
[`cinematica.service`](cinematica.service), so adapt that file's user, paths and
settings to your host. The release installer uses a template instead.

Configure sudo permissions for the service restarts, unit-file installation and
`systemctl daemon-reload` used by the hook. Include the Sendspin service operations
if you use network audio. The hook checks permission to restart Cinematica before
deploying; that check does not verify every later command.

## Install the hook

From the repository root on your development machine:

```bash
server/deploy/install-hook.sh your-server
```

Replace `your-server` with an SSH hostname or alias. The script creates the remote
repository and checkout directory, installs the hook, and adds a local `pi-deploy`
remote if one does not already exist.

Check the destination before pushing:

```bash
git remote get-url pi-deploy
```

Re-run the hook installer after changing `post-receive`. Updating its copy in the
repository alone does not replace the hook installed in the bare repository.

## Deploy

```bash
git push pi-deploy main
```

The hook deploys updates to `main`. If `server/` changed, it checks Python syntax,
synchronises files, updates service definitions where needed, and restarts
Cinematica. It prints recent logs and checks whether the service is active.

Changes confined to the TV app or root README do not restart the server. Changes
to documentation inside `server/` do trigger deployment and restart.

An active systemd service is only a startup check. Verify the health endpoint and
the feature you changed. A failed post-receive hook does not undo the Git push or
automatically restore the previous live files.

## Saved state

The sync uses `rsync --delete`, with exclusions for `.env`, connection and playback
state, legacy ratings, `transcode/`, Sendspin identity/pairing files, Python caches,
backup files and legacy Git directories. The exact list is in `post-receive`.

Keep provider state outside the live directory, normally under `/var/lib/cinematica`.
Check the exclusions before adding any other runtime directory: an unlisted path
can be removed during deployment. In particular, the hook does not exclude a
`stremio/` directory placed inside its live directory.

## Sendspin runtime

On the host, run the runtime installer as the service account:

```bash
bash ~/cinematica/deploy/install-sendspin.sh
```

It creates the Python environment at `~/cinematica-venv312` by default. It does not
install the systemd unit itself. On a subsequent server deployment, the hook can
render and install the unit if it finds the runtime. It restarts the bridge when
the bridge code or unit changes; the main server still restarts for any server change.

The release installer uses `/var/lib/cinematica-sendspin/` instead. Choose and
configure one deployment layout rather than mixing their defaults.

[`sendspin_probe.py`](sendspin_probe.py) is a diagnostic that connects to a player
and plays a tone. Run it deliberately when testing the audio output.

## Roll back

Create a revert commit and deploy it:

```bash
git revert <commit-to-undo>
git push pi-deploy main
```

Check the service and playback again. A code revert does not restore configuration,
provider data or other saved state; use the corresponding backup if those changed.

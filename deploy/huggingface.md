# Hugging Face Spaces

**This needs a PRO subscription.** Docker Spaces moved behind PRO: the Space
creation page offers Static free and marks Gradio and Docker as *Paid*, and the
pricing page lists "Host ZeroGPU, Gradio & Docker Spaces" among PRO features.
Free CPU-basic hardware still exists for Spaces a PRO member creates — it is
creating a Docker Space at all that is gated.

For a free deployment see [README.md](README.md); Render's free instance type
builds the same `Dockerfile` unchanged.

Everything below still works if you have PRO, and the `space` branch is already
prepared for it.

## Steps

**1. Create a write token.** <https://huggingface.co/settings/tokens> → **Create
new token** → type **Write** → name it `ucm-bridge-deploy` → copy it. This is
what you paste when git asks for a password; your account password will not
work.

**2. Create the Space.** <https://huggingface.co/new-space>

| Field | Value |
|---|---|
| Space name | `ucm-bridge-console` |
| SDK | **Docker** → **Blank** |
| Hardware | **CPU basic · 2 vCPU · 16 GB** |
| Visibility | **Public** (private works, but only you can open it) |

**3. Add the Space as a git remote.** From this repository:

```bash
git remote add space https://huggingface.co/spaces/YOUR-USERNAME/ucm-bridge-console
```

**4. Push the `space` branch to the Space's `main`.**

```bash
git push space space:main --force
```

`--force` is right the first time only: the Space was created with a starter
commit sharing no history with this repository, and this replaces it. Username
is your HF username; password is the **token** from step 1.

The `space` branch is `main` with `README.md` swapped for
[`huggingface/README.md`](huggingface/README.md). Spaces reads that file's YAML
frontmatter to decide the SDK and the port, and it must be at the repository
root — hence a branch, rather than putting hosting frontmatter on the front page
of the project.

**5. Watch it build.** The Space page shows the Docker build log. Three to four
minutes cold. When it flips to **Running**, the console is at
`https://huggingface.co/spaces/YOUR-USERNAME/ucm-bridge-console`.

## Shipping a later change

```bash
git checkout space && git merge main && cp deploy/huggingface/README.md README.md
```

```bash
git add README.md && git commit -m "Sync from main" && git push space space:main
```

No `--force` after the first push. The copy resolves the README either way: if
`main` changed it the merge conflicts and this overwrites the conflict, and if it
did not, it is a no-op.

## Why 48 hours beats 15 minutes

The one thing Spaces still does better than the free alternatives: it sleeps
after **48 hours** without a visitor rather than 15 minutes, so a link you send
someone is usually warm when they open it. On cpu-basic the sleep timer is not
configurable.

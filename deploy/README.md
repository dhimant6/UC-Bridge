# Deploying the console

The whole thing is one container: `Dockerfile` builds the React console, then
serves it and the JSON API from a single Python process. No database, no Redis,
no object store — state is in-process (ADR-0002 leaves storage undecided), so
anywhere that can run one container can run this.

## Recommendation: Hugging Face Spaces (Docker SDK)

**Free, no credit card, ever — including for the compute.** This is the only
mainstream host that runs a real always-addressable container without asking for
a card at any point, and it is a straightforward `git push`.

- 2 vCPU, 16 GB RAM, free "CPU basic" hardware.
- Public HTTPS URL with a certificate, no configuration.
- Sleeps after **48 hours** of no visitors and wakes on the next request. On
  cpu-basic the sleep timer is not configurable.
- Deploy is `git push`; Spaces builds the `Dockerfile` itself.

16 GB of RAM is far more than this needs, and 48 hours is a generous idle window
compared with the 15-minute spin-downs elsewhere.

### Steps

**1. Create the account.** <https://huggingface.co/join> — email and password,
no card.

**2. Create a write token.** <https://huggingface.co/settings/tokens> → **Create
new token** → type **Write** → name it `ucm-bridge-deploy` → copy it. This is
what you paste when git asks for a password; your account password will not
work.

**3. Create the Space.** <https://huggingface.co/new-space>

| Field | Value |
|---|---|
| Space name | `ucm-bridge-console` |
| License | leave blank, or pick one |
| SDK | **Docker** → **Blank** |
| Hardware | **CPU basic · 2 vCPU · 16 GB · FREE** |
| Visibility | **Public** (private works, but only you can open it) |

**4. Add the Space as a git remote.** From this repository:

```bash
git remote add space https://huggingface.co/spaces/YOUR-USERNAME/ucm-bridge-console
```

**5. Push the `space` branch to the Space's `main`.**

```bash
git push space space:main --force
```

`--force` is right the first time only: the Space was created with a starter
commit that has no shared history with this repository, and this replaces it.
Username is your HF username; password is the **token** from step 2.

The `space` branch is `main` with `README.md` swapped for
[`huggingface/README.md`](huggingface/README.md). Spaces reads that file's YAML
frontmatter to decide the SDK and the port, and it has to be at the repository
root — hence a branch rather than putting hosting frontmatter on the project's
front page.

**6. Watch it build.** The Space page shows the Docker build log. Three to four
minutes for a cold build. When it flips to **Running**, the console is at
`https://huggingface.co/spaces/YOUR-USERNAME/ucm-bridge-console`.

### Shipping a later change

```bash
git checkout space && git merge main && cp deploy/huggingface/README.md README.md
```

```bash
git add README.md && git commit -m "Sync from main" && git push space space:main
```

No `--force` after the first push. The copy resolves the README either way: if
`main` changed it the merge conflicts and this overwrites the conflict, and if it
did not, it is a no-op.

## Alternatives, and what each costs you

| Host | Card needed | Idle behaviour | Notes |
|---|---|---|---|
| **Hugging Face Spaces** | No | Sleeps at 48 h | Recommended. Docker, 2 vCPU / 16 GB. |
| **Render** free web service | Not to sign up; may be asked for compute | Spins down at 15 min, ~1 min cold start | Builds the `Dockerfile`. Verify the card prompt before committing. |
| **Fly.io / Railway / Koyeb** | Yes | — | All now require a card on the free tier. Ruled out. |
| **PythonAnywhere** free | No | Always on | **Does not work.** WSGI only, and FastAPI is ASGI. |
| **GitHub Pages / Cloudflare Pages** | No | Always on | Static only, so the Python control plane cannot run. See below. |

### If you want a zero-backend option

GitHub Pages cannot run Python, so the console would need its API responses
baked in as fixtures at build time. That is genuinely achievable — every payload
is deterministic, so a script could capture them — but it is a different
artefact: the guardrail refusals would be canned strings rather than the library
actually refusing, which is the one thing this console exists to demonstrate. Not
recommended unless a permanently-awake link matters more than the demo being
real.

## Running it locally

```bash
pip install -e ".[api,dev]"
```

```bash
cd ui && npm install && npm run build
```

```bash
python -m ucm_bridge.api
```

That serves the console and the API together on <http://127.0.0.1:8000>, with
OpenAPI docs at `/api/docs`.

For UI work, run the two separately so you get hot reload — Vite proxies `/api`
to port 8000:

```bash
python -m ucm_bridge.api --reload
```

```bash
cd ui && npm run dev
```

## Building the image yourself

```bash
docker build -t ucm-bridge-console .
```

```bash
docker run --rm -p 7860:7860 ucm-bridge-console
```

## What the deployment is not

- **It has no authentication.** Identity comes from an `X-UCM-Roles` header so
  the console can switch roles and show the RBAC boundaries. A real deployment
  resolves it from an OIDC token; the swap is confined to `tenant_context()` in
  `src/ucm_bridge/api/app.py`.
- **It holds no credentials and reaches no network.** Connectors are driven by
  the committed cassettes. `VaultCredentialProvider` raises
  `NotImplementedError` rather than guessing a Vault integration.
- **State is in-process.** Restart or sleep loses discovery results, plans, runs,
  and the audit chain. `JsonFileRunStore` exists if you want runs to survive, but
  nothing is wired to a disk by default because a free host's filesystem is
  usually ephemeral anyway.
- **CORS is wide open.** Harmless when the API carries no credentials and no
  private data, and it means the Vite dev server works against a deployed
  backend. It would need tightening the moment either of those changed.

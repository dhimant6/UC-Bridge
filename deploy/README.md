# Deploying the console

The whole thing is one container: `Dockerfile` builds the React console, then
serves it and the JSON API from a single Python process. No database, no Redis,
no object store — state is in-process (ADR-0002 leaves storage undecided), so
anywhere that can run one container can run this.

## What this needs from a host

Worth stating before the options, because it rules most of them out:

- **A container, or a Python 3.12 runtime plus Node to build the console.**
- **One long-lived process.** State is in-process, and the pipeline is a
  sequence — discover, then assess, then plan, then run. Serverless platforms
  that spread requests across invocations will lose a plan between building it
  and dry-running it. This is the constraint that eliminates Vercel and Lambda,
  not cost.
- **Not much else.** ~200 MB of RAM, no disk, no database, no outbound network.

## Recommendation: Render, free instance type

- Builds the `Dockerfile` in this repository, unchanged.
- **750 free instance-hours a month**, which is more than a month of wall-clock.
- Public HTTPS with a managed certificate.
- **Spins down after 15 minutes idle**, ~1 minute to wake on the next request.
- [`render.yaml`](../render.yaml) in the repository root means there is nothing
  to configure by hand.

No payment method is required to run free services. Render's own free-tier
documentation only mentions a payment method in the context of exceeding the
bandwidth allowance — at which point services without one are suspended rather
than billed — which is the behaviour of a tier that does not need a card to
start.

Nor does it need access to your repositories: see
[without a connected account](#without-a-connected-account) for the URL-only
route, and note that the connected route is scoped per-repository by GitHub.

### Steps

**1. Create the account.** <https://render.com/register> — GitHub, GitLab,
Google, or email.

**2. New → Blueprint.** <https://dashboard.render.com/blueprints>

This route goes through Render's GitHub App, so it asks to connect your account.
**Scope it when you install:** GitHub's install screen offers *All repositories*
or *Only select repositories* — pick the second and tick only `UC-Bridge`.
Render then cannot see anything else you own, private or otherwise, because
GitHub enforces the scope rather than Render honouring it. Revoke later at
<https://github.com/settings/installations>.

If you would rather Render had no link to your GitHub account at all, skip to
[without a connected account](#without-a-connected-account).

**3. Apply.** Render reads [`render.yaml`](../render.yaml), finds one web
service on the free plan, and starts building. Nothing to fill in.

**4. Watch the build.** Five to eight minutes cold — Render's free builders are
slower than a laptop, and the image compiles the console with Node before
installing Python. The log is live in the dashboard.

**5. Open it.** `https://ucm-bridge-console.onrender.com`, or whatever name
Render assigned if that one was taken.

### Shipping a later change

Push to `main`. Render redeploys automatically.

### Without a connected account

Render can deploy any public repository from its URL, with no Git provider
credentials at all:

**New → Web Service** → paste `https://github.com/dhimant6/UC-Bridge` into
**Public Git Repository** → **Language: Docker** → **Instance Type: Free** →
Create. The `Dockerfile` and `PORT` are detected, so the only thing lost versus
the blueprint is that two dropdowns are set by hand.

The real cost is deploys, not setup: **Render supports neither auto-deploy nor
PR previews for a service connected this way**, because there are no credentials
to hang a webhook off. You redeploy from the dashboard, or hit the service's
**Deploy Hook** URL (Settings → Deploy Hook), which is a plain GET:

```bash
curl -X POST "https://api.render.com/deploy/srv-XXXX?key=YYYY"
```

That is also the piece to drop into a GitHub Action if you want push-to-deploy
without granting Render anything — the secret then lives in your repository
rather than as access to it.

Blueprints are not available on this route; `render.yaml` is read through a
connected provider.

## Hugging Face Spaces: no longer free for this

**Docker Spaces now require PRO.** The Space creation page offers Static free,
with Gradio and Docker marked *Paid*, and the pricing page lists "Host ZeroGPU,
Gradio & Docker Spaces" as a PRO feature. Free CPU-basic hardware still exists
for Spaces that PRO members create; it is the *creation* of a Docker Space that
is now gated.

If you have PRO, or take the $9/month, everything needed is still here — the
`space` branch and [`huggingface/README.md`](huggingface/README.md) — and the
steps are in [huggingface.md](huggingface.md).

A **Static** Space could host the console but not the control plane, so the API
responses would have to be baked in as fixtures at build time. Every payload is
deterministic, so that is achievable, but it is a different artefact: the
guardrail refusals become canned strings rather than the library actually
refusing, which is the one thing this console exists to demonstrate.

## Everything else, and what it costs you

| Host | Card | Idle | Verdict |
|---|---|---|---|
| **Render** free | Not to start | Down at 15 min, ~1 min wake | **Recommended.** Docker, 750 h/month. |
| **Back4App Containers** | No | Sleeps; 600 active hours | Works, but 256 MB RAM is tight and the platform is opinionated. Reasonable fallback. |
| **Hugging Face Spaces** | No | Sleeps at 48 h | Docker now needs PRO. Static is free but cannot run Python. |
| **Northflank** free sandbox | Yes, to verify | — | Card required at signup. |
| **Fly.io / Railway / Koyeb** | Yes | — | All require a card on the free tier. |
| **Vercel / Netlify Functions** | No | Always warm | **Breaks this app.** Serverless loses in-process state between the plan and the dry run. |
| **PythonAnywhere** free | No | Always on | **Does not work.** WSGI only; FastAPI is ASGI. |
| **GitHub / Cloudflare Pages** | No | Always on | Static only. Same trade-off as a Static Space. |

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

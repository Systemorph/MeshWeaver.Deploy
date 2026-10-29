# Generated deployment targets

`deploy/compose`, `deploy/compose-ha`, `deploy/aca` and `deploy/marketplace/mainTemplate.json` are
**generated**. Their generator is `src/Memex.Deploy.AppHost` in MeshWeaver.Plugins; what each
target guarantees, and the test that checks it, is in that project's `README.md`. Never hand-edit a
generated file here: the next regeneration overwrites it. Change the generator, then regenerate.

## Regenerate

From this repository's root, with a MeshWeaver.Plugins checkout of `main` beside it:

```bash
APPHOST=../MeshWeaver.Plugins/src/Memex.Deploy.AppHost/Memex.Deploy.AppHost.csproj
aspire publish --non-interactive --apphost $APPHOST -o <out>/compose    -- --mode compose
aspire publish --non-interactive --apphost $APPHOST -o <out>/compose-ha -- --mode compose-ha
aspire publish --non-interactive --apphost $APPHOST -o <out>/aca        -- --mode azure
```

Then copy into this tree:

| Generated | Committed as |
|---|---|
| `<out>/compose/docker-compose.yaml` | `deploy/compose/docker-compose.yaml` |
| `<out>/compose/.env` | `deploy/compose/.env.example` |
| `<out>/compose-ha/docker-compose.yaml` | `deploy/compose-ha/docker-compose.yaml` |
| `<out>/compose-ha/.env` | `deploy/compose-ha/.env.example` |
| `<out>/aca/**` | `deploy/aca/**` (replace the folder) |
| `az bicep build --file deploy/aca/main.bicep --outfile deploy/marketplace/mainTemplate.json` | `deploy/marketplace/mainTemplate.json` |

The generated `.env` is committed under the name `.env.example` because a real `.env` is
gitignored: it holds the secrets.

Validate before committing:

```bash
# compose: with a filled .env beside each file (a scratch copy, never in this tree)
docker compose --project-directory <copy>/compose    -f <copy>/compose/docker-compose.yaml    config
docker compose --project-directory <copy>/compose-ha -f <copy>/compose-ha/docker-compose.yaml config
# ACA: main.bicep alone deploys the environment, memex-postgres, memex-migration and memex-portal
az bicep build --file deploy/aca/main.bicep --stdout > /dev/null
```

## Using a compose target

1. Copy `.env.example` to `.env` beside the compose file.
2. Fill the two secrets it declares:
   - `MEMEX_POSTGRES_PASSWORD`: the Postgres password. Left blank, a fresh volume gets an empty
     password.
   - `KEY_PROTECTION_MASTER_KEY`: a base64 32-byte key; the portal receives it as
     `Ai__KeyProtection__MasterKey`. It is required, not only in production: left blank, the
     deployment cannot store a credential at all. Entering a provider key, saving a GitHub
     credential, minting a plugin-registry sync-token signing key and rotating the Entra
     credential each refuse, naming the setting. Reading stays tolerant, so an install that
     already holds legacy plaintext keys keeps working. Never commit it.
3. `docker compose up -d`.

The same `.env` is the portal's `env_file`, so any portal configuration key you add there in the
ASP.NET double-underscore form (`Section__Key`) reaches the container. The service's own
`environment:` entries are derived from the Deployment record and win over `.env`, so a key the
record already sets (`Deployment__Backend`, `Deployment__Orleans__Clustering`, `Storage__*`,
`Graph__Storage__*`, `Authentication__EnableDevLogin`, `Mcp__BaseUrl`, …) is changed in the
generator's input, not in `.env`.

Keys the record does not set, which an operator typically adds to `.env`:

```bash
# AI provider keys (bring your own; only providers whose feature flag is on consume them)
Anthropic__ApiKey=
AzureOpenAI__ApiKey=
AzureOpenAI__Endpoint=

# Feature flags: all default to true (absent = current behaviour). Turn a capability OFF
# explicitly; a disabled flag wins even when its key is set.
Features__Ai__Providers__Anthropic=true
Features__Ai__Providers__AzureFoundry=false
Features__Ai__Providers__AzureOpenAI=true
Features__Ai__Providers__OpenAI=false
# The co-hosted CLIs need the portal-ai image, which has their binaries.
Features__Ai__Clis__ClaudeCode=false
Features__Ai__Clis__Copilot=false
```

Sign-in, email, Teams and embedding secrets are declared in the generated `.env` only when the
generator is run with that feature configured (MeshWeaver.Plugins `src/Memex.Deploy.AppHost/README.md`
→ *Secrets*).

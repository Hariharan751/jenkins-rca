# Google OAuth Setup for bbctl

## Normal users: nothing to do

The bbctl binary released via Homebrew or the GitHub Releases page includes the
Google OAuth client secret. Just run:

```bash
bbctl login
```

No environment variables, no config file, no setup required. Local builds
(`go build ./cmd/bbctl`) also include the embedded secret — same behavior as
release builds.

## What is JENKINS_RCA_OIDC_CLIENT_SECRET?

`JENKINS_RCA_OIDC_CLIENT_SECRET` is an **override** for the embedded OAuth 2.0 client
secret. You only need it if you are pointing bbctl at a different Google OAuth
client (e.g. your own deployment with a different OAuth app).

```bash
JENKINS_RCA_OIDC_CLIENT_SECRET=GOCSPX-... bbctl login
```

## Why is the secret in source?

bbctl uses a Google OAuth **Desktop App** client. For this client type, the
"secret" is not cryptographically sensitive — PKCE (Proof Key for Code Exchange)
protects the OAuth flow, not the client secret. Google's own documentation
acknowledges that desktop app client secrets cannot be kept confidential.

We previously attempted to inject this value at build time via `-ldflags`, but
the complexity cost outweighed any actual security benefit given the secret's
public-by-design nature. Treating this value as source is the correct engineering
trade-off here — **and ONLY here**. Do not use this as precedent for actual
secrets (API keys, database passwords, service tokens, etc.).

## When rotating the secret

1. Generate a new secret in Google Cloud Console → APIs & Services → Credentials
2. Update `defaultOIDCClientSecret` in `internal/config/config.go`
3. Tag a new release — Homebrew users get it on next `brew upgrade`

## Advanced: overriding the OAuth client

If you are running your own bbctl backend with a different Google OAuth client:

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Select your project → **APIs & Services → Credentials**
3. Find your OAuth 2.0 Desktop App client and note the client ID and secret

Set the override before running `bbctl login`:

```bash
export JENKINS_RCA_OIDC_CLIENT_SECRET=GOCSPX-...
bbctl login
```

You will also want to override the other OIDC fields and backend URL via
`~/.bbctl/config.yaml` or `JENKINS_RCA_BACKEND_URL`.

## Security note

The client secret authenticates the **application** to Google, not the user.
If it is ever compromised, rotate it via Google Cloud Console and ship a new
binary release.

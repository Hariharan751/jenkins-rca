"""Google OAuth web flow + session-cookie auth for the dashboard.

Auth is OPT-IN — if `JENKINS_RCA_OIDC_CLIENT_ID` env var is missing, the
auth dependency becomes a no-op and the dashboard is open. This keeps
dev / local runs working without GCP setup.

Production env (set in /etc/systemd/system/jenkins-rca.service.d/env.conf):
  JENKINS_RCA_OIDC_CLIENT_ID=<web-client-id>.apps.googleusercontent.com
  JENKINS_RCA_OIDC_CLIENT_SECRET=GOCSPX-<web-secret>
  JENKINS_RCA_SESSION_SECRET=<random 32-byte hex>
  JENKINS_RCA_BASE_URL=https://jenkins-rca.jinka.in
  JENKINS_RCA_ALLOWED_DOMAIN=blackbuck.com    # optional, default blackbuck.com

OAuth client must be **Web Application** type (Desktop OAuth clients
cannot redirect to https hosts). Authorized redirect URI configured in
GCP console:
  https://jenkins-rca.jinka.in/rca/v1/auth/callback
"""
import base64
import json
import os
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse


OIDC_CLIENT_ID     = os.environ.get("JENKINS_RCA_OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("JENKINS_RCA_OIDC_CLIENT_SECRET", "")
SESSION_SECRET     = os.environ.get("JENKINS_RCA_SESSION_SECRET", "")
BASE_URL           = os.environ.get("JENKINS_RCA_BASE_URL", "https://jenkins-rca.jinka.in")
ALLOWED_DOMAIN     = os.environ.get("JENKINS_RCA_ALLOWED_DOMAIN", "blackbuck.com")

# Google OIDC endpoints (stable; could be discovered via .well-known but
# hardcoding avoids a startup network call).
GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Redirect URI registered in Google Cloud Console for the web-app client.
# The /rca prefix is required because the ALB rule routes
# jenkins-rca.jinka.in/rca/* → the jenkins-rca-tg target group.
REDIRECT_URI = f"{BASE_URL}/rca/v1/auth/callback"


def is_enabled() -> bool:
    """Auth is enabled iff all required env vars are set.

    Lets dev / local installs run without GCP wiring — auth becomes
    no-op and the dashboard is open. Prod must set all three.
    """
    return bool(OIDC_CLIENT_ID and OIDC_CLIENT_SECRET and SESSION_SECRET)


def login_url(state: str) -> str:
    """Build the Google authorization URL with state token."""
    params = {
        "client_id":     OIDC_CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "access_type":   "online",
        "prompt":        "select_account",
        "state":         state,
        # Hint Google to filter to a specific Workspace domain. Helps UX —
        # users on personal Gmail accounts are immediately rejected at the
        # picker rather than after callback. Defense-in-depth; still verify
        # email domain post-callback (hd hint is not security-binding).
        "hd":            ALLOWED_DOMAIN,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """Trade authorization code for id_token. Raises on failure."""
    data = {
        "code":          code,
        "client_id":     OIDC_CLIENT_ID,
        "client_secret": OIDC_CLIENT_SECRET,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data=data)
        resp.raise_for_status()
        return resp.json()


def decode_id_token(id_token: str) -> dict:
    """Decode the JWT payload WITHOUT signature verification.

    Safe because the token came directly from Google's token endpoint over
    HTTPS in `exchange_code`. We are not accepting id_tokens from an
    untrusted caller. Full signature verification with JWKS rotation would
    add ~80 lines + a periodic key-fetch — not worth it for this trust model.
    """
    try:
        _, payload_b64, _ = id_token.split(".")
        # Re-pad base64url
        padding = "=" * (-len(payload_b64) % 4)
        payload_json = base64.urlsafe_b64decode(payload_b64 + padding).decode()
        return json.loads(payload_json)
    except Exception:
        return {}


def email_allowed(email: str) -> bool:
    """Restrict access to the configured Workspace domain."""
    return bool(email) and email.lower().endswith("@" + ALLOWED_DOMAIN.lower())


def require_auth(request: Request):
    """FastAPI dependency — call from dashboard routes.

    Returns: dict {email, name, picture} from the session.
    On no/invalid session: raises HTTPException 302 → /v1/auth/login.

    If auth is disabled (env not configured) returns a placeholder dict so
    routes still work in dev. Production must set all OIDC env vars.
    """
    if not is_enabled():
        return {"email": "anon@local", "name": "anonymous", "picture": ""}
    user = request.session.get("user")
    if not user or not email_allowed(user.get("email", "")):
        # 307 redirect preserves the original path in `next` query param
        # so we can bounce back after login. Browsers handle this cleanly.
        next_url = request.url.path
        if request.url.query:
            next_url += "?" + request.url.query
        # Wrap the redirect in an HTTPException so dependency-injection short-circuits
        # the route. FastAPI's normal Depends() raises don't allow direct
        # RedirectResponse return.
        raise HTTPException(
            status_code=307,
            detail="auth required",
            headers={"Location": f"/rca/v1/auth/login?next={next_url}"},
        )
    return user


def new_state_token() -> str:
    """Random opaque token to prevent CSRF on the OAuth flow."""
    return secrets.token_urlsafe(32)

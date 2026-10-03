"""Google credentials for the MCP server (LLD 3.9).

Two modes:
* OAuth user (default for a personal/demo account): run once
      python -m advisor_agent.mcp_server.google_auth
  to open the consent screen and write `AGENT_GOOGLE_TOKEN_FILE` (token.json).
* Service account (Workspace): `AGENT_GOOGLE_SERVICE_ACCOUNT_FILE` + optional
  `AGENT_GOOGLE_DELEGATED_USER` for domain-wide delegation (needed for Gmail drafts).

Scopes are the minimum the five tools need. `gmail.compose` allows creating drafts; the
server simply has no tool that sends.
"""

from pathlib import Path
from typing import Any

SCOPES = [
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/gmail.compose",
]


class GoogleAuthError(RuntimeError):
    pass


def load_credentials(
    *,
    token_file: str | None,
    service_account_file: str | None = None,
    delegated_user: str | None = None,
) -> Any:
    """Return google-auth credentials; refreshes an expired OAuth token and saves it back."""
    if service_account_file:
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            service_account_file, scopes=SCOPES
        )
        return creds.with_subject(delegated_user) if delegated_user else creds

    if not token_file or not Path(token_file).exists():
        raise GoogleAuthError(
            "No Google credentials. Run `python -m advisor_agent.mcp_server.google_auth` "
            "(OAuth) or set AGENT_GOOGLE_SERVICE_ACCOUNT_FILE."
        )
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            Path(token_file).write_text(creds.to_json())
        else:
            raise GoogleAuthError(
                f"Google token in {token_file} is invalid; re-run the consent flow"
            )
    return creds


def build_services(creds: Any) -> tuple[Any, Any, Any]:
    """(calendar, docs, gmail) discovery clients; `cache_discovery=False` avoids file caches."""
    from googleapiclient.discovery import build

    return (
        build("calendar", "v3", credentials=creds, cache_discovery=False),
        build("docs", "v1", credentials=creds, cache_discovery=False),
        build("gmail", "v1", credentials=creds, cache_discovery=False),
    )


def run_consent_flow(credentials_file: str, token_file: str) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
    creds = flow.run_local_server(port=0)
    Path(token_file).parent.mkdir(parents=True, exist_ok=True)
    Path(token_file).write_text(creds.to_json())


def main() -> None:
    from advisor_agent.config import get_settings

    s = get_settings()
    if not s.google_credentials_file or not Path(s.google_credentials_file).exists():
        raise SystemExit(
            "Set AGENT_GOOGLE_CREDENTIALS_FILE to the OAuth client JSON downloaded from "
            "Google Cloud Console (APIs & Services > Credentials > Desktop app)."
        )
    run_consent_flow(s.google_credentials_file, s.google_token_file)
    print(f"Saved Google token to {s.google_token_file}")


if __name__ == "__main__":
    main()

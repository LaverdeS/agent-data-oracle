from agent_data_oracle.preview_access import PreviewAccess

PREVIEW_ACCESS_SECRET = "founder-held-preview-secret"


def founder_preview_access(
    *recipient_emails: str,
    founder_emails: frozenset[str] | None = None,
) -> PreviewAccess:
    recipients = frozenset(recipient_emails)
    return PreviewAccess.founder_only(
        access_secret=PREVIEW_ACCESS_SECRET,
        recipient_emails=recipients,
        founder_emails=founder_emails if founder_emails is not None else recipients,
    )

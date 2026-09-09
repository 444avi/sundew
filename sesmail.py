"""Email delivery for the vavi monitors.

Two backends sit behind one ``send(env, msg)`` interface, chosen by
configuration so callers never change and switching providers is an env-only
change:

* **SMTP (Mailtrap)** — used when ``SMTP_HOST`` is set. Connects with STARTTLS
  on ``SMTP_PORT`` (default 587) and authenticates with ``SMTP_USER`` /
  ``SMTP_PASSWORD``. This is the current production path.
* **Amazon SES** — used when ``SMTP_HOST`` is empty. Sends via SESv2 SendEmail
  (raw content) authenticated by the EC2 instance role (no static secret);
  bounces/complaints publish to the SES configuration set in
  ``SES_CONFIGURATION_SET``. Kept as a fallback.

boto3 (and smtplib) are imported lazily inside the backend that needs them, so
importing this module never requires either, and the SES path only needs boto3
when SMTP is not configured.
"""


def send(env, msg):
    """Deliver a pre-built EmailMessage. ``msg['From']`` must be an address on
    the verified sending domain; ``msg['To']`` may be one address or a
    comma-separated list. Raises on transport/permission errors.

    Sets a Reply-To (from ``EMAIL_REPLY_TO``, default
    ``avi@arboretuminvestments.net``) unless the caller already set one, so
    replies reach a monitored inbox rather than the send-only From address."""
    if "Reply-To" not in msg:
        reply_to = env.get("EMAIL_REPLY_TO", "avi@arboretuminvestments.net")
        if reply_to:
            msg["Reply-To"] = reply_to
    if env.get("SMTP_HOST"):
        _send_smtp(env, msg)
    else:
        _send_ses(env, msg)


def _send_smtp(env, msg):
    import smtplib

    host = env["SMTP_HOST"]
    port = int(env.get("SMTP_PORT", "587"))
    user = env.get("SMTP_USER", "")
    password = env.get("SMTP_PASSWORD", "")
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)


def _send_ses(env, msg):
    import boto3

    client = boto3.client("sesv2", region_name=env.get("AWS_REGION", "us-west-1"))
    recipients = [addr.strip() for addr in (msg["To"] or "").split(",") if addr.strip()]
    kwargs = dict(
        FromEmailAddress=msg["From"],
        Destination={"ToAddresses": recipients},
        Content={"Raw": {"Data": msg.as_bytes()}},
    )
    config_set = env.get("SES_CONFIGURATION_SET", "arboretum-notifications")
    if config_set:
        kwargs["ConfigurationSetName"] = config_set
    client.send_email(**kwargs)

"""Amazon SES delivery for the vavi monitors.

Sends a pre-built ``email.message.EmailMessage`` through SESv2 SendEmail using
raw content, so every subject, header, and body the callers already build goes
out byte-for-byte unchanged. Authentication is the EC2 instance role (no SMTP
password); the region comes from ``AWS_REGION`` and defaults to us-west-1.

boto3 is imported lazily inside :func:`send` so that merely importing the
monitors (as the test suite does) never requires boto3 to be installed.
"""


def send(env, msg):
    """Deliver ``msg`` via SES. ``msg['From']`` must be an address on a verified
    SES identity (arboretuminvestments.net). ``msg['To']`` may be a single
    address or a comma-separated list. Raises on transport/permission errors."""
    import boto3

    client = boto3.client("sesv2", region_name=env.get("AWS_REGION", "us-west-1"))
    recipients = [addr.strip() for addr in (msg["To"] or "").split(",") if addr.strip()]
    kwargs = dict(
        FromEmailAddress=msg["From"],
        Destination={"ToAddresses": recipients},
        Content={"Raw": {"Data": msg.as_bytes()}},
    )
    # Attach the SES configuration set so bounces/complaints/deliveries are
    # published to SNS for monitoring. Set SES_CONFIGURATION_SET="" to disable.
    config_set = env.get("SES_CONFIGURATION_SET", "arboretum-notifications")
    if config_set:
        kwargs["ConfigurationSetName"] = config_set
    client.send_email(**kwargs)

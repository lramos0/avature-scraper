"""Operator acknowledgement text and helpers."""

from __future__ import annotations

ACK_PHRASE = "I ACCEPT LEGAL RESPONSIBILITY"

LEGAL_WARNING = f"""
The site's robots.txt indicates that the selected path is not allowed for this user agent.

Continuing may violate the site's stated access preferences and may create legal, contractual,
or reputational risk. You are responsible for ensuring that you have authorization and that your
use complies with applicable laws, terms of service, policies, and rate limits.

To continue anyway, type exactly:

    {ACK_PHRASE}

Press Enter with anything else to stop safely.
""".strip()

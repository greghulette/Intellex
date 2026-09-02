"""Make HTTPS work on a stock Python, wherever that Python came from.

macOS is why this file exists. The python.org framework build does NOT read the
system keychain; it looks for CAs at

    /Library/Frameworks/Python.framework/Versions/<x.y>/etc/openssl/cert.pem

and the installer ships that directory EMPTY, leaving you to run "Install
Certificates.command" by hand afterwards. Almost nobody does -- it is a step in a
ReadMe nobody opens. Every HTTPS request from that interpreter then fails with
CERTIFICATE_VERIFY_FAILED.

Not a theoretical failure: it is exactly how the first Mac run of NaviLink went.
fetch_webui.py reported "cannot reach greghulette.github.io ... (offline is fine)"
on a machine with a perfectly good network, and the launcher's version line said
"offline -- cannot compare" for the same reason. Both described the symptom
accurately and the cause not at all, which is the worst way to be wrong.

certifi is the same CA bundle Install Certificates.command installs, so leaning on
it here has that script's effect without requiring anyone to have run it.

VERIFICATION IS NEVER DISABLED. An empty CA store is a reason to go and find CAs,
not a reason to stop checking them. What comes back from this fetch is served to a
browser as same-origin script, so an unverified fetch would be a genuinely bad
trade -- worse than the failure it is trying to avoid.
"""
from __future__ import annotations

import functools
import ssl


@functools.lru_cache(maxsize=1)
def context() -> ssl.SSLContext:
    """A context that can actually verify.

    Cached: building one parses the whole CA bundle, and a tool refresh makes ~35
    requests back to back.
    """
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats()["x509_ca"]:
        return ctx                      # platform store is populated -- leave it alone
    try:
        import certifi
    except ImportError:
        return ctx                      # nothing better to offer; fail honestly
    ctx.load_verify_locations(cafile=certifi.where())
    return ctx


def is_cert_error(exc: BaseException | None) -> bool:
    """Is this "I could not verify" rather than "I could not connect"?

    Worth separating, because the two have opposite fixes and urlopen buries the
    real one: a verification failure arrives as URLError with the SSLError hanging
    off .reason, so an isinstance check on the outer exception misses every time.
    """
    for _ in range(5):                  # bounded: .reason can be a str, or self-referential
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        if exc is None:
            return False
        exc = (getattr(exc, "reason", None)
               or getattr(exc, "cause", None)
               or getattr(exc, "__cause__", None))
    return False


# Printed verbatim wherever a fetch dies of this, because the fix is short and the
# error message on its own points at the network rather than at the interpreter.
ADVICE = (
    "This Python cannot verify ANY certificate -- its CA store is empty.\n"
    "  That is a stock python.org macOS install, not a network problem. Fix it with:\n"
    "      .venv/bin/python3 -m pip install certifi\n"
    "  or once, for every venv built from this interpreter:\n"
    "      open '/Applications/Python 3.9/Install Certificates.command'"
)

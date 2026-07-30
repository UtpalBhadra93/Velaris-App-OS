"""HxSentinel — tenant-keyed hashing for identity-bearing signals.

The signal store exists to answer "which other cases share this phone number /
email / account?". That requires the value to be *matchable*, but not
*readable*. A keyed digest gives both:

* equal inputs inside one tenant produce equal digests, so matching works;
* the stored digest cannot be reversed, so the table is useless if it leaks;
* the key is the tenant's own DEK, so digests from two tenants never collide
  into a false match and cannot be correlated across tenants;
* shredding the tenant's DEK orphans the whole signal index for that tenant —
  crypto-shredding that follows the tenant, which is what GDPR erasure wants.

BLAKE2b keyed mode is used rather than plain ``sha256(salt + value)``: it is a
proper keyed MAC (no length-extension foot-gun), it is fast, and it is in the
standard library, so this adds no dependency.

The cost of this design is honest and worth stating: a finding can say *that*
two cases share a phone number, but not *which* number. Showing the value would
mean storing it, and a table whose whole purpose is linking identities across
cases is the last place that should hold plaintext contact details.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

#: Domain separator — keeps these digests unrelated to any other keyed hash
#: that might later be derived from the same DEK.
_PERSON = b"hxsentinel"

#: 32 bytes -> 64 hex chars, comfortably inside signal_key VARCHAR(128).
_DIGEST_SIZE = 32


def normalize(signal_type: str, value: str) -> str:
    """Canonicalise a value so trivial formatting differences still match.

    Matching is only as good as the normalisation: ``+44 20 7946 0018`` and
    ``00442079460018`` are the same phone, and ``Alice@Example.COM `` is the
    same mailbox as ``alice@example.com``. Without this, an attacker (or plain
    sloppy data entry) evades linkage by changing punctuation.
    """
    v = unicodedata.normalize("NFKC", value or "").strip().lower()
    if not v:
        return ""

    if signal_type == "identity_email":
        # Whitespace and case only. Deliberately NOT stripping dots or +tags:
        # provider-specific aliasing rules differ, and guessing wrong would
        # merge two genuinely distinct mailboxes.
        return v

    if signal_type == "identity_phone":
        digits = re.sub(r"\D", "", v)
        # Strip one international prefix form so 00xx and +xx converge.
        if digits.startswith("00"):
            digits = digits[2:]
        return digits

    if signal_type == "identity_address":
        # Collapse whitespace and drop punctuation; address free-text varies
        # far more than it means.
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", v)).strip()

    return v


def signal_digest(value: str, *, dek: bytes, signal_type: str) -> str:
    """Return the tenant-keyed digest for an identity-bearing value.

    ``signal_type`` is folded into the digest so the same string used as two
    different signal types cannot cross-match.
    """
    norm = normalize(signal_type, value)
    if not norm:
        raise ValueError("cannot hash an empty value")
    if not dek:
        raise ValueError("a tenant DEK is required to hash identity signals")

    # signal_type is folded into the HASHED INPUT, not glued onto the output:
    # the same string used as two different signal types must not produce the
    # same digest, and the type is already its own column so repeating it in
    # the key would only waste width.
    #
    # BLAKE2b caps the key at 64 bytes; DEKs are 32, but clamp defensively
    # rather than let a longer key raise at runtime.
    h = hashlib.blake2b(
        f"{signal_type}|{norm}".encode("utf-8"),
        key=dek[:64],
        person=_PERSON,
        digest_size=_DIGEST_SIZE,
    )
    return h.hexdigest()

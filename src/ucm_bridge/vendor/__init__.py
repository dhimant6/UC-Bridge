"""Vendor API surface: verified call declarations and pluggable transports.

Every call this platform can make to a vendor system is declared here on an
allow-list, with a note of where the signature was verified and when. Connectors
call through a transport rather than talking to a vendor SDK directly, which is
what makes them cassette-testable offline and what keeps the full set of
production-reachable calls reviewable in one place.
"""

from ucm_bridge.vendor.cassette import (
    Cassette,
    CassetteMiss,
    RecordedInteraction,
    interaction_key,
)

__all__ = ["Cassette", "CassetteMiss", "RecordedInteraction", "interaction_key"]

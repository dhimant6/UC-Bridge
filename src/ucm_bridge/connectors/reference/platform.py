"""MemoryPBX: a fake UC platform used to exercise the connector contract.

Deliberately not a mock. It behaves like a real platform in the ways that make
connectors hard to write:

* it has its own native key scheme and its own record shapes, unrelated to the
  canonical model;
* it counts every read and every write, so a test can *prove* a discovery run
  wrote nothing rather than trusting that it did not;
* it can be configured to be eventually consistent, to fail a given key, or to
  throttle, so retry, confirm-poll, and quarantine paths are exercised rather
  than assumed.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


class MemoryPBXFault(Exception):
    """Raised by the fake platform. The connector translates these into ConnectorErrors."""

    def __init__(self, message: str, *, code: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


@dataclass
class MemoryPBXEstate:
    """An in-memory estate. One instance per 'cluster'."""

    instance_id: str
    read_only: bool = False

    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    lines: dict[str, dict[str, Any]] = field(default_factory=dict)
    numbers: dict[str, dict[str, Any]] = field(default_factory=dict)
    sites: dict[str, dict[str, Any]] = field(default_factory=dict)

    read_count: int = 0
    write_count: int = 0

    #: Native keys that should fail on write, mapped to a fault code.
    fail_on_write: dict[str, str] = field(default_factory=dict)
    #: Number of failed attempts remaining per key, for retry testing.
    transient_failures: dict[str, int] = field(default_factory=dict)
    #: When > 0, a written object only becomes readable after this many reads.
    replication_delay_reads: int = 0
    _pending_visibility: dict[str, int] = field(default_factory=dict)

    _COLLECTIONS = ("users", "lines", "numbers", "sites")

    # -- read -------------------------------------------------------------- #

    def collection(self, name: str) -> dict[str, dict[str, Any]]:
        if name not in self._COLLECTIONS:
            raise MemoryPBXFault(f"No such collection {name!r}", code="NOT_FOUND")
        return getattr(self, name)  # type: ignore[no-any-return]

    def list_records(self, collection: str) -> list[dict[str, Any]]:
        self.read_count += 1
        return [copy.deepcopy(r) for r in self.collection(collection).values()]

    def get_record(self, collection: str, key: str) -> dict[str, Any] | None:
        self.read_count += 1
        pending = self._pending_visibility.get(f"{collection}:{key}")
        if pending is not None:
            if pending > 1:
                self._pending_visibility[f"{collection}:{key}"] = pending - 1
                return None
            del self._pending_visibility[f"{collection}:{key}"]
        record = self.collection(collection).get(key)
        return copy.deepcopy(record) if record is not None else None

    # -- write ------------------------------------------------------------- #

    def upsert(self, collection: str, key: str, record: dict[str, Any]) -> dict[str, Any]:
        if self.read_only:
            raise MemoryPBXFault(
                f"Estate {self.instance_id} is read-only; refusing write to {collection}:{key}",
                code="READ_ONLY",
            )

        remaining = self.transient_failures.get(key, 0)
        if remaining > 0:
            self.transient_failures[key] = remaining - 1
            raise MemoryPBXFault(
                f"Transient failure writing {collection}:{key}",
                code="THROTTLED",
                retry_after=0.0,
            )

        fault_code = self.fail_on_write.get(key)
        if fault_code:
            raise MemoryPBXFault(
                f"Permanent failure writing {collection}:{key}", code=fault_code
            )

        self.write_count += 1
        stored = copy.deepcopy(record)
        self.collection(collection)[key] = stored
        if self.replication_delay_reads > 0:
            self._pending_visibility[f"{collection}:{key}"] = self.replication_delay_reads
        return copy.deepcopy(stored)

    def delete(self, collection: str, key: str) -> bool:
        if self.read_only:
            raise MemoryPBXFault(
                f"Estate {self.instance_id} is read-only; refusing delete of {collection}:{key}",
                code="READ_ONLY",
            )
        existed = self.collection(collection).pop(key, None) is not None
        if existed:
            self.write_count += 1
        return existed

    # -- test affordances --------------------------------------------------- #

    def state_fingerprint(self) -> dict[str, Any]:
        """Deep copy of all data, for asserting a source estate was not modified."""
        return {name: copy.deepcopy(self.collection(name)) for name in self._COLLECTIONS}

    def total_records(self) -> int:
        return sum(len(self.collection(name)) for name in self._COLLECTIONS)


def build_demo_estate(instance_id: str = "memorypbx-source") -> MemoryPBXEstate:
    """A small but non-trivial estate: two sites, three users, shared line, an ELIN.

    Includes the awkward cases on purpose: a user with no external number
    (a Teams blocker), and a site whose civic address lacks a sub-unit.
    """
    estate = MemoryPBXEstate(instance_id=instance_id)

    estate.sites["MUC-HQ"] = {
        "site_code": "MUC-HQ",
        "name": "Munich HQ",
        "country": "DE",
        "house_number": "7",
        "street": "Leopoldstrasse",
        "city": "Munich",
        "postal_code": "80802",
        "sub_unit": "Floor 3",
        "elin": "+498912345000",
        "validated_by": "Deutsche Telekom",
        "validated_on": "2026-03-14",
        "subnets": ["10.20.30.0/24"],
        "dynamic_location": True,
    }
    estate.sites["LON-BR"] = {
        "site_code": "LON-BR",
        "name": "London Branch",
        "country": "GB",
        "house_number": "22",
        "street": "Bishopsgate",
        "city": "London",
        "postal_code": "EC2N 4AJ",
        "sub_unit": None,
        "elin": "+442071838750",
        "validated_by": None,
        "validated_on": None,
        "subnets": ["10.40.50.0/24"],
        "dynamic_location": False,
    }

    estate.numbers["+498912345101"] = {
        "e164": "+498912345101",
        "site_code": "MUC-HQ",
        "number_type": "DID",
        "assigned_to": "amueller",
        "assignment_kind": "USER",
    }
    estate.numbers["+498912345102"] = {
        "e164": "+498912345102",
        "site_code": "MUC-HQ",
        "number_type": "DID",
        "assigned_to": "bschmidt",
        "assignment_kind": "USER",
    }
    estate.numbers["+442071838751"] = {
        "e164": "+442071838751",
        "site_code": "LON-BR",
        "number_type": "DID",
        "assigned_to": "cjones",
        "assignment_kind": "USER",
    }
    estate.numbers["+498912345000"] = {
        "e164": "+498912345000",
        "site_code": "MUC-HQ",
        "number_type": "ELIN",
        "assigned_to": None,
        "assignment_kind": None,
    }
    estate.numbers["+442071838750"] = {
        "e164": "+442071838750",
        "site_code": "LON-BR",
        "number_type": "ELIN",
        "assigned_to": None,
        "assignment_kind": None,
    }

    estate.users["amueller"] = {
        "username": "amueller",
        "first_name": "Anna",
        "last_name": "Mueller",
        "email": "anna.mueller@contoso.example",
        "department": "Finance",
        "site_code": "MUC-HQ",
        "extension": "5101",
        "did": "+498912345101",
        "enabled": True,
        "voice_enabled": True,
        "cor_class": "INTERNATIONAL",
    }
    estate.users["bschmidt"] = {
        "username": "bschmidt",
        "first_name": "Bruno",
        "last_name": "Schmidt",
        "email": "bruno.schmidt@contoso.example",
        "department": "Finance",
        "site_code": "MUC-HQ",
        "extension": "5102",
        "did": "+498912345102",
        "enabled": True,
        "voice_enabled": True,
        "cor_class": "NATIONAL",
    }
    estate.users["cjones"] = {
        "username": "cjones",
        "first_name": "Cerys",
        "last_name": "Jones",
        "email": "cerys.jones@contoso.example",
        "department": "Sales",
        "site_code": "LON-BR",
        "extension": "7301",
        "did": "+442071838751",
        "enabled": True,
        "voice_enabled": True,
        "cor_class": "NATIONAL",
    }
    estate.users["warehouse"] = {
        # No DID: a Teams Phone blocker the assessment engine must catch.
        "username": "warehouse",
        "first_name": "Warehouse",
        "last_name": "Handset",
        "email": None,
        "department": "Operations",
        "site_code": "MUC-HQ",
        "extension": "5900",
        "did": None,
        "enabled": True,
        "voice_enabled": True,
        "cor_class": "INTERNAL_ONLY",
    }

    estate.lines["MUC-HQ:5101"] = {
        "line_id": "MUC-HQ:5101",
        "extension": "5101",
        "site_code": "MUC-HQ",
        "label": "Anna Mueller",
        "owner": "amueller",
        "did": "+498912345101",
        "shared_with": [],
        "button": 1,
    }
    estate.lines["MUC-HQ:5102"] = {
        "line_id": "MUC-HQ:5102",
        "extension": "5102",
        "site_code": "MUC-HQ",
        "label": "Bruno Schmidt",
        "owner": "bschmidt",
        "did": "+498912345102",
        "shared_with": [],
        "button": 1,
    }
    estate.lines["MUC-HQ:5199"] = {
        # Shared line: Anna and Bruno both hold an appearance. A wave-planning
        # dependency, and a fidelity risk on most cloud targets.
        "line_id": "MUC-HQ:5199",
        "extension": "5199",
        "site_code": "MUC-HQ",
        "label": "Finance Hotline",
        "owner": "amueller",
        "did": None,
        "shared_with": ["bschmidt"],
        "button": 2,
    }
    estate.lines["LON-BR:7301"] = {
        "line_id": "LON-BR:7301",
        "extension": "7301",
        "site_code": "LON-BR",
        "label": "Cerys Jones",
        "owner": "cjones",
        "did": "+442071838751",
        "shared_with": [],
        "button": 1,
    }
    estate.lines["MUC-HQ:5900"] = {
        "line_id": "MUC-HQ:5900",
        "extension": "5900",
        "site_code": "MUC-HQ",
        "label": "Warehouse",
        "owner": "warehouse",
        "did": None,
        "shared_with": [],
        "button": 1,
    }

    # Reads performed during construction are setup, not discovery.
    estate.read_count = 0
    estate.write_count = 0
    return estate

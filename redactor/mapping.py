"""The entity registry and the encrypted mapping key.

One :class:`MappingStore` covers a single run, so every document in a batch
sees the same substitutions.  The store also writes the mapping key - the
original-to-replacement table - to its own AES-encrypted file that is
deliberately kept *outside* the delivered archive.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import categories, names as _names, surrogates

KDF_ITERATIONS = 480_000


@dataclass
class Entity:
    """One real-world thing being replaced everywhere it appears."""

    key: str                       # casefolded canonical form - the identity
    category: str
    canonical: str                 # as the operator sees it
    replacement: str               # placeholder, or the surrogate's full name
    role: str = ""
    source: str = "pattern"        # pattern | name-list | ner | caption
    person: _names.PersonName | None = None
    surrogate: _names.PersonName | None = None
    variants: list[_names.NameVariant] = field(default_factory=list)
    enabled: bool = True
    occurrences: int = 0
    documents: set[str] = field(default_factory=set)
    notes: str = ""

    @property
    def is_person(self) -> bool:
        return categories.style_for(self.category) == "person"

    @property
    def label(self) -> str:
        return categories.label_for(self.category)


class MappingStore:
    """Assigns and remembers every replacement used in a run."""

    def __init__(self) -> None:
        self.entities: dict[str, Entity] = {}
        self._counters: dict[str, int] = {}
        self._used_replacements: set[str] = set()
        self.started = datetime.now(timezone.utc)
        self.filenames: dict[str, str] = {}

    # ------------------------------------------------------------- people --
    def add_person(
        self,
        full_name: str,
        category: str = "person",
        role: str = "",
        source: str = "name-list",
        include_single_tokens: bool = True,
    ) -> Entity | None:
        parsed = _names.parse(full_name)
        if not parsed.canonical:
            return None
        key = f"{category}:{parsed.canonical.casefold()}"
        existing = self.entities.get(key)
        if existing:
            if role and not existing.role:
                existing.role = role
            return existing

        surrogate = self._unique_person_surrogate(parsed)
        entity = Entity(
            key=key,
            category=category,
            canonical=parsed.canonical,
            replacement=surrogate.canonical,
            role=role,
            source=source,
            person=parsed,
            surrogate=surrogate,
            variants=_names.variants(parsed, include_single_tokens=include_single_tokens),
        )
        self.entities[key] = entity
        self._used_replacements.add(surrogate.canonical.casefold())
        # An earlier person may have been handed a surrogate that IS this new
        # real person's name - presenting a real party's true name as someone
        # else's fake would de-anonymize them. Re-issue any such surrogate.
        # All clashing surrogates are cleared before regenerating, or the
        # shared-family-surname scan would just echo the clashing surname back.
        clashing = []
        for other in self.entities.values():
            if other is entity or not other.is_person or other.surrogate is None:
                continue
            if other.person is None:
                continue
            clash = (other.surrogate.canonical.casefold() == parsed.canonical.casefold()
                     or (parsed.last and other.surrogate.last
                         and other.surrogate.last.casefold() == parsed.last.casefold()))
            if clash:
                clashing.append(other)
        for other in clashing:
            self._used_replacements.discard(other.surrogate.canonical.casefold())
            other.surrogate = None
        for other in clashing:
            other.surrogate = self._unique_person_surrogate(other.person)
            other.replacement = other.surrogate.canonical
            self._used_replacements.add(other.surrogate.canonical.casefold())
        return entity

    def _real_person_names(self) -> set[str]:
        """Real names (and surnames) no surrogate may collide with."""
        out: set[str] = set()
        for entity in self.entities.values():
            if entity.is_person and entity.person is not None:
                out.add(entity.person.canonical.casefold())
                if entity.person.last:
                    out.add(entity.person.last.casefold())
        return out

    def _shared_surrogate_surname(self, last: str) -> str | None:
        """People who share a surname must share a fake surname.

        Two parties named Smith are the common case in family law, and if they
        were given different fake surnames a bare "Smith" in the text would be
        ambiguous - it would silently attach to whichever entity happened to be
        registered first.  Keeping the family name shared makes the bare form
        unambiguous and reads the way a real document does.
        """
        if not last:
            return None
        for entity in self.entities.values():
            if not entity.is_person or not entity.person or not entity.surrogate:
                continue
            if entity.person.last and entity.person.last.casefold() == last.casefold():
                return entity.surrogate.last
        return None

    def _unique_person_surrogate(self, parsed: _names.PersonName) -> _names.PersonName:
        want_middle = bool(parsed.middles)
        shared_last = self._shared_surrogate_surname(parsed.last)
        real = self._real_person_names()
        for attempt in range(64):
            candidate = surrogates.person(parsed.canonical, want_middle, attempt)
            if shared_last:
                candidate = _names.PersonName(
                    raw=f"{candidate.first} {shared_last}",
                    first=candidate.first,
                    middles=candidate.middles,
                    last=shared_last,
                )
            folded = candidate.canonical.casefold()
            if folded in self._used_replacements:
                continue
            if folded == parsed.canonical.casefold():
                continue
            # never hand out a real registered person's name - or surname,
            # unless it is the deliberately shared family surrogate - as fake
            if folded in real:
                continue
            if (not shared_last and candidate.last
                    and candidate.last.casefold() in real):
                continue
            return candidate
        return surrogates.person(parsed.canonical, want_middle, 999)

    # -------------------------------------------------------------- values --
    def add_value(self, category: str, original: str, source: str = "pattern") -> Entity:
        """Register a non-person value and assign it a tagged placeholder."""
        norm = " ".join(original.split())
        key = f"{category}:{norm.casefold()}"
        existing = self.entities.get(key)
        if existing:
            return existing

        tag = categories.tag_for(category)
        self._counters[tag] = self._counters.get(tag, 0) + 1
        entity = Entity(
            key=key,
            category=category,
            canonical=norm,
            replacement=surrogates.placeholder(tag, self._counters[tag]),
            source=source,
        )
        self.entities[key] = entity
        return entity

    # --------------------------------------------------------------- misc --
    def get(self, category: str, original: str) -> Entity | None:
        return self.entities.get(f"{category}:{' '.join(original.split()).casefold()}")

    def active(self) -> list[Entity]:
        return [e for e in self.entities.values() if e.enabled]

    def persons(self) -> list[Entity]:
        return [e for e in self.entities.values() if e.is_person]

    def record_hit(self, entity: Entity, document: str, count: int = 1) -> None:
        entity.occurrences += count
        entity.documents.add(document)

    def register_filename(self, original: str, replacement: str) -> None:
        self.filenames[original] = replacement

    # -------------------------------------------------------------- export --
    def rows(self) -> list[dict]:
        out = []
        for entity in sorted(
            self.entities.values(), key=lambda e: (e.category, e.canonical.casefold())
        ):
            out.append(
                {
                    "category": entity.category,
                    "category_label": entity.label,
                    "original": entity.canonical,
                    "replacement": entity.replacement,
                    "role": entity.role,
                    "source": entity.source,
                    "applied": "yes" if entity.enabled else "no",
                    "occurrences": entity.occurrences,
                    "documents": "; ".join(sorted(entity.documents)),
                }
            )
        return out

    def to_json(self) -> str:
        payload = {
            "tool": "Anonymizer / Redactor",
            "generated_utc": self.started.isoformat(),
            "entities": self.rows(),
            "filenames": [
                {"original": k, "replacement": v} for k, v in sorted(self.filenames.items())
            ],
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)

    def to_csv(self) -> str:
        rows = self.rows()
        buf = io.StringIO()
        fields = [
            "category_label", "original", "replacement", "role",
            "source", "applied", "occurrences", "documents",
        ]
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        if self.filenames:
            buf.write("\n")
            writer2 = csv.writer(buf)
            writer2.writerow(["original filename", "delivered filename"])
            for original, replacement in sorted(self.filenames.items()):
                writer2.writerow([original, replacement])
        return buf.getvalue()


# --------------------------------------------------------------------------
# encryption
# --------------------------------------------------------------------------


def _fernet_for(password: str, salt: bytes) -> Fernet:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=KDF_ITERATIONS
    )
    return Fernet(base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8"))))


def write_encrypted_key(store: MappingStore, path: Path, password: str) -> Path:
    """Write the mapping key as a password-protected file."""
    if not password:
        raise ValueError("a password is required to write the mapping key")
    salt = os.urandom(16)
    token = _fernet_for(password, salt).encrypt(store.to_json().encode("utf-8"))
    envelope = {
        "format": "anonymizer-redactor-mapping-key",
        "version": 1,
        "kdf": {"name": "PBKDF2HMAC-SHA256", "iterations": KDF_ITERATIONS,
                "salt": base64.b64encode(salt).decode("ascii")},
        "ciphertext": token.decode("ascii"),
    }
    path = Path(path)
    path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:      # pragma: no cover - filesystem dependent
        pass
    return path


def read_encrypted_key(path: Path, password: str) -> dict:
    """Decrypt a mapping key written by :func:`write_encrypted_key`."""
    envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    salt = base64.b64decode(envelope["kdf"]["salt"])
    plaintext = _fernet_for(password, salt).decrypt(envelope["ciphertext"].encode("ascii"))
    return json.loads(plaintext.decode("utf-8"))

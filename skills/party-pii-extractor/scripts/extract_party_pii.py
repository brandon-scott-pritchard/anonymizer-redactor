#!/usr/bin/env python3
"""Pull every party's identifying information out of a folder of case files.

One command, run again after each step it asks for:

    python extract_party_pii.py run INPUT [--out DIR] [--single-case]

INPUT is a folder or a .zip. Each top-level subfolder is one case (or the whole
input is one case with ``--single-case``), and each case gets one CSV for the
operator to type into the Anonymizer's party sheet.

The work is deterministic - the same files give the same CSV - and done here
rather than by the model, which only does what a rule cannot:

1. **scan**: every file is read into text. A page with no text layer is OCR'd
   when Tesseract is installed; otherwise, or when OCR reads it poorly, the page
   is rendered to an image and queued for a model to transcribe.
2. **parties**: who the petitioner, respondent, children and other parties are,
   from the captions, the child rosters and the former-name clauses - or from
   ``--petitioner`` / ``--respondent``, or a hand-edited ``parties.json``.
3. **analyze**: the Anonymizer's own detectors run over every page; each value
   is tied to the party whose name is on its line or in the block above it,
   which is the rule the app itself uses. What no rule can tie to anybody is
   written to ``review.json`` with a few lines of context for the model.
4. **finalize**: the model's decisions are merged and the CSV written.

The command prints one ``NEXT:`` line saying what it needs, and exits:
0 done, 2 transcriptions needed, 3 parties needed, 4 review decisions needed,
1 an error.

Values never go to standard output: the CSV holds full SSNs and account
numbers, so what is printed is counts.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# the Anonymizer's own detectors, trimmed to what this script uses (partycore/),
# and the file readers both skills share (partyread.py): beside this script in a
# packaged skill, in skills/_shared/ in the source tree
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if not (HERE / "partycore").is_dir():
    sys.path.insert(0, str(HERE.parents[1] / "_shared"))
from partycore import caption, children, forms, names, patterns, places, transactions  # noqa: E402
# re-exported too: the tests and the eval reach the readers through this module
from partyread import (  # noqa: E402,F401
    TEXT_EXT, IMAGE_EXT, VIEWABLE, SKIP_NAMES, MIN_TEXT_CHARS, SCAN_TEXT_CHARS,
    OCR_MIN_CONFIDENCE, RENDER_DPI, OCR_DPI, Unreadable, Page, _have,
    _fitz, tesseract, ocr_image, _xml_text, W, read_docx,
    read_odt, _BUILTIN_DATE_FORMATS, _col, read_xlsx, _HTMLText, html_text,
    rtf_text, read_legacy_doc, Scan, read_pdf, THIN_LAYER_CHARS, _has_text,
    _read_pdf_fallback, _name_by_content, _plumber_coverage, _queue_image, _ocr_or_queue, read_image,
    read_email, read_msg, read_any, _named, _safe_extract, EXCLUDE,
    excluded, walk, SECTION_WORDS, find_cases, looks_like_sections, safe_name,
    save_json, load_json, scan_case, load_pages, UNREADABLE, unread_transcripts,
    missing_transcripts, _norm, _FOLD, _fold)

# --------------------------------------------------------------------------
# the party sheet's fields, in the order the app shows them
# --------------------------------------------------------------------------

FIELD_LABEL = {"name": "Names", "dob": "Date of birth", "ssn": "SSN", "address": "Addresses",
               "phone": "Phones", "email": "Emails", "tin": "TIN / EIN",
               "dl": "Driver's licence", "passport": "Passport", "employer": "Employers",
               "business": "Businesses", "account": "Accounts", "card": "Cards",
               "handle": "Handles", "vehicle": "Vehicles", "case_number": "Case numbers",
               "parcel": "Parcels", "other": "Other identifiers"}
FIELD_ORDER = list(FIELD_LABEL)

# What a detector's find is, as a kind of party value - a copy of the app's
# parties.KIND_OF and parties.LEAVE (a test keeps them equal). Anything not
# here is left as written by the app, so it is not worth typing in.
KIND_OF = {"ssn": "ssn", "ein": "tin", "phone": "phone", "fax": "phone", "dob": "dob",
           "street_address": "address", "bank_account": "account",
           "masked_account": "account", "investment_account": "account",
           "credit_card": "card", "email": "email", "social_handle": "handle",
           "payment_handle": "handle", "username": "handle", "drivers_license": "dl",
           "passport": "passport", "vin": "vehicle", "license_plate": "vehicle",
           "case_number": "case_number", "parcel_number": "parcel",
           "deed_reference": "parcel", "legal_description": "parcel",
           "alien_number": "other", "military_id": "other", "mrn": "other",
           "student_id": "other", "voter_id": "other", "inmate_number": "other",
           "tribal_id": "other", "vehicle_title": "vehicle", "vessel_number": "vehicle",
           "iban": "account", "crypto_wallet": "account"}
LEAVE = frozenset({"policy_number", "claim_number", "health_plan", "loan_number",
                   "employee_id", "check_number", "order_number", "tracking_number",
                   "routing_number", "case_designator"})
CASE_LEVEL = {"case_number", "parcel"}      # the case's, not one party's

# Anchoring - copied from the app's parties.py (a test keeps them equal).
HEADER_LINES = 15
BLOCK_LINES = 4
SIGNATURE_LINES = 8          # counsel's signature block: name, bar no., firm, address, phone, email
_HEADER_LABEL = re.compile(
    r"(?i)\b(?:account\s+holders?|account\s+(?:number|no\.?|#)|acct\.?\s*(?:number|no\.?|#)"
    r"|account\s+ending|card\s+(?:ending|number|no\.?|#)|statement\s+for)")
_TRANSFER_LINE = re.compile(
    r"(?i)\b(?:transfer|xfer|trnsfr|tfr|zelle|venmo|cash\s?app|paypal|withdrawal|deposit|"
    r"payment|pmt|ach|online|mobile|to|from|card|acct|account|checking|savings)\b")
_LEAVE_LINE = re.compile(
    r"(?i)\b(?:loans?|mortgages?|heloc|polic(?:y|ies)|claims?|members?(?:ship)?|lease|lien|"
    r"escrow|student|auto\s+fin(?:ance)?)\b")
_ACTIVITY_ROW = re.compile(
    r"^\s*(?:\d{1,2}/\d{1,2}(?![/\-.]?\d)|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"[a-z]*\.?[^\S\n]+\d{1,2}(?!\d)(?!,?[^\S\n]*\d{4}))", re.IGNORECASE)
_ACTIVITY_HEADING = re.compile(
    r"^\s*(?:(?:account\s+)?activity|transactions?(?:\s+(?:history|detail))?|"
    r"date\s+description|deposits(?:\s+and\s+(?:other\s+)?credits)?|withdrawals|"
    r"purchases|payments\s+and\s+credits)\b", re.IGNORECASE)
_LOCKBOX_OR_BANK = re.compile(
    r"(?i)\b(?:p\.?\s?o\.?\s+box|post\s+office\s+box|lockbox|bank|bancorp|credit\s+union|"
    r"n\.a\.|mortgage|servicing|lending|insurance|financial|card\s+services|remit)\b")

# --------------------------------------------------------------------------
# parties
# --------------------------------------------------------------------------

PETITIONER_WORDS = ("petitioner", "plaintiff", "appellant", "movant", "applicant", "claimant",
                    "co-petitioner", "obligee")
RESPONDENT_WORDS = ("respondent", "defendant", "appellee", "obligor", "co-respondent")
NON_PARTY_WORDS = ("attorney", "guardian ad litem", "judge", "commissioner")


def _role_of(role: str) -> str:
    r = role.casefold()
    if r.startswith("signatory"):
        return "signatory"           # a /s/ line: counsel or a party - never assumed either
    if any(w in r for w in NON_PARTY_WORDS):
        return "nonparty"
    if "minor" in r or "child" in r or r == "ward":
        return "child"
    if r.startswith(("counter-claimant", "counterclaimant", "counter-petitioner")):
        return "respondent"
    if r.startswith(("counter-defendant", "counterdefendant", "counter-respondent")):
        return "petitioner"
    if any(r.startswith(w) for w in PETITIONER_WORDS):
        return "petitioner"
    if any(r.startswith(w) for w in RESPONDENT_WORDS):
        return "respondent"
    if r.startswith("caption party"):
        return "caption"
    return "other"


# what marks a signature block as counsel's: never just a /s/ line, which a party
# signs too
ATTORNEY_CUE = re.compile(r"(?i)\b(?:attorneys?\s+for|counsel\s+for|bar\s+(?:no|number|#)|"
                          r"esq\b|law\s+(?:firm|office|group)|family\s+law|pllc|llp|p\.c\.)")


def _counsel_signs(text: str, name: str) -> bool:
    """An attorney cue within three lines of the signer's name."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if name.casefold() in " ".join(line.split()).casefold():
            if ATTORNEY_CUE.search("\n".join(lines[max(0, i - 1):i + 4])):
                return True
    return False


def _same(a: str, b: str) -> bool:
    pa, pb = names.parse(a), names.parse(b)
    if names.same_person(pa, pb):
        return True
    return bool(pa.last and pb.last and names.alike(pa.last, pb.last)
                and pa.first and pb.first and names.same_given(pa.first, pb.first))


def _richest(forms: list[str]) -> str:
    return max(forms, key=lambda f: (names.richness(names.parse(f)), not f.isupper(), f))


def _person(name_list: list[str]) -> dict:
    return {"names": name_list}


def harvest_parties(pages: list[Page]) -> dict:
    """Who the parties are, from captions, rosters and former-name clauses."""
    regions: dict[str, str] = {}
    for p in pages:
        if p.first:
            regions[f"{p.file}#{p.page}"] = caption.caption_region(p.text)
        else:
            # a later page's running header
            regions[f"{p.file}#{p.page}"] = "\n".join(p.text.splitlines()[:6])
    found = caption.harvest_documents(regions)

    buckets: dict[str, list[str]] = defaultdict(list)
    order: list[tuple[str, str]] = []
    for cand in found:
        role = _role_of(cand.role)
        if cand.confidence == "low" or (role == "caption" and cand.confidence != "high"):
            if role != "nonparty":
                continue
        buckets[role].append(cand.name)
        order.append((role, cand.name))

    def people(forms: list[str]) -> list[list[str]]:
        groups: list[list[str]] = []
        for f in forms:
            for g in groups:
                if any(_same(f, h) for h in g):
                    g.append(f)
                    break
            else:
                groups.append([f])
        return groups

    pet = people(buckets["petitioner"])
    res = people(buckets["respondent"])
    caps = [g for g in people(buckets["caption"])
            if not any(_same(g[0], h) for grp in pet + res for h in grp)]
    # "In re the Marriage of X and Y" and "X v. Y" put the petitioner first
    if not pet and caps:
        pet.append(caps.pop(0))
    if not res and caps:
        res.append(caps.pop(0))
    others = people(buckets["other"]) + caps
    others += pet[1:] + res[1:]
    # counsel signs at the foot of a pleading, below the caption region: their
    # phone, address and email there are nobody's in the case
    feet = {}
    for p in pages:
        lines = p.text.splitlines()
        feet[f"{p.file}#{p.page}#foot"] = "\n".join(lines[-30:])
    for label, text in feet.items():
        for cand in caption.harvest(text, label):
            role = _role_of(cand.role)
            if role == "nonparty" or (role == "signatory" and _counsel_signs(text, cand.name)):
                buckets["nonparty"].append(cand.name)
    # a party can sign under another name ("/s/ Rowena Radcliffe" after a decree
    # restores it): someone who shares a party's given name, or is named in a
    # former-name clause, is never taken for counsel
    party_forms = [h for g in pet + res + others for h in g]
    clause_names = {n.casefold() for p in pages for pair in caption.former_names(p.text)
                    for n in pair}
    party_firsts = {names.parse(h).first.casefold() for h in party_forms if names.parse(h).first}
    nonparty = sorted({n for n in buckets["nonparty"]
                       if not any(_same(n, h) for h in party_forms)
                       and n.casefold() not in clause_names
                       and names.parse(n).first.casefold() not in party_firsts})

    kids: dict[str, dict] = {}
    for p in pages:
        for child in children.harvest(p.text, p.tables, p.file):
            if child.is_initials:
                key = child.name.replace(".", "").replace(" ", "").upper()
            else:
                key = child.key
            entry = kids.setdefault(key, {"names": [child.name], "dob": ""})
            if child.born and not entry["dob"]:
                entry["dob"] = child.born
    for g in people(buckets["child"]):
        if not any(_same(g[0], n) for k in kids.values() for n in k["names"]):
            kids[g[0].casefold()] = {"names": [_richest(g)], "dob": ""}
    # a child is never also a parent
    kid_list = [k for k in kids.values()
                if not any(_same(k["names"][0], h) for g in pet[:1] + res[:1] for h in g)]

    former: list[tuple[str, str]] = []
    for p in pages:
        former += caption.former_names(p.text)

    def written(name: str) -> str:
        """An ALL-CAPS caption name as the body of the document writes it."""
        if not name.isupper():
            return name
        pattern = re.compile(r"\s+".join(re.escape(t) for t in name.split()), re.IGNORECASE)
        for p in pages:
            for m in pattern.finditer(p.text):
                if not m.group(0).isupper():
                    return " ".join(m.group(0).split())
        return name

    def entry(group: list[str]) -> dict:
        canonical = written(_richest(group))
        alts = []
        for cur, old in former:
            if _same(cur, canonical) and old not in alts:
                alts.append(old)
            elif _same(old, canonical) and cur not in alts:
                alts.append(cur)
        return {"names": [canonical] + [a for a in alts if a.casefold() != canonical.casefold()]}

    out = {
        "status": "auto",
        "petitioner": entry(pet[0]) if pet else {"names": []},
        "respondent": entry(res[0]) if res else {"names": []},
        "children": kid_list,
        "others": [entry(g) for g in others],
        "non_parties": nonparty,
    }
    if not pet or not res:
        out["status"] = "needs_input"
        holders: Counter = Counter()
        for p in pages:
            for h in transactions.account_holders(p.text, p.file):
                holders[h.name] += 1
        out["candidates"] = [n for n, _ in holders.most_common(12)]
    return out


# where a financial record names a child: an EOB's or a bill's patient, a 1040's
# dependents, a school's student
HOUSEHOLD_LABEL = re.compile(r"(?i)\b(?:patient|dependents?|student|child(?:ren)?|son|daughter|"
                             r"minor)\b[ \t]*:?")


def household_children(parties: dict, pages: list[Page]) -> list[dict]:
    """Children no roster lists: people under a patient, dependent or student
    label who carry the family surname and are neither parent. Proposed in the
    PARTIES block for the model to confirm, never assumed silently."""
    parents = [n for role in ("petitioner", "respondent")
               for n in parties.get(role, {}).get("names", []) if n.strip()]
    surnames = {names.parse(n).last.casefold() for n in parents if names.parse(n).last}
    if not surnames:
        return []
    known = parents + [n for k in parties.get("children", []) for n in k.get("names", [])]
    found: dict[str, list[str]] = {}
    for page in pages:
        for m in HOUSEHOLD_LABEL.finditer(page.text):
            end = page.text.find("\n", page.text.find("\n", m.end()) + 1)
            window = page.text[m.end():end if end > 0 else None]
            for run in ONE_PERSON.finditer(window):
                cand = names.parse(run.group(0))
                if not (cand.first and cand.last and len(cand.first.strip(".")) > 1):
                    continue
                if not any(names.alike(cand.last, s) for s in surnames):
                    continue
                text = " ".join(run.group(0).split())
                if any(_same(text, k) for k in known):
                    continue
                for group in found.values():
                    if _same(text, group[0]):
                        group.append(text)
                        break
                else:
                    found[text.casefold()] = [text]
    return [{"names": [written_form(_richest(g), pages)], "dob": "",
             "source": "named under a patient, dependent or student label"}
            for g in found.values()]


def written_form(name: str, pages: list[Page]) -> str:
    """An ALL-CAPS name as the body of a document writes it, where one does."""
    if not name.isupper():
        return name
    pattern = re.compile(r"\s+".join(re.escape(t) for t in name.split()), re.IGNORECASE)
    for p in pages:
        for m in pattern.finditer(p.text):
            if not m.group(0).isupper():
                return " ".join(m.group(0).split())
    return name


def check_parties(parties) -> list[str]:
    """What is wrong with a parties.json a model or a person edited, named
    exactly - never a crash, never a party silently missing."""
    if not isinstance(parties, dict):
        return ["it is not one JSON object"]
    problems = []
    if parties.get("status") not in ("auto", "given", "confirmed", "needs_input"):
        problems.append('"status" must be "confirmed" once you have checked it')
    for role in ("petitioner", "respondent"):
        entry = parties.get(role, {"names": []})
        if not isinstance(entry, dict) or not isinstance(entry.get("names", []), list) \
                or not all(isinstance(n, str) for n in entry.get("names", [])):
            problems.append(f'"{role}" must look like {{"names": ["Full Name", ...]}}')
    for key in ("children", "others"):
        group = parties.get(key, [])
        if not isinstance(group, list):
            problems.append(f'"{key}" must be a list')
            continue
        for i, entry in enumerate(group, 1):
            if not isinstance(entry, dict) or not isinstance(entry.get("names"), list) \
                    or not entry["names"] or not all(isinstance(n, str) for n in entry["names"]):
                problems.append(f'"{key}" entry {i} must look like {{"names": ["Full Name"]}}'
                                + (' (it says "name", not "names")' if isinstance(entry, dict)
                                   and "name" in entry else ""))
    if not isinstance(parties.get("non_parties", []), list):
        problems.append('"non_parties" must be a list of names')
    if parties.get("status") == "confirmed" and not (
            parties.get("petitioner", {}).get("names") or parties.get("respondent", {}).get("names")):
        problems.append("a confirmed sheet needs at least a petitioner's or a respondent's name")
    return problems


def apply_cli_parties(parties: dict, args) -> dict:
    if args.petitioner:
        parties["petitioner"] = {"names": list(args.petitioner)}
    if args.respondent:
        parties["respondent"] = {"names": list(args.respondent)}
    if args.child:
        parties["children"] = [{"names": [c], "dob": ""} for c in args.child]
    if args.other:
        parties["others"] = [{"names": [o]} for o in args.other]
    if args.petitioner or args.respondent:
        parties["status"] = "given"
    return parties


def party_labels(parties: dict) -> list[tuple[str, dict]]:
    """(CSV role label, entry) for every party, in the sheet's order."""
    out = []
    if parties.get("petitioner", {}).get("names"):
        out.append(("Petitioner", parties["petitioner"]))
    if parties.get("respondent", {}).get("names"):
        out.append(("Respondent", parties["respondent"]))
    for i, kid in enumerate(parties.get("children", []), 1):
        if kid.get("names"):
            out.append((f"Child {i}", kid))
    for i, other in enumerate(parties.get("others", []), 1):
        if other.get("names"):
            out.append((f"Other party {i}", other))
    return out


# --------------------------------------------------------------------------
# finding a party's name on a line
# --------------------------------------------------------------------------

BOTH = "Both parties"
CASE = "Case"


class NameIndex:
    """Which party's name is written where - the basis for tying a value to a party."""

    def __init__(self, labels: list[tuple[str, dict]], non_parties: list[str]):
        self.labels = [label for label, _ in labels]
        persons: dict[str, list[names.PersonName]] = {}
        for label, entry in labels:
            persons[label] = [names.parse(n) for n in entry.get("names", []) if n.strip()]
        for i, n in enumerate(non_parties):
            persons[f"~nonparty{i}"] = [names.parse(n)]
        self.persons = persons
        surnames: dict[str, set[str]] = defaultdict(set)
        firsts: dict[str, set[str]] = defaultdict(set)
        for label, people in persons.items():
            for p in people:
                if p.last:
                    surnames[p.last.casefold()].add(label)
                if p.first and len(p.first.strip(".")) > 1:
                    firsts[p.first.casefold()].add(label)
        self.family = {s for s, owners in surnames.items()
                       if len({o for o in owners if not o.startswith("~")}) > 1}
        self.patterns: list[tuple[re.Pattern, str]] = []
        for label, people in persons.items():
            multi, single_ci, single_cs, towns = set(), set(), set(), set()
            for p in people:
                for v in names.variants(p):
                    words = v.text.split()
                    if len(words) > 1:
                        multi.add(v.text)
                        continue
                    word = v.text.strip(".").casefold()
                    if len(word) < 3 or label.startswith("~"):
                        continue                   # a non-party is matched by full name only
                    if places.known(word):
                        # "Brooklyn", "Jordan": a party's given name that is also a town
                        # is matched only as a name is written - capitalised, not
                        # followed by a state - never "SOUTH JORDAN, UT"
                        if word in firsts:
                            towns.add(v.text.strip(".").title())
                        continue
                    if word in surnames and (word in self.family or len(surnames[word]) > 1):
                        continue                   # the family's, handled below
                    if word in firsts and len(firsts[word]) > 1:
                        continue                   # two parties share the given name
                    if v.everyday or v.risky:
                        # an everyday word ("Hope", "Baker") only with its capital -
                        # as a caption shouts it and as a sentence writes it
                        single_cs |= {v.text, v.text.title(), v.text.upper()}
                    else:
                        single_ci.add(v.text)
            if multi or single_ci:
                self.patterns.append((self._regex(multi | single_ci, True), label))
            if single_cs:
                self.patterns.append((self._regex(single_cs, False), label))
            if towns:
                self.patterns.append((re.compile(
                    rf"(?<![\w'’])(?:{'|'.join(map(re.escape, sorted(towns)))})(?![\w'’])"
                    rf"(?!,?\s+(?:[A-Z]{{2}}\b|Utah|City|County))"), label))
        if self.family:
            plain = {f for f in self.family if not places.known(f)}
            towns = {f.title() for f in self.family if places.known(f)}
            if plain:
                self.patterns.append((self._regex(plain, True), BOTH))
            if towns:
                # a family named Jordan or Logan: never "SOUTH JORDAN, UT"
                self.patterns.append((re.compile(
                    rf"(?<![\w'’])(?:{'|'.join(map(re.escape, sorted(towns)))})(?![\w'’])"
                    rf"(?!,?\s+(?:[A-Z]{{2}}\b|Utah|City|County))"), BOTH))
        # "Petitioner's Venmo handle", "the Respondent's SSN": the role word names
        # the party as surely as a name does - unless it is counsel's line
        self.roles: list[tuple[re.Pattern, str]] = []
        if "Petitioner" in self.labels:
            self.roles.append((PETITIONER_WORD, "Petitioner"))
        if "Respondent" in self.labels:
            self.roles.append((RESPONDENT_WORD, "Respondent"))
        self.roles.append((PARTIES_WORD, BOTH))

    @staticmethod
    def _regex(forms, ignore_case: bool) -> re.Pattern:
        def token(t: str) -> str:
            # "K." in a name matches "K" too: "ROWENA K ASHDOWN" in a statement header
            if re.fullmatch(r"[A-Za-z]\.", t):
                return re.escape(t[0]) + r"\.?"
            return names.escape_token(t)
        bodies = sorted({r"\s+".join(token(t) for t in f.split()) for f in forms},
                        key=len, reverse=True)
        flags = re.IGNORECASE if ignore_case else 0
        return re.compile(rf"{names.BOUNDARY_LEFT}(?:{'|'.join(bodies)}){names.BOUNDARY_RIGHT}",
                          flags)

    def mentions_by_line(self, lines: list[str]) -> dict[int, list[tuple[int, int, str]]]:
        """Line -> every (start, end, owner) of a party's name or role word on it."""
        out: dict[int, list[tuple[int, int, str]]] = {}
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            found = []
            for regex, label in self.patterns:
                found += [(m.start(), m.end(), label) for m in regex.finditer(line)
                          if not someone_elses(line, m.end())]
            for regex, label in self.roles:
                found += [(m.start(), m.end(), label) for m in regex.finditer(line)
                          if not COUNSEL_BEFORE.search(line[max(0, m.start() - 40):m.start()])
                          and not someone_elses(line, m.end())]
            if found:
                out[i] = sorted(found)
        return out


PETITIONER_WORD = re.compile(r"(?i)\b(?:petitioner|plaintiff)(?:['’]s)?\b(?![\s-]*(?:and|&)\s+"
                             r"(?:respondent|defendant))")
RESPONDENT_WORD = re.compile(r"(?i)\b(?:respondent|defendant)(?:['’]s)?\b")
PARTIES_WORD = re.compile(r"(?i)\b(?:the\s+parties|both\s+parties|petitioner\s+and\s+respondent|"
                          r"plaintiff\s+and\s+defendant|jointly)\b")
COUNSEL_BEFORE = re.compile(r"(?i)(?:attorneys?|counsel|lawyers?|representing|on\s+behalf\s+of|"
                            r"guardian\s+ad\s+litem)\s+(?:for\s+)?(?:the\s+)?$")
# "Petitioner's counsel", "Respondent's father", "Rowena's employer, Red Rock...":
# the line is about someone else - the possessive names the relationship, not
# the owner of what follows
def someone_elses(line: str, end: int) -> bool:
    """Is the name or role word ending at ``end`` a possessive naming somebody
    else - "Petitioner's counsel", "Rowena's father", "the Smiths' landlord"?"""
    if line[max(0, end - 2):end] in ("'s", "’s"):
        rest = line[end:]
    elif line[end:end + 2] in ("'s", "’s"):
        rest = line[end + 2:]
    elif line[end:end + 1] in ("'", "’"):
        rest = line[end + 1:]
    else:
        return False
    return bool(SOMEONE_ELSES.match(rest[:60]))


SOMEONE_ELSES = re.compile(
    r"(?i)\s+(?:\w+\s+){0,2}?(?:counsel|attorneys?|lawyers?|law\s+firm|paralegal|"
    r"employer|boss|supervisor|co-?workers?|office|father|mother|parents?|brother|sister|"
    r"sons?|daughters?|aunt|uncle|cousin|grand\w+|\w+-in-law|step\w+|friend|partner|"
    r"boyfriend|girlfriend|fianc[ée]e?|spouse|husband|wife|landlord|neighbou?r|roommate|"
    r"therapist|doctor|physician|counselor|caseworker)\b")
COUNSEL_LINE = re.compile(r"(?i)\b(?:represent(?:ed|s|ing)?\s+by|appear(?:ed|s|ing)?\s+"
                          r"(?:through|with|by)|counsel|attorneys?|lawyers?|law\s+(?:firm|office)|"
                          r"esq\b|bar\s+(?:no|number|#)|paralegal|guardian\s+ad\s+litem|"
                          r"mediator|commissioner|judge)\b")
# a numbered paragraph starts a new statement: the block under a name ends there
NUMBERED = re.compile(r"^\s*(?:\(?\d{1,3}[.)]|\(?[a-z][.)]|[IVX]{1,5}\.)\s+\S")


def _one(found: set[str]) -> str | None:
    """A full name says whose it is; two parties, or the family surname alone,
    say only that it is the family's. A non-party's name says it is nobody's."""
    specific = {o for o in found if o != BOTH}
    if specific and all(o.startswith("~") for o in specific):
        return "~nonparty"
    specific = {o for o in specific if not o.startswith("~")}
    if len(specific) == 1:
        return next(iter(specific))
    return BOTH


def _row_shape(line: str) -> str:
    """How a table row starts - "Line 9 -", "Member 9:" - so rows of one table
    are seen as siblings, not as an address block under the first."""
    m = re.match(r"\s*([A-Za-z]{2,12})\s*#?\s*\d{1,3}\b\s*[-:.)]", line)
    return m.group(1).casefold() if m else ""


def _clip(line: str, value: str, width: int = 110) -> str:
    """A line squeezed to ``width`` around ``value``, the value in [[ ]]."""
    line = " ".join(line.split())
    value = " ".join(value.split())
    at = line.find(value) if value else -1
    if at >= 0:
        line = f"{line[:at]}[[{value}]]{line[at + len(value):]}"
    if len(line) <= width:
        return line
    start = max(0, min(at - width // 3, len(line) - width)) if at >= 0 else 0
    return ("..." if start else "") + line[start:start + width] + "..."


class Context:
    """One page's lines and who is named on them - the app's anchoring rule."""

    def __init__(self, page: Page, index: NameIndex):
        self.page = page
        self.lines = page.text.split("\n")
        self.starts = [0] + [m.end() for m in re.finditer("\n", page.text)]
        self.mentions = index.mentions_by_line(self.lines)
        self.owners = {i: {label for _, _, label in found} for i, found in self.mentions.items()}
        self._header_end: int | None = None
        self.why, self.tie_line = "", None
        self.statement_like = bool(STATEMENT_CUE.search(page.text))

    def line_of(self, at: int) -> int:
        lo, hi = 0, len(self.starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.starts[mid] <= at:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _nearest(self, line_index: int, col: int) -> str:
        """Two parties on one line: "Petitioner is employed by X. Respondent works
        at Y." Each value is the party named last before it; "the parties" or
        "jointly" there, or nobody before it, leaves it the family's."""
        before = [m for m in self.mentions[line_index] if m[1] <= col]
        if not before:
            return BOTH
        end = max(m[1] for m in before)
        last = {m[2] for m in before if m[1] == end}
        if any(m[2] == BOTH and m[0] < end <= m[1] for m in before):
            return BOTH
        return _one(last)

    def header_end(self) -> int:
        if self._header_end is None:
            self._header_end = HEADER_LINES
            for i, line in enumerate(self.lines[:HEADER_LINES]):
                if _ACTIVITY_ROW.match(line) or _ACTIVITY_HEADING.match(line):
                    self._header_end = i
                    break
        return self._header_end

    def anchor(self, line_index: int, kind: str, col: int | None = None) -> str | None:
        """The owner the value on ``line_index`` is tied to, or None. ``self.why``
        says which rule tied it, for the model reviewing the proposal."""
        self.tie_line = None
        if line_index in self.owners:
            owner = _one(self.owners[line_index])
            self.why = "same line"
            if COUNSEL_LINE.search(self.lines[line_index]) or any(
                    o.startswith("~") for o in self.owners[line_index]):
                # "Respondent is represented by Jane Doe, (801) 555-0199": the party
                # is named, and so is someone else - never a tie "_rest" may accept
                self.why = "same line (counsel or a non-party named too)"
            self.tie_line = line_index
            if owner == BOTH and col is not None:
                return self._nearest(line_index, col)
            return owner
        if not _ACTIVITY_ROW.match(self.lines[line_index]):
            for step in range(1, SIGNATURE_LINES + 1):
                above = line_index - step
                if above < 0 or not self.lines[above].strip() \
                        or _ACTIVITY_ROW.match(self.lines[above]) \
                        or NUMBERED.match(self.lines[above + 1]):
                    break
                if _row_shape(self.lines[above]) and \
                        _row_shape(self.lines[above]) == _row_shape(self.lines[line_index]):
                    break               # "Line 4 - Laura" under "Line 2 - Diane": sibling rows
                if above in self.owners:
                    owner = _one(self.owners[above])
                    if step > BLOCK_LINES and owner != "~nonparty":
                        break           # past an address block: only counsel's runs this long
                    self.why = f"block under a name ({step} up)"
                    self.tie_line = above
                    return owner
        # a statement's header block - never an email's, whose header is its sender's
        header = (self.page.first and self.page.method not in ("email", "msg")
                  and line_index < self.header_end() and self.statement_like)
        labelled = kind in ("account", "card") and (
            _HEADER_LABEL.search(self.lines[line_index])
            or (line_index and _HEADER_LABEL.search(self.lines[line_index - 1])))
        if kind == "address" and _LOCKBOX_OR_BANK.search(self.lines[line_index]):
            header = False
        if header or labelled:
            found = set().union(*(self.owners.get(i, set()) for i in range(self.header_end()))) \
                if self.page.first else set()
            self.why = "first-page header"
            self.tie_line = min((i for i in range(self.header_end()) if self.owners.get(i)),
                                default=None) if self.page.first else None
            return _one(found) if found else None
        self.why = "nobody named near"
        return None

    def snippet(self, line_index: int, value: str) -> str:
        """The value's line, marked - and the line that tied it, when that was
        another line (a name above it, a statement's header)."""
        out = []
        if self.tie_line is not None and self.tie_line != line_index:
            out.append(_clip(self.lines[self.tie_line], "", 80))
            if self.tie_line < line_index - 1:
                out.append("...")
        elif self.tie_line is None and line_index and self.lines[line_index - 1].strip():
            out.append(_clip(self.lines[line_index - 1], "", 80))  # nobody tied it: show more
        out.append(_clip(self.lines[line_index], value))
        if self.tie_line is None and line_index + 1 < len(self.lines) \
                and self.lines[line_index + 1].strip():
            out.append(_clip(self.lines[line_index + 1], "", 80))
        return "\n".join(out)


# --------------------------------------------------------------------------
# values: keys for folding, forms for the CSV
# --------------------------------------------------------------------------

_SUFFIX_ABBR = {"street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "lane": "ln",
                "court": "ct", "circle": "cir", "boulevard": "blvd", "place": "pl",
                "parkway": "pkwy", "highway": "hwy", "terrace": "ter", "trail": "trl",
                "way": "way", "apartment": "apt", "suite": "ste", "unit": "unit"}
_DIR_ABBR = {"north": "n", "south": "s", "east": "e", "west": "w", "northeast": "ne",
             "northwest": "nw", "southeast": "se", "southwest": "sw"}


def digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def value_key(kind: str, value: str) -> str:
    v = " ".join(value.split())
    if kind in ("ssn", "tin"):
        return digits(v)
    if kind == "phone":
        return digits(v)[-10:]
    if kind in ("account", "card"):
        if re.search(r"[Xx*•#]{2}|\.\.", v):
            return "tail:" + digits(v)[-4:]
        return re.sub(r"[^0-9A-Za-z]", "", v).upper()
    if kind == "dob":
        parsed = parse_date(v)
        return parsed.isoformat() if parsed else v.casefold()
    if kind == "address":
        words = re.sub(r"[^\w\s]", " ", v.casefold()).split()
        return " ".join(_DIR_ABBR.get(w, _SUFFIX_ABBR.get(w, w)) for w in words)
    if kind == "handle":
        return v.casefold().lstrip("@$")
    if kind in ("vehicle", "dl", "passport", "case_number", "parcel", "other"):
        return re.sub(r"[^0-9A-Za-z]", "", v).upper()
    return v.casefold()


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                       "sep", "oct", "nov", "dec"), 1)}


def parse_date(text: str) -> date | None:
    t = text.strip().rstrip(".,")
    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})", t)
    if m:
        mo, d, y = int(m[1]), int(m[2]), int(m[3])
        y += 1900 if y < 100 and y > 30 else (2000 if y < 100 else 0)
        return _date(y, mo, d)
    m = re.fullmatch(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", t)
    if m:
        return _date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", t)
    if m and m[1][:3].casefold() in _MONTHS:
        return _date(int(m[3]), _MONTHS[m[1][:3].casefold()], int(m[2]))
    m = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})", t)
    if m and m[2][:3].casefold() in _MONTHS:
        return _date(int(m[3]), _MONTHS[m[2][:3].casefold()], int(m[1]))
    return None


def _date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def display(kind: str, value: str) -> str:
    """The value as the CSV shows it.

    Spreadsheets turn a run of digits into a number - an account's leading
    zeros vanish and a card number becomes 4.12E+15 - so digit-only values are
    written grouped, which the app accepts in every field.
    """
    v = " ".join(value.split())
    d = digits(v)
    if kind in ("ssn",) and len(d) == 9 and d == re.sub(r"[\s\-]", "", v):
        v = f"{d[:3]}-{d[3:5]}-{d[5:]}"
    elif kind == "tin" and len(d) == 9 and d == re.sub(r"[\s\-]", "", v):
        v = f"{d[:2]}-{d[2:]}"
    elif kind == "phone" and len(d) in (10, 11):
        d10 = d[-10:]
        v = f"({d10[:3]}) {d10[3:6]}-{d10[6:]}"
    elif kind in ("account", "card", "dl", "passport", "case_number", "parcel") \
            and v.isdigit() and (len(v) >= 6 or v.startswith("0")):
        # the app matches these with or without spaces (parties.account_forms,
        # id_forms); a spreadsheet would drop leading zeros or round long ones
        v = " ".join(v[i:i + 4] for i in range(0, len(v), 4))
    elif kind == "dob":
        parsed = parse_date(v)
        if parsed:
            v = f"{parsed.month:02d}/{parsed.day:02d}/{parsed.year}"
    if v[:1] in ("=", "+", "-") or (v[:1] == "@" and kind != "handle"):
        v = "'" + v                       # never a spreadsheet formula
    return v


@dataclass
class Value:
    owner: str
    kind: str
    value: str
    detail: str = ""
    file: str = ""
    page: str = ""
    count: int = 0
    context: dict | None = None        # one place it was found, for a conflict's review


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------


DETAIL = {"masked_account": "masked - last digits only", "investment_account":
          "investment account", "license_plate": "plate", "vin": "VIN",
          "vehicle_title": "title number", "vessel_number": "hull number", "fax": "fax",
          "payment_handle": "payment app", "crypto_wallet": "crypto wallet", "iban": "IBAN"}

# "works at Wasatch Orthopedic Group", "employed by Acme, Inc." - prose the
# app's statement and pay-stub readers are not built for
_ORG_WORD = r"(?:[A-Z][A-Za-z0-9&'’.\-]*|of|the|and|&|for|de)"
EMPLOYED_AT = re.compile(
    rf"(?i:\b(?:employed\s+(?:by|at|with)|works?\s+(?:at|for)|working\s+(?:at|for)|"
    rf"employer(?:\s+is|\s*:)|job\s+(?:at|with))\s+(?:the\s+)?)"
    rf"((?:[A-Z][A-Za-z0-9&'’.\-]*)(?:[ \t]+{_ORG_WORD}){{0,6}})")


_ABBREVIATIONS = {"inc", "co", "corp", "ltd", "llc", "bros", "jr", "sr", "st", "mt", "no", "assn"}


def first_sentence(text: str) -> str:
    """Up to the first full stop that ends a sentence: "Arts. Respondent" ends
    one; "Acme Co. Inc." and "J. P. Morgan" do not."""
    for m in re.finditer(r"\.\s+(?=[A-Z])", text):
        word = re.search(r"([A-Za-z]+)$", text[:m.start()])
        if word and (len(word.group(1)) == 1 or word.group(1).casefold() in _ABBREVIATIONS):
            continue
        return text[:m.start()]
    return text



def _wordlike(name: str) -> bool:
    """A name, not an OCR smear: mostly letters, few stray symbols."""
    letters = sum(ch.isalpha() for ch in name)
    return letters >= 0.7 * len(name.replace(" ", "")) and sum(ch in "|)(}{[]" for ch in name) <= 1


def employers_on(text: str) -> list[str]:
    found = [e.name for e in transactions.employers(text)] + transactions.pay_stub_employers(text)
    for m in EMPLOYER_BOX.finditer(text):
        # the box's first cell: up to a run of spaces or the first number
        cell = re.split(r"\s{2,}|\s(?=\d)", m.group(1).strip())[0].strip(" ,.")
        if len(cell) >= 3 and re.search(r"[A-Za-z]{2}", cell):
            found.append(cell)
    for m in EMPLOYED_AT.finditer(text):
        name = first_sentence(m.group(1))
        name = re.sub(r"(?:\s+(?:of|the|and|&|for|de))+$", "", name).strip(" .,;")
        if len(name) >= 3 and not names.ordinary_word(name):
            found.append(name)
    return list(dict.fromkeys(found))


QUANTITY = re.compile(r"\d+\.\d+")
SSN_SHAPED = re.compile(r"(?<![\d\-/.])\d{3}-\d{2}-\d{4}(?![\d\-/.])")
# a form's box label over its value: "EMPLOYER'S name, address..." then the employer
EMPLOYER_BOX = re.compile(r"(?im)employer['’]?s\s+name[^\n]*\n\s*([^\n]+)")
# "Student Checking Account 0004991827", "Member Share Savings": a deposit account,
# whatever else the line says - never dropped as a loan's or a policy's
OWN_ACCOUNT_LINE = re.compile(r"(?i)\b(?:checking|savings|share|money\s+market|deposit|"
                              r"brokerage|ira|401\(?k\)?|hsa|account\s+(?:number|no\.?|#))\b")
# what makes a first page a statement or a form, whose header block names its holder -
# a letter's "Re: Ashdown v. Ashdown" block does not
STATEMENT_CUE = re.compile(
    r"(?i)\b(?:statement\s+(?:period|date|of\s+account|closing)|closing\s+date|account\s+"
    r"(?:summary|number|no\.?|#|holder)|beginning\s+balance|ending\s+balance|pay\s+(?:period|"
    r"date)|earnings\s+statement|form\s+w-?2|1099|1040|explanation\s+of\s+benefits|billing\s+"
    r"(?:date|period)|amount\s+due|policy\s+(?:number|period)|member\s+(?:id|number)|"
    r"credit\s+report|loan\s+(?:number|statement)|escrow|payment\s+due)\b")


def is_tail(value: str) -> bool:
    """A masked account or a bare last four - the app's ``parties._tail``."""
    d = digits(value)
    return len(d) >= 4 and (bool(re.search(r"[Xx*•#]{2,}|\.{2,}", value)) or len(d) <= 6)


def detect(pages: list[Page], labels: list[tuple[str, dict]], non_parties: list[str]):
    """Every candidate party value in the case, each with the owner the rules
    propose (or none), why, and where it was seen - for the model to decide.

    What the app itself always leaves as written is not proposed at all and is
    only counted: loan, policy and routing numbers, a merchant's phone on a
    statement row, a bank's address, anything beside counsel's or a judge's name.
    """
    index = NameIndex(labels, non_parties)
    found: dict[tuple[str, str], dict] = {}      # (kind, key) -> candidate
    left = Counter()
    dropped: dict[tuple[str, str, str], dict] = {}   # what a rule kept from the review

    def leave(reason: str, kind: str, value: str, page: Page) -> None:
        left[reason] += 1
        key = (reason, kind, value_key(kind, value))
        if key not in dropped:
            dropped[key] = {"reason": reason, "kind": kind, "value": " ".join(value.split()),
                            "where": page.file + (f" p.{page.page}" if page.page else "")}

    def note(kind: str, value: str, owner: str | None, why: str, detail: str, page: Page,
             ctx: Context | None, li: int | None, transfer: bool = False):
        value = " ".join(value.split())          # "P.O. Box 221\nKaysville": one line
        item = found.setdefault((kind, value_key(kind, value)), {
            "kind": kind, "value": value, "detail": detail, "file": page.file,
            "page": page.page, "count": 0, "owners": {}, "contexts": [], "transfer": False})
        item["count"] += 1
        if detail and not item["detail"]:
            item["detail"] = detail
        item["transfer"] = item["transfer"] or transfer
        slot = owner or ("~" if why == "beside a non-party's name" else "")
        seen = item["owners"].setdefault(slot, {"why": why, "count": 0})
        seen["count"] += 1
        # one place per proposed owner, and a second for a value nobody is tied to
        same = [c for c in item["contexts"] if c["owner"] == slot]
        if ctx is not None and li is not None and len(same) < (1 if owner else 2) \
                and len(item["contexts"]) < 4:
            # the first place gets the lines around it; a second, its own line
            text = ctx.snippet(li, value) if not item["contexts"] else _clip(ctx.lines[li], value)
            if text.strip() == f"[[{' '.join(value.split())}]]":
                text = "(alone on its line)"
            item["contexts"].append({"owner": slot,
                                     "where": page.file + (f" p.{page.page}" if page.page else ""),
                                     "text": text})

    for page in pages:
        if not page.text.strip():
            continue
        ctx = Context(page, index)
        protected = patterns.allowlist_spans(page.text)
        for m in patterns.scan(page.text, protected=protected):
            kind = KIND_OF.get(m.category)
            if m.category in LEAVE:
                leave("left as written", m.category.replace("_", " "), m.text, page)
                continue
            if kind is None:
                continue
            if m.bare:
                # no label, no mask: a bare run of digits that passes a checksum -
                # a card number, or chance. Offered for the model to judge.
                li = ctx.line_of(m.start)
                note(kind, m.text, None, "bare number (passes a checksum, no label)",
                     DETAIL.get(m.category, ""), page, ctx, li)
                continue
            value = m.text
            li = ctx.line_of(m.start)
            line = ctx.lines[li]
            if kind == "case_number" and QUANTITY.fullmatch(value):
                leave("quantity", kind, value, page)   # "MID CAP INDEX 344.9182": shares
                continue
            if kind in CASE_LEVEL:
                note(kind, value, CASE, "case number",
                     m.category.replace("_", " ") if kind == "parcel" else "", page, ctx, li)
                continue
            if kind in ("account", "card") and _LEAVE_LINE.search(line) and li not in ctx.owners \
                    and not OWN_ACCOUNT_LINE.search(line):
                leave("loan or policy", kind, value, page)   # left as written by design
                continue
            detail = m.category.replace("_", " ") if kind == "other" else DETAIL.get(m.category, "")
            owner = ctx.anchor(li, kind, m.start - ctx.starts[li])
            if owner == "~nonparty":
                # beside counsel's or a judge's name - usually theirs, but a Cc line
                # or an intake sheet puts the client's own details there too
                note(kind, value, None, "beside a non-party's name", detail, page, ctx, li)
                continue
            transfer = kind == "account" and bool(_TRANSFER_LINE.search(line))
            if transfer and owner is None and is_tail(value):
                # a masked tail on a money line nobody is named on: the app replaces
                # it by itself (parties.file_match) - nothing to type. A tail tied to
                # a party ("Kyle D Harrington Account ...7190") is still proposed.
                leave("transfer tail", kind, value, page)
                continue
            note(kind, value, owner, ctx.why, detail, page, ctx, li, transfer)

        # an SSN-shaped number the validity rules reject (a 9xx area, a typo, a
        # "never issued" sample) still names someone: offered, flagged
        taken = {(m.start, m.end) for m in patterns.scan(page.text)}
        for m in SSN_SHAPED.finditer(page.text):
            if any(a <= m.start() < b for a, b in taken) or patterns.valid_ssn(m.group(0)):
                continue
            li = ctx.line_of(m.start())
            owner = ctx.anchor(li, "ssn", m.start() - ctx.starts[li])
            if owner == "~nonparty":
                note("ssn", m.group(0), None, "beside a non-party's name",
                     "SSN-shaped, fails the SSA validity rules", page, ctx, li)
                continue
            note("ssn", m.group(0), owner, ctx.why, "SSN-shaped, fails the SSA validity rules",
                 page, ctx, li)

        # employers and the family's businesses, tied by their line or by the page
        for kind, names_found in (("employer", employers_on(page.text)),
                                  ("business", [b.name for b in transactions.businesses(page.text)
                                                if _wordlike(b.name)])):
            for name in names_found:
                at = page.text.find(name)
                li = ctx.line_of(at) if at >= 0 else None
                owner = ctx.anchor(li, kind, at - ctx.starts[li]) if li is not None else None
                why = ctx.why if li is not None else ""
                if owner is None:
                    # a pay stub or W-2 names one employee: the one party named on the page
                    on_page = {_one(o) for o in ctx.owners.values()} - {"~nonparty", BOTH}
                    if len(on_page) == 1:
                        owner, why = next(iter(on_page)), "only party on the page"
                if owner == "~nonparty":
                    owner, why = None, "beside a non-party's name"
                note(kind, name, owner, why or "nobody named near", "", page, ctx, li)

    # a masked tail of a full number tied to one party is that number
    tails: dict[str, set[str]] = defaultdict(set)
    for (kind, key), item in found.items():
        if kind in ("account", "card") and not key.startswith("tail:") and len(digits(key)) >= 4:
            tails[digits(key)[-4:]].update(o for o in item["owners"] if o)
    candidates = []
    for (kind, key), item in found.items():
        owners = {o: v for o, v in item["owners"].items() if o and o != "~"}
        specific = sorted(set(owners) - {BOTH, CASE}, key=role_rank)
        if key.startswith("tail:"):
            full = tails.get(key[5:], set())
            if full and (not owners or set(owners) <= full):
                left["tail of full"] += 1          # already on the sheet as the full number
                dropped[("tail of full", kind, key)] = {"reason": "tail of full", "kind": kind,
                                                        "value": item["value"], "where": item["file"]}
                continue

        if len(specific) > 1:
            proposed, why = None, "conflict: " + " vs ".join(specific)
        elif specific:
            proposed, why = specific[0], owners[specific[0]]["why"]
        elif BOTH in owners:
            proposed, why = BOTH, owners[BOTH]["why"] + ", both or family surname"
        elif CASE in owners:
            proposed, why = CASE, owners[CASE]["why"]
        elif item["transfer"]:
            proposed, why = BOTH, "transfer line"
        elif "~" in item["owners"]:
            proposed, why = None, "beside a non-party's name"
        else:
            proposed, why = None, "nobody named near"
        # the place that justifies the proposal comes first - the reviewer reads it
        # first, and a group shows only it: a store's receipt printing the same
        # number must never stand in for the bill that names the party
        shown = sorted(item["contexts"], key=lambda c: (c["owner"] != (proposed or ""),
                                                        c["owner"] in ("", "~")))
        candidates.append({"kind": kind, "value": item["value"], "detail": item["detail"],
                           "file": item["file"], "page": item["page"], "count": item["count"],
                           "proposed": proposed, "why": why,
                           "mixed": len({c["owner"] for c in item["contexts"]}) > 1,
                           "contexts": [{k: c[k] for k in ("where", "text")} for c in shown]})
    return candidates, left, sorted(dropped.values(), key=lambda d: (d["reason"], d["kind"],
                                                                     d["value"]))


# one written name: "Rowena K. Ashdown", "EMMA R HARRINGTON" (one case style, single
# spaces, never two columns run together) or surname first, "ASHDOWN, MARCUS D."
_TC = r"[A-Z][a-z'’\-]+(?:[A-Z][a-z'’\-]+)?"
_UC = r"[A-Z][A-Z'’\-]+"
ONE_PERSON = re.compile(rf"(?<![\w'’])(?:{_TC}(?: [A-Z]\.?| {_TC}){{0,2}} {_TC}"
                        rf"|{_UC}(?: [A-Z]\.?| {_UC}){{0,2}} {_UC})(?![\w'’])")
NAME_FORM = re.compile(rf"{ONE_PERSON.pattern}"
                       rf"|(?<![\w'’])(?:{_TC}, {_TC}(?: [A-Z]\.?| {_TC})?"
                       rf"|{_UC}, {_UC}(?: [A-Z]\.?| {_UC})?)(?![\w'’])")
QUOTED_NICK = re.compile(r"([A-Z][a-z]+)\s+[\"“'‘]([A-Z][a-z]+)[\"”'’]\s+([A-Z][A-Za-z'’\-]+)")


def name_variants(pages: list[Page], labels: list[tuple[str, dict]]) -> list[Value]:
    """Every written form of each party's name found in the files.

    The app derives "J. Smith", "Smith, John" and "Mr. Smith" from one entry
    by itself; those rows are marked so they need not be typed. What it cannot
    derive is listed plainly: a misspelling or OCR misreading, a nickname, an
    extra middle name, a suffix, a former or maiden name.
    """
    people = []
    for label, entry in labels:
        forms = [n for n in entry.get("names", []) if n.strip()]
        if not forms:
            continue
        parsed = [names.parse(f) for f in forms]
        derived = {v.text.casefold() for p in parsed for v in names.variants(p)}
        lasts = {p.last.casefold() for p in parsed if p.last}
        people.append((label, forms, parsed, derived, lasts))
    found: dict[tuple[str, str], Value] = {}
    spelled: dict[tuple[str, str], Counter] = defaultdict(Counter)

    def record(label: str, text: str, page: Page, detail: str, at: int):
        text = " ".join(text.split()).strip(" ,")
        key = (label, text.casefold())
        spelled[key][text] += 1
        hit = found.get(key)
        if hit is None:
            lo = page.text.rfind("\n", 0, at) + 1
            hi = page.text.find("\n", at)
            line = " ".join(page.text[lo:hi if hi >= 0 else None].split())[:160]
            found[key] = Value(label, "name", text, detail, page.file, page.page, 1,
                               {"where": page.file + (f" p.{page.page}" if page.page else ""),
                                "text": line})
        else:
            hit.count += 1

    for page in pages:
        for m in NAME_FORM.finditer(page.text):
            run = m.group(0)
            cand = names.parse(run)
            if not cand.last or not cand.first:
                continue
            matches = []
            for label, forms, parsed, derived, lasts in people:
                if not any(names.alike(cand.last, last) for last in lasts):
                    continue
                if any(_given_fits(cand, p) for p in parsed):
                    matches.append((label, forms, parsed, derived, lasts))
            if len(matches) != 1:
                continue
            label, forms, parsed, derived, lasts = matches[0]
            text = " ".join(run.split()).strip(" ,")
            if text.casefold() in {f.casefold() for f in forms}:
                detail = "as entered"
            elif text.casefold() in derived:
                detail = "derived by the app - no need to type"
            elif cand.last.casefold() not in lasts:
                detail = "spelled differently - misspelling or OCR misread"
            elif not any(names.alike(cand.first, p.first) for p in parsed if p.first) \
                    and len(cand.first.strip(".")) > 1:
                detail = "nickname"
            else:
                detail = "other written form"
            record(label, text, page, detail, m.start())
        for m in QUOTED_NICK.finditer(page.text):
            for label, forms, parsed, derived, lasts in people:
                if m[3].casefold() in lasts and any(
                        names.alike(m[1], p.first) for p in parsed if p.first):
                    record(label, f"{m[2]} {m[3]}", page, "nickname (in quotes)", m.start())
                    record(label, m[2], page, "nickname (in quotes)", m.start())
        # a nickname written alone: "Kit said..." - only one the app's list knows
        for label, forms, parsed, derived, lasts in people:
            for p in parsed:
                for nick in names.NICKNAMES.get(p.first.casefold(), ()):
                    if len(nick) < 3 or names.ordinary_word(nick):
                        continue
                    for hit in re.finditer(rf"(?<![\w'’]){re.escape(nick.title())}(?![\w'’])",
                                           page.text):
                        record(label, hit.group(0), page, "nickname (alone)", hit.start())
    # the most common spelling of each form is the one shown
    for key, value in found.items():
        value.value = spelled[key].most_common(1)[0][0]
    out = list(found.values())
    for label, forms, *_ in people:
        for i, form in enumerate(forms):
            key = (label, form.casefold())
            if key in found:
                found[key].detail = "full name" if i == 0 else "former or other name"
                found[key].value = form          # as entered, not as a caption shouts it
            else:
                out.append(Value(label, "name", form, "full name" if i == 0
                                 else "former or other name", "", "", 0))
    return out


# "restored to her former name of", "maiden name, ", "shall be known as", "f/k/a",
# "née": what follows is another name of the person the clause is about
# strong: the words say outright that a name follows - a bare surname counts
NAME_CLAUSE = re.compile(
    r"(?i)\b(?:(?:former|maiden|birth|prior|previous|married|legal)\s+(?:sur)?name"
    r"(?:\s+(?:of|is|was|to))?|(?:shall|will|to)\s+be\s+known\s+as|(?:now|formerly|also|"
    r"previously)\s+known\s+as|[nfa]/k/a|a\.?k\.?a\.?|n[ée]e)\b[\s,:]*"
    r"(?:(?:her|his|their|the)\s+)?(?:(?:former|maiden|birth|prior)\s+)?"
    r"(?:(?:sur)?name\s+(?:of\s+)?)?")
# weak: "restored to", "returned to", "changed to" - "returned to Utah", "changed to
# Zions" - so only a full two-word name after them is taken, and it is reviewed
WEAK_CLAUSE = re.compile(
    r"(?i)\b(?:(?:restored|returned?|revert(?:s|ed)?|changed?)\s+to|resume[sd]?)\b[\s,:]*"
    r"(?:(?:her|his|their|the)\s+)?(?:(?:former|maiden|birth|prior)\s+)?"
    r"(?:(?:sur)?name\s+(?:of\s+)?)?")
SURNAME_ONLY = re.compile(r"(?:[A-Z][a-z'’\-]+(?:[A-Z][a-z'’\-]+)?|[A-Z][A-Z'’\-]+)(?![\w'’])")


def former_name_clauses(pages: list[Page], labels: list[tuple[str, dict]]) -> list[Value]:
    """Names a clause says are another name of someone - mapped to the party the
    sentence is about (the name or role word nearest before the clause, else the
    party sharing the new name's given name), or left for the model to place."""
    index = NameIndex(labels, [])
    known = {n.casefold() for _, e in labels for n in e.get("names", [])}
    firsts = {}
    for label, entry in labels:
        for n in entry.get("names", []):
            first = names.parse(n).first.casefold()
            if first:
                firsts.setdefault(first, set()).add(label)
    out: dict[tuple[str, str], Value] = {}
    for page in pages:
        lines = page.text.split("\n")
        mentions = index.mentions_by_line(lines)
        for li, line in enumerate(lines):
            clauses = [(m, True) for m in NAME_CLAUSE.finditer(line)] + \
                      [(m, False) for m in WEAK_CLAUSE.finditer(line)]
            for m, strong_clause in clauses:
                rest = line[m.end():]
                hit = NAME_FORM.match(rest) or (SURNAME_ONLY.match(rest) if strong_clause
                                                else None)
                if not hit:
                    continue
                name = " ".join(hit.group(0).split()).strip(" ,.")
                if name.casefold() in known or len(name) < 3 or places.known(name) \
                        and not strong_clause:
                    continue
                # "maiden name is Young": an everyday word is still a surname after
                # a strong clause; elsewhere it is not taken
                before = [x for x in mentions.get(li, []) if x[1] <= m.start()]
                owner = _one({before[-1][2]}) if before else None
                cand = names.parse(name)
                if owner in (None, BOTH, "~nonparty") and cand.first:
                    labels_for = firsts.get(cand.first.casefold(), set())
                    owner = next(iter(labels_for)) if len(labels_for) == 1 else None
                if owner in (BOTH, "~nonparty"):
                    owner = None
                if " " not in name and owner:
                    # "née Pettigrew": the party's given name with the old surname
                    first = names.parse(dict(labels)[owner]["names"][0]).first
                    name = f"{first} {name}" if first else name
                key = (owner or "", name.casefold())
                if key in out:
                    out[key].count += 1
                    continue
                out[key] = Value(owner or "", "name", name,
                                 "former name (clause)" if strong_clause
                                 else "possible former name (clause)", page.file,
                                 page.page, 1, {"where": page.file + (f" p.{page.page}"
                                                                    if page.page else ""),
                                                "text": _clip(line, hit.group(0))})
    return list(out.values())


def with_variants(labels: list[tuple[str, dict]], variants: list[Value]):
    """The parties with every form found that the app cannot derive - a
    nickname, a misreading - added, so a value beside "Mac" is Marcus's."""
    extra: dict[str, list[str]] = defaultdict(list)
    for v in variants:
        # a quoted nickname ('Marcus "Mac" Ashdown') or a former-name clause says
        # outright who it is; a near-spelling could be a sibling or a witness,
        # so it is reviewed before anything is tied to it
        if v.detail.startswith("nickname (in quotes)") or (
                v.detail == "former name (clause)" and " " in v.value.strip()):
            extra[v.owner].append(v.value)
    # before review, an added form anchors by its full written form only: a single
    # word from a clause ("Utah", "Zions") must never tie lines to a party
    return [(label, {**entry, "names": list(entry.get("names", [])) + extra.get(label, [])})
            for label, entry in labels]


def _given_fits(cand: names.PersonName, party: names.PersonName) -> bool:
    if not party.first:
        return False
    a, b = cand.first.strip("."), party.first.strip(".")
    if len(a) == 1 or len(b) == 1:
        return a[:1].casefold() == b[:1].casefold()
    return names.same_given(a, b)


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

CSV_HEADER = ["Role", "Field", "Value", "Detail", "File", "Page", "Times found"]


def role_rank(label: str) -> tuple:
    base = {"Petitioner": 0, "Respondent": 1, BOTH: 4, CASE: 5, "Unassigned": 6}
    if label.startswith("Child "):
        return (2, int(label.split()[1]))
    if label.startswith("Other party "):
        return (3, int(label.split()[2]))
    return (base.get(label, 7), 0)


def sheet_refusal(kind: str, value: str) -> str:
    """Why the app's party sheet would refuse this value in its own field - the
    app's parties._normalise rules - or "" if it takes it."""
    d = digits(value)
    if kind in ("ssn", "tin") and len(d) != 9:
        return f"not 9 digits (an {kind.upper()} field takes only a whole number)"
    if kind == "dob" and forms.parse_date(" ".join(value.split())) is None:
        return "not a whole date"
    if kind == "phone":
        dd = digits(value.split("x")[0].split("ext")[0])
        if not (len(dd) == 10 or (len(dd) == 11 and dd.startswith("1"))):
            return "not a 10-digit phone number"
    if kind == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()):
        return "not a whole email address"
    if kind == "address":
        parsed = forms.parse_address(" ".join(value.split()))
        if parsed is None or not (parsed.number or parsed.box):
            return "a place without a house number or PO box"
    if kind in ("account", "card") and len(d) < 4:
        return "fewer than 4 digits"
    if kind == "dl" and len(re.sub(r"[^A-Za-z0-9]", "", value)) < 4:
        return "shorter than a licence number"
    if kind == "handle" and len(value.strip().lstrip("@$")) < 3:
        return "shorter than a handle"
    return ""


def refile(v: Value) -> Value:
    """A value the sheet's own field would refuse goes under Other identifiers,
    which takes anything and still replaces it wherever it is printed."""
    why = sheet_refusal(v.kind, v.value)
    if why:
        v.detail = "; ".join(x for x in (v.detail, f"{why} - the sheet takes it under Other "
                                                   f"identifiers") if x)
        v.kind = "other"
    return v


def write_csv(path: Path, rows: list[Value]) -> None:
    detail_rank = {"full name": 0, "former or other name": 1}
    rows = sorted(rows, key=lambda v: (
        role_rank(v.owner), FIELD_ORDER.index(v.kind),
        detail_rank.get(v.detail, 3 if v.detail.startswith("derived") else 2),
        -v.count, v.value.casefold()))
    tmp = path.with_suffix(".csv.tmp")
    # utf-8-sig: Excel reads accented names correctly only with the byte-order mark
    with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        for v in rows:
            w.writerow([v.owner, FIELD_LABEL[v.kind], display(v.kind, v.value), v.detail,
                        v.file, v.page, v.count or ""])
    tmp.replace(path)


# --------------------------------------------------------------------------
# the sweep: a model reads every page for what the rules missed
# --------------------------------------------------------------------------

SWEEP_BATCH_CHARS = 60_000        # about 15,000 tokens of page text per batch
SWEEP_REPEAT = 3                  # a line on this many pages is shown once
SWEEP_FORMAT = 4                  # bump when the batches are written differently
SWEEP_BATCH_LINES = 1200          # a helper's file reader shows about 2,000 lines at once
# a column header names what the rows under it are: shown on every page
COLUMN_HEADER = re.compile(r"(?i)^(?=[^\d]*$).*\b(?:name|birth|dob|ssn|social|account|phone|"
                           r"address|relationship|dependent|member|patient|student|employee)\b")
FIELD_WORDS = {label.casefold(): kind for kind, label in FIELD_LABEL.items()}
FIELD_WORDS.update({kind: kind for kind in FIELD_LABEL}, name="name", names="name",
                   phone="phone", email="email", address="address", account="account",
                   card="card", handle="handle", vehicle="vehicle", employer="employer",
                   business="business", dob="dob", birthdate="dob", tin="tin", ein="tin",
                   licence="dl", license="dl", passport="passport", parcel="parcel",
                   other="other", **{"date of birth": "dob", "driver's license": "dl",
                                     "case number": "case_number", "case numbers": "case_number"})




def _kind_by_shape(value: str) -> str:
    """A finding whose field word is not one of the sheet's: filed by what it looks like."""
    v = value.strip()
    d = re.sub(r"\D", "", v)
    if "@" in v and "." in v.split("@")[-1]:
        return "email"
    if re.fullmatch(r"\d{3}-\d{2}-\d{4}", v):
        return "ssn"
    if patterns.phone_shaped(v) or (len(d) in (10, 11) and re.fullmatch(r"[\d\s().+-]+", v)):
        return "phone"
    if re.search(r"\d+\s+\w+.*\b(?:st|street|ave|avenue|rd|road|dr|drive|ln|lane|ct|court|"
                 r"blvd|way|pl|place|cir|circle|pkwy|hwy|apt|unit|suite)\b", v, re.I):
        return "address"
    if len(d) >= 6 and len(d) >= len(re.sub(r"\W", "", v)) - 2:
        return "account"
    return "other"






def sweep_stage(work: Path, pages: list[Page], labels: list[tuple[str, dict]],
                candidates: list[dict], values: list[Value],
                dropped: list[dict] = ()) -> tuple[str, list[dict]]:
    """Write the sweep's batches, or read its findings back: ("done", new items)
    once every batch has a findings file, else (what is still wanted, [])."""
    folder = work / "sweep"
    signature = hashlib.sha1(json.dumps(
        [SWEEP_FORMAT] + [[p.file, p.page, p.text] for p in pages]
        + [[l, e.get("names", [])] for l, e in labels],
        ensure_ascii=False).encode()).hexdigest()
    manifest = load_json(folder / "manifest.json", {}) or {}
    if manifest.get("signature") != signature:
        manifest = write_sweep(folder, pages, labels, candidates, values, signature, dropped)
    waiting = [b for b in manifest["batches"] if not _swept(folder / f"{b}.found.txt")]
    if waiting:
        bad = [b for b in waiting if (folder / f"{b}.found.txt").exists()]
        return (f"{len(waiting)} of {len(manifest['batches'])} batch(es) to read in {folder} - "
                f"write <batch>.found.txt for each"
                + (f" ({', '.join(bad[:5])} hold no findings and no NONE: read them again)"
                   if bad else "")), []
    found = read_sweep(folder, manifest, pages, labels, candidates, values, dropped)
    rejected = [l for l in (folder / "rejected.txt").read_text("utf-8").splitlines() if l]
    print(f"Sweep: {len(found)} new value(s) for review"
          + (f"; {len(rejected)} finding(s) rejected as not printed where stated - see "
             f"{folder / 'rejected.txt'}" if rejected else ""))
    return "done", found


def _swept(path: Path) -> bool:
    """A batch is read when its findings file says NONE or holds at least one
    finding line - an empty file or an apology is not a reading."""
    if not path.exists():
        return False
    lines = [l.strip().strip("`") for l in path.read_text("utf-8", "replace").splitlines()]
    return any(l.upper() == "NONE" or l.count("|") >= 4 for l in lines)


def derived_names(labels: list[tuple[str, dict]]) -> set[str]:
    """Every written form the app derives from the parties' names by itself:
    "Marcus", "Ashdown", "J. Smith", "Smith, John" - nothing for the sweep to add."""
    out = set()
    for _, entry in labels:
        for n in entry.get("names", []):
            out |= {_norm(v.text) for v in names.variants(names.parse(n))}
            out.add(_norm(n))
    return out


def page_ids(pages: list[Page]) -> dict[str, Page]:
    return {f"P{i:04d}": p for i, p in enumerate(pages, 1)}


def write_sweep(folder: Path, pages: list[Page], labels: list[tuple[str, dict]],
                candidates: list[dict], values: list[Value], signature: str,
                dropped: list[dict] = ()) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("batch_*"):
        old.unlink()
    # what a rule already kept out (a masked tail on a money line, a loan number,
    # counsel's block) is "found" too: the sweep is for what nothing looked at
    known = {_norm(c["value"]) for c in candidates} | {_norm(v.value) for v in values}
    known |= {_norm(d["value"]) for d in dropped}
    known |= derived_names(labels)
    # an address's street line alone, as a statement prints it over its city
    known |= {_norm(c["value"]).split(",")[0] for c in candidates if c["kind"] == "address"}
    # a bare four digits is marked only beside its mask (the tails piece below):
    # "9876" alone would mark "9876 S Main" and a ZIP+4
    known = sorted((k for k in known if len(k) >= 3 and not re.fullmatch(r"[\d\s-]{1,6}", k)),
                   key=len, reverse=True)
    tails = sorted({digits(c["value"])[-4:] for c in candidates
                    if c["kind"] in ("account", "card") and len(digits(c["value"])) >= 4})
    pieces = [re.escape(k).replace(r"\ ", r"\s+") for k in known]
    if tails:
        # "XXXXXX9901", "...9901", "ending in 9901" of a number already found
        pieces.append(r"(?:[Xx*•#]{2,}[\s-]?|\.{2,4}|ending\s+in\s+)(?:" + "|".join(tails) + ")")
    # whole words only - "Mac" must not mark the start of "mac_builds_utah", or the
    # sweep is told a handle nobody found is already known
    mark = re.compile(r"(?<![\w@$])(?:" + "|".join(pieces) + r")(?![\w])",
                      re.IGNORECASE) if pieces else None
    repeats = Counter()
    for p in pages:
        repeats.update({_norm(line) for line in p.text.split("\n") if line.strip()})
    shown: set[str] = set()
    parties = "; ".join(f"{label} = {' / '.join(e.get('names', []))}" for label, e in labels)
    blocks: list[str] = []
    for pid, p in page_ids(pages).items():
        out, omitted = [], 0
        for n, line in enumerate(p.text.split("\n"), 1):
            text = _norm(line)
            if not text:
                continue
            if repeats[text] >= SWEEP_REPEAT and not COLUMN_HEADER.search(text):
                if text in shown:
                    omitted += 1
                    continue
                shown.add(text)
            if mark is not None:
                text = mark.sub(lambda m: f"⟦{m.group(0)}⟧", text)
                rest = re.sub(r"⟦[^⟧]*⟧", "", text)
                if not (re.search(r"[A-Za-z]{2}", rest) or re.search(r"\d{4}", rest)
                        or patterns.DATE_IN_TEXT.search(rest)):
                    omitted += 1                   # only found values, money and punctuation
                    continue
            out.append(f"L{n}: {text}")
        head = f"=== {pid} | {p.file}" + (f" | page {p.page}" if p.page else "")
        if omitted:
            head += f" | {omitted} line(s) shown on earlier pages left out"
        blocks.append(head + " ===\n" + ("\n".join(out) if out else "(no new lines)"))
    batches, current, size, rows = [], [], 0, 0
    for block in blocks:
        n = block.count("\n") + 1
        if current and (size + len(block) > SWEEP_BATCH_CHARS or rows + n > SWEEP_BATCH_LINES):
            batches.append(current)
            current, size, rows = [], 0, 0
        current.append(block)
        size += len(block)
        rows += n
    if current:
        batches.append(current)
    ids = []
    for i, batch in enumerate(batches, 1):
        bid = f"batch_{i:03d}"
        ids.append(bid)
        header = (f"SWEEP {bid} ({i} of {len(batches)})\nPARTIES: {parties}\n"
                  "Values in ⟦ ⟧ are already found - do not report them.\n\n")
        (folder / f"{bid}.txt").write_text(header + "\n\n".join(batch)
                                           + f"\n\n=== END OF {bid} ===\n", encoding="utf-8")
    manifest = {"signature": signature, "batches": ids}
    save_json(folder / "manifest.json", manifest)
    return manifest


def read_sweep(folder: Path, manifest: dict, pages: list[Page], labels: list[tuple[str, dict]],
               candidates: list[dict], values: list[Value],
               dropped: list[dict] = ()) -> list[dict]:
    """The sweep's findings as review items. A value not printed on the page it
    names is rejected (the model misread or invented it); one already found
    is dropped; everything else goes to the review, flagged as the sweep's."""
    by_id = page_ids(pages)
    derived = {d.casefold() for d in derived_names(labels)}
    have = {(c["kind"], value_key(c["kind"], c["value"])) for c in candidates}
    have |= {(v.kind, value_key(v.kind, v.value)) for v in values}
    ruled = {value_key(d["kind"], d["value"]) for d in dropped}
    owners = {label.casefold(): label for label, _ in labels} | {
        BOTH.casefold(): BOTH, "both": BOTH, CASE.casefold(): CASE}
    found: dict[tuple[str, str], dict] = {}
    rejected, skipped = [], []
    for bid in manifest["batches"]:
        for raw in (folder / f"{bid}.found.txt").read_text("utf-8", "replace").splitlines():
            line = raw.strip().strip("`")
            if not line or line.upper() == "NONE" or line.startswith("#"):
                continue
            parts = [x.strip() for x in line.split("|")]
            if len(parts) < 5:
                rejected.append(f"{bid}: not 6 fields: {line[:80]}")
                continue
            if parts and parts[0] == "":
                parts = parts[1:]                  # a markdown row: "| P0042 | L7 | ..."
            while parts and parts[-1] == "":
                parts = parts[:-1]
            if len(parts) < 5:
                rejected.append(f"{bid}: not 6 fields: {line[:80]}")
                continue
            pid, lno, value, field_word, whose = parts[:5]
            reason = " | ".join(parts[5:]) or "found by the sweep"
            value = value.replace("⟦", "").replace("⟧", "").strip().strip("\"'`").rstrip(".,;:")
            digits_id = re.sub(r"\D", "", pid)
            pid = f"P{int(digits_id):04d}" if digits_id else pid
            page = by_id.get(pid.upper())
            kind = FIELD_WORDS.get(field_word.casefold().strip()) or _kind_by_shape(value)
            if page is None:
                rejected.append(f"{bid}: no page {pid}")
                continue
            if not value or _fold(value) not in _fold(page.text):
                rejected.append(f"{bid}: {pid} does not print {value[:40]!r}")
                continue
            key = (kind, value_key(kind, value))
            skip = ("already in the review" if key in have else
                    "a form the app derives from a party's name"
                    if kind == "name" and _norm(value).casefold() in derived else
                    "a town on its own" if kind in ("address", "other")
                    and places.known(_norm(value)) else
                    "kept out by a rule the app applies itself"
                    if key[1] in ruled or value_key("account", value) in ruled else "")
            if skip:
                skipped.append(f"{bid}: {pid} {value[:40]!r} - {skip}")
                continue
            lines = page.text.split("\n")
            n = int(re.sub(r"\D", "", lno) or 0)
            if not (1 <= n <= len(lines)) or _fold(value) not in _fold(lines[n - 1]):
                n = next((i for i, l in enumerate(lines, 1) if _fold(value) in _fold(l)), 0)
            context = _clip(lines[n - 1], value) if n else ""
            if n > 1 and lines[n - 2].strip():
                context = _clip(lines[n - 2], "") + "\n" + context
            item = found.get(key)
            if item is None:
                found[key] = {"kind": kind, "value": _norm(value), "detail": "",
                              "file": page.file, "page": page.page, "count": 1,
                              "proposed": owners.get(whose.casefold()),
                              "why": f"sweep: {reason[:90]}",
                              "contexts": [{"where": page.file + (f" p.{page.page}" if page.page
                                                                  else ""), "text": context}]}
            else:
                item["count"] += 1
    (folder / "rejected.txt").write_text("\n".join(rejected) + ("\n" if rejected else ""),
                                         encoding="utf-8")
    (folder / "skipped.txt").write_text("\n".join(skipped) + ("\n" if skipped else ""),
                                        encoding="utf-8")
    return list(found.values())


def strong(item: dict) -> bool:
    """A proposal "_rest": "accept" may wave through: a party's own name or role
    word on the value's line, or this case's number. Anything weaker - a block
    under a name, a header, the sweep, no proposal - is answered explicitly."""
    return bool(item["proposed"]) and item["why"] in ("same line", "case number")


def check_decisions(path: Path, decisions, items: list[dict], groups: list[dict],
                    valid: set[str]) -> tuple[list[str], dict]:
    """Every answer an id the review has and a word the review offers, or a list
    of exactly what to fix - never a silent guess. Answers are matched without
    regard to case ("petitioner" is Petitioner) and returned normalised."""
    if not path.exists():
        return [], {}
    if not isinstance(decisions, dict):
        return ["it is not one JSON object of id: answer pairs"], {}
    canon = {v.casefold(): v for v in valid | {"drop", "unsure"}}
    ids = {i["id"] for i in items} | {g["id"] for g in groups}
    problems, out = [], {}
    for k, v in decisions.items():
        k = str(k).strip()
        if k == "_rest":
            if str(v).strip().casefold() != "accept":
                problems.append(f'"_rest" can only be "accept" (got {v!r})')
            out[k] = "accept"
            continue
        if k.upper() not in ids:
            problems.append(f"{k} is not an id in review.txt")
            continue
        answer = canon.get(str(v).strip().casefold())
        if answer is None:
            problems.append(f"{k}: {v!r} is not one of the answers ({', '.join(sorted(canon.values()))})")
            continue
        out[k.upper()] = answer
    if len(problems) > 12:
        problems = problems[:12] + [f"...and {len(problems) - 12} more"]
    return problems, out


def needs_review(v: Value) -> bool:
    """A name form the rules are least sure of: the model looks at it."""
    return v.detail.startswith(("nickname", "spelled differently", "other written form",
                                "former name (clause)", "possible former name (clause)"))


LEFT_LABEL = {"transfer tail": "masked account tails on transaction rows nobody is named on "
                                "(the app replaces these itself)",
              "loan or policy": "loan, mortgage, policy or claim numbers (left as written by "
                                "design)",
              "non-party": "values beside counsel's or a judge's name",
              "tail of full": "masked tails of a full number already proposed",
              "quantity": "decimal quantities shaped like case numbers",
              "left as written": "routing, loan, policy, claim, member, employee, check, "
                                 "order and tracking numbers (left as written by design)"}


GROUP_MIN = 4       # this many items of one shape share a block in the review


def _shape(item: dict) -> tuple:
    """What makes items alike enough to be read as one: field, proposal, why,
    the folder they came from, and their context with the value and digits
    blanked - "(alone on its line)" in accounts/joint_checking_D4/, say."""
    c = item["contexts"][0] if item["contexts"] else {"where": item["file"], "text": ""}
    folder, _, base = c["where"].rpartition("/")
    # the file's name without its dates and numbers: one bank's monthly statements
    stem = re.sub(r"[\d_\-. ]+", " ", base.split(" p.")[0]).strip().casefold()
    if item["proposed"] is None:
        # nobody is tied to these: grouped by file and field, each member keeping
        # its own snippet in the block - the model reads every one
        return (item["kind"], None, item["why"], folder, stem)
    line = next((x for x in c["text"].split("\n") if "[[" in x), c["text"])
    text = re.sub(r"\[\[.*?\]\]", "[[]]", line)
    text = re.sub(r"\d+(?:[.,]\d+)*", "9", text)
    return (item["kind"], item["proposed"], item["why"], folder, stem, text.casefold())


def group_items(items: list[dict]) -> list[dict]:
    """Fold runs of alike items into groups; each item keeps its own id."""
    by_shape: dict[tuple, list[dict]] = defaultdict(list)
    for item in items:
        # a value seen where it points to different owners is read on its own,
        # with every place shown - never folded into a group
        if item["kind"] != "name" and not item["why"].startswith("conflict") \
                and not item.get("mixed"):
            by_shape[_shape(item)].append(item)
    groups = []
    for shape, members in by_shape.items():
        if len(members) < GROUP_MIN:
            continue
        gid = f"G{len(groups) + 1}"
        for m in members:
            m["group"] = gid
        groups.append({"id": gid, "members": [m["id"] for m in members]})
    return groups


def write_review_text(path: Path, labels: list[tuple[str, dict]], items: list[dict],
                      left: Counter | None = None, groups: list[dict] | None = None) -> None:
    """The review in as few words as will do - what the model reads."""
    out = ["PARTIES"]
    for label, entry in labels:
        forms = entry.get("names", [])
        out.append(f"  {label} = {forms[0]}" + (f" (also {'; '.join(forms[1:])})"
                                                 if len(forms) > 1 else "")
                   + (f" [proposed: {entry['source']} - check it]" if entry.get("source") else ""))
    answers = [label for label, _ in labels] + [BOTH, CASE, "drop", "unsure"]
    out += ["", "ANSWER each item with one of: " + ", ".join(answers),
            "Format: id | proposed owner (? = none) | field | value | why proposed | times found",
            '"_rest": "accept" covers only proposals whose why is "same line" or "case number";'
            " every other item and group needs its own answer.",
            "Why: same line = a party's name or role word on the value's line; block under a"
            " name (n up) = n lines below one; first-page header = a statement's or form's"
            " header block; only party on the page; transfer line; nobody named near;"
            " conflict = tied to two parties in different places; name form = a written form"
            " of the party's name. The indented line is where it was found ([[value]]).",
            ""]
    if left:
        out.insert(len(out) - 1, "Not proposed, by rule (counts only): " + "; ".join(
            f"{n} {LEFT_LABEL.get(k, k)}"
            for k, n in sorted(left.items())))
    by_id = {i["id"]: i for i in items}
    if groups:
        out.append("GROUPS - items of one shape. Members of a ? group are listed with their own"
                   " line after the colon. Answer a group by its G id (it applies to every member), and"
                   " answer any member that differs by its R id - an R answer overrides its"
                   " group. Read every member: one unlike the rest is answered by its R id.")
        out.append("")
        for g in groups:
            members = [by_id[r] for r in g["members"]]
            first = members[0]
            out.append(f"{g['id']} | {len(members)} items | {first['proposed'] or '?'} | "
                       f"{FIELD_LABEL[first['kind']]} | {first['why']}")
            examples = [m for m in (members[0], members[-1]) if m["contexts"]]
            for m in {m["id"]: m for m in examples}.values():
                c = m["contexts"][0]
                text = " / ".join(line for line in c["text"].split("\n") if line.strip())
                out.append(f"    e.g. {c['where']}: {text}")
            for m in members:
                where = m["contexts"][0]["where"].rsplit("/", 1)[-1] if m["contexts"] else ""
                own = ""
                if m["contexts"] and m["proposed"] is None:
                    marked = next((x for x in m["contexts"][0]["text"].split("\n") if "[[" in x),
                                  "")
                    own = " : " + _clip(marked.replace("[[", "").replace("]]", ""),
                                        m["value"], 60) if marked else ""
                out.append(f"    {m['id']} {m['value']} [{where}] x{m['count']}{own}")
        out += ["", "ITEMS"]
    for item in items:
        if item.get("group"):
            continue
        out.append(f"{item['id']} | {item['proposed'] or '?'} | {FIELD_LABEL[item['kind']]} | "
                   f"{item['value']}" + (f" ({item['detail']})" if item["detail"] and item["kind"] != "name"
                                     else "")
                   + f" | {item['why']} | x{item['count']}")
        for c in item["contexts"]:
            text = " / ".join(line for line in c["text"].split("\n") if line.strip())
            out.append(f"    {c['where']}: {text}")
    out.append("")
    out.append(f"=== END OF REVIEW ({len(items)} items"
               + (f", {len(groups)} groups" if groups else "") + ") ===")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def merge_rows(rows: list[Value]) -> list[Value]:
    """One row per owner, field and value, however often it was found."""
    merged: dict[tuple[str, str, str], Value] = {}
    for v in rows:
        key = (v.owner, v.kind, value_key(v.kind, v.value))
        hit = merged.get(key)
        if hit is None:
            merged[key] = v
        else:
            hit.count += v.count
            hit.file, hit.page = hit.file or v.file, hit.page or v.page
            if v.detail and v.detail not in hit.detail:
                hit.detail = "; ".join(x for x in (hit.detail, v.detail) if x)
    return list(merged.values())


def child_dobs(labels: list[tuple[str, dict]]) -> list[Value]:
    return [Value(label, "dob", entry["dob"], "from the child roster", "", "", 0)
            for label, entry in labels if label.startswith("Child") and entry.get("dob")]


# --------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="scan, analyze and write the CSVs, stopping where a model is needed")
    run.add_argument("input", type=Path, help="a folder or a .zip")
    run.add_argument("--out", type=Path, help="where the CSVs go (default: beside the input)")
    run.add_argument("--single-case", action="store_true",
                     help="the whole input is one case, even with subfolders")
    run.add_argument("--rescan", action="store_true", help="read every file again")
    run.add_argument("--no-ocr", action="store_true", help="queue every scan for a model instead")
    run.add_argument("--no-sweep", action="store_true",
                     help="skip the model's read-through for what the rules missed (lower recall; "
                          "only when the attorney accepts that)")
    run.add_argument("--case", help="limit to one case (by name)")
    run.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                     help="skip files whose name or relative path matches (repeat); "
                          "rerun with --rescan after changing it")
    run.add_argument("--petitioner", action="append", help="petitioner's name (repeat for former names)")
    run.add_argument("--respondent", action="append", help="respondent's name (repeat for former names)")
    run.add_argument("--child", action="append", help="a child's full name (repeat per child)")
    run.add_argument("--other", action="append", help="another party's full name (repeat)")
    clean = sub.add_parser("clean", help="delete the work folder (full text, page images, "
                                         "review files) once the CSVs are delivered")
    clean.add_argument("out", type=Path, help="the output folder the run printed")
    args = ap.parse_args(argv)
    if args.cmd == "clean":
        work = args.out.expanduser().resolve() / ".work"
        if not work.is_dir():
            print(f"Nothing to clean: {work} does not exist")
            return 0
        shutil.rmtree(work)
        print(f"Deleted {work}. The CSVs beside it are kept.")
        return 0

    EXCLUDE[:] = args.exclude
    source = args.input.expanduser().resolve()
    if not source.exists():
        print(f"ERROR: {source} does not exist")
        return 1
    out = (args.out.expanduser().resolve() if args.out
           else source.parent / f"{source.stem if source.is_file() else source.name}_party_pii")
    if out == source or source in out.parents:
        print("ERROR: --out must be outside the folder being read")
        return 1
    work_root = out / ".work"
    if source.is_file():
        if source.suffix.lower() != ".zip":
            print("ERROR: INPUT must be a folder or a .zip")
            return 1
        unzipped = work_root / "_input"
        if args.rescan or not unzipped.exists():
            shutil.rmtree(unzipped, ignore_errors=True)
            _safe_extract(source, unzipped)
        tops = [p for p in unzipped.iterdir() if not p.name.startswith(".") and p.name != "__MACOSX"]
        source = tops[0] if len(tops) == 1 and tops[0].is_dir() else unzipped

    cases = find_cases(source, args.single_case)
    if args.case:
        cases = {k: v for k, v in cases.items() if k == args.case}
        if not cases:
            print(f"ERROR: no case named {args.case!r}")
            return 1
    if (args.petitioner or args.respondent or args.child or args.other) and len(cases) > 1:
        print("ERROR: party names apply to one case - add --case NAME or --single-case")
        return 1
    if not args.single_case and len(cases) > 1 and looks_like_sections(source):
        print("WARNING: the subfolders look like sections of ONE case "
              f"({', '.join(sorted(cases)[:6])}...). If so, rerun with --single-case.")

    print(f"Input: {len(cases)} case(s). Output folder: {out}")
    use_ocr = not args.no_ocr
    if use_ocr and not tesseract():
        print("No OCR engine here: pages without text will be queued for a model to transcribe.")

    stops: dict[str, list[str]] = defaultdict(list)
    done = []
    for name, src in cases.items():
        work = work_root / safe_name(name)
        work.mkdir(parents=True, exist_ok=True)
        if args.rescan or not (work / "scan.json").exists():
            info = scan_case(name, src, work, use_ocr)
        else:
            info = load_json(work / "scan.json", {})
        unread = info.get("unreadable", [])
        if unread:
            print(f"WARNING {name}: {len(unread)} file(s) could not be read and were not "
                  f"searched - listed in {work / 'scan.json'}")
            if any("no PDF reader" in why for _, why in unread):
                print("  No PDF reader here: install one (pip install pymupdf, or pypdf), "
                      "then rerun with --rescan.")
        blank = unread_transcripts(work)
        if blank:
            print(f"WARNING {name}: {len(blank)} scanned page(s) could not be read by the "
                  f"transcriber and were not searched: {', '.join(blank[:5])}"
                  + (" ..." if len(blank) > 5 else ""))
        waiting = missing_transcripts(work)
        if waiting:
            stops["transcribe"].append(f"{name}: {len(waiting)} page(s) - {work / 'queue.json'}")
            continue

        pages = load_pages(work)
        parties_path = work / "parties.json"
        try:
            parties = json.loads(parties_path.read_text("utf-8")) if parties_path.exists() else None
        except ValueError as exc:
            stops["parties"].append(f"{name}: parties.json is not valid JSON ({exc}) - fix it")
            continue
        fresh = parties is None or (args.rescan and parties.get("status") in ("auto", "given"))
        if fresh:
            parties = harvest_parties(pages)
        parties = apply_cli_parties(parties, args)
        problems = check_parties(parties)
        if problems:
            stops["parties"].append(f"{name}: parties.json needs fixing - " + "; ".join(problems))
            continue
        # children no roster lists - a patient on a bill, a dependent on a 1040 -
        # once the parents are known, however they came to be known
        if parties.get("status") != "needs_input" and not parties.get("household_checked") \
                and not args.child:
            parties["children"] = parties.get("children", []) + household_children(parties, pages)
            parties["household_checked"] = True
        save_json(parties_path, parties)
        if parties.get("status") == "needs_input":
            stops["parties"].append(f"{name}: {parties_path}")
            continue

        labels = party_labels(parties)
        variants = name_variants(pages, labels)
        variants += [v for v in former_name_clauses(pages, labels)
                     if (v.owner, v.value.casefold()) not in
                     {(x.owner, x.value.casefold()) for x in variants}]
        candidates, left, dropped = detect(pages, with_variants(labels, variants),
                                           parties.get("non_parties", []))
        save_json(work / "not_proposed.json", dropped)     # every rule drop, for an audit
        values = [v for v in variants if not needs_review(v)] + child_dobs(labels)
        candidates += [{"kind": "name", "value": v.value, "detail": v.detail, "file": v.file,
                        "page": v.page, "count": v.count, "proposed": v.owner or None,
                        "why": f"name form: {v.detail}",
                        "contexts": [v.context] if v.context else []}
                       for v in variants if needs_review(v)]
        if not args.no_sweep:
            state, found = sweep_stage(work, pages, labels, candidates, values, dropped)
            if state != "done":
                stops["sweep"].append(f"{name}: {state}")
                continue
            candidates += found
        items = sorted(candidates, key=lambda c: (role_rank(c["proposed"] or "Unassigned"),
                                                  FIELD_ORDER.index(c["kind"]),
                                                  value_key(c["kind"], c["value"])))
        for i, item in enumerate(items, 1):
            item["id"] = f"R{i}"
        groups = group_items(items)
        review_path, decisions_path = work / "review.json", work / "decisions.json"
        try:
            decisions = json.loads(decisions_path.read_text(encoding="utf-8")) \
                if decisions_path.exists() else {}
        except ValueError as exc:
            stops["review"].append(f"{name}: decisions.json is not valid JSON ({exc}) - fix it")
            continue
        previous = load_json(review_path, {}) or {}
        # everything the reviewer is shown: a change to any of it - a context, a
        # group - and the old answers no longer answer what is on the page
        signature = hashlib.sha1(json.dumps([items, groups], sort_keys=True,
                                            ensure_ascii=False).encode()).hexdigest()
        if previous.get("signature") != signature:
            decisions = {}                           # the items changed: old answers are stale
            if decisions_path.exists():
                decisions_path.unlink()
        save_json(review_path, {"signature": signature, "items": items, "groups": groups})
        write_review_text(work / "review.txt", labels, items, left, groups)
        valid = {label for label, _ in labels} | {BOTH, CASE}
        problems, decisions = check_decisions(decisions_path, decisions, items, groups, valid)
        if problems:
            stops["review"].append(f"{name}: decisions.json needs fixing - " + "; ".join(problems))
            continue
        accept_rest = str(decisions.get("_rest", "")).strip().casefold() == "accept"
        missing = [i["id"] for i in items
                   if i["id"] not in decisions and i.get("group") not in decisions
                   and not (accept_rest and strong(i))]
        if items and (missing or not decisions):
            stops["review"].append(f"{name}: {len(items)} item(s), {len(missing)} without a "
                                   f"decision - read {work / 'review.txt'}, write {decisions_path}"
                                   + (f" (still unanswered: {', '.join(missing[:20])}"
                                      + (" ..." if len(missing) > 20 else "") + ")"
                                      if decisions else ""))
            continue
        for item in items:
            choice = str(decisions.get(item["id"]) or decisions.get(item.get("group") or "")
                         or (item["proposed"] if accept_rest and strong(item) else "")
                         or "unsure").strip()
            if choice.casefold() == "drop":
                continue
            if choice not in valid:
                choice = "Unassigned"
            note = ("" if choice == item["proposed"] else
                    "model could not tell whose" if choice == "Unassigned"
                    else "assigned by the model")
            values.append(Value(choice, item["kind"], item["value"],
                                "; ".join(x for x in (item["detail"], note) if x),
                                item["file"], item["page"], item["count"]))
        csv_path = out / f"{safe_name(name)}.csv"
        write_csv(csv_path, merge_rows([refile(v) for v in values]))
        counts = Counter(v.owner for v in values if not v.detail.startswith("derived"))
        done.append((name, csv_path, counts, info, left))

    for name, csv_path, counts, info, left in done:
        roles = ", ".join(f"{k}: {counts[k]}" for k in sorted(counts, key=role_rank))
        unread = info.get("unreadable", [])
        print(f"DONE {name}: {csv_path.name} - {roles or 'no values'}; "
              f"{info.get('files', 0)} files, {info.get('pages', 0)} pages "
              f"({info.get('ocr_pages', 0)} OCR'd); {len(unread)} file(s) unreadable"
              + (f" (listed in {(work_root / safe_name(name) / 'scan.json')})" if unread else ""))
    steps = (("transcribe", 2, "transcribe the queued page images (see SKILL.md, "
              "'Transcribing scans')"),
             ("parties", 3, "no caption named both parties - fill in parties.json (see "
              "SKILL.md, 'Naming the parties')"),
             ("sweep", 5, "read the sweep batches for anything the rules missed (see SKILL.md, "
              "'The sweep')"),
             ("review", 4, "decide each candidate value (see SKILL.md, 'Reviewing')"))
    code = 0
    for key, exit_code, what in steps:
        if stops[key]:
            print(f"NEXT: {what}, then run this command again.")
            for s in stops[key]:
                print(f"  {s}")
            code = code or exit_code
    if code:
        return code
    print("NEXT: nothing - all CSVs written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

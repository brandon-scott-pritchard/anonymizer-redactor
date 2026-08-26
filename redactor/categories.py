"""Category registry.

Every detector, surrogate and report line is keyed by a category id defined
here.  ``style`` decides what the anonymize mode substitutes:

    "person"      -> a realistic fake name (component aware)
    "placeholder" -> a tagged placeholder such as ``[SSN-1]``

Redact mode ignores ``style`` and blacks everything out.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    tag: str          # placeholder tag stem, e.g. "SSN" -> [SSN-1]
    group: str        # UI grouping
    style: str = "placeholder"
    enabled: bool = True


def _c(key, label, tag, group, style="placeholder", enabled=True):
    return Category(key, label, tag, group, style, enabled)


CATEGORIES: tuple[Category, ...] = (
    # ---------------------------------------------------------------- people
    _c("person", "Person name", "NAME", "People", style="person"),
    _c("minor", "Minor child", "MINOR", "People", style="person"),
    # A roster naming children by initials is already the form the rules ask
    # for, but the initials still identify a child against the rest of the
    # document. A placeholder keeps the table readable; a full invented name in
    # a two-character cell would not.
    _c("minor_initials", "Minor child (initials)", "CHILD", "People"),
    _c("organization", "Organization / employer / school", "ORG", "People"),
    _c("location", "Location / city / facility", "LOCATION", "People"),

    # --------------------------------------------------------------- contact
    _c("email", "Email address", "EMAIL", "Contact"),
    _c("phone", "Phone number", "PHONE", "Contact"),
    _c("fax", "Fax number", "FAX", "Contact"),
    _c("url", "Web address", "URL", "Contact"),
    _c("social_handle", "Social media handle", "HANDLE", "Contact"),
    _c("username", "Username / login", "USERNAME", "Contact"),
    _c("ip_address", "IP address", "IP", "Contact"),
    _c("mac_address", "Device / MAC address", "DEVICE", "Contact"),
    _c("gps", "GPS coordinates", "GPS", "Contact"),

    # ------------------------------------------------------ government ident
    _c("ssn", "Social Security number", "SSN", "Government ID"),
    _c("ein", "EIN / Tax ID", "TAXID", "Government ID"),
    _c("drivers_license", "Driver's license", "DL", "Government ID"),
    _c("passport", "Passport number", "PASSPORT", "Government ID"),
    _c("alien_number", "USCIS A-number / visa", "ANUM", "Government ID"),
    _c("military_id", "Military service number", "MILID", "Government ID"),
    _c("inmate_number", "Inmate / booking number", "INMATE", "Government ID"),
    _c("student_id", "Student ID", "STUDENTID", "Government ID"),
    # On a pay stub this sits beside the SSN and identifies the employee just as
    # well within the employer's records.
    _c("employee_id", "Employee / payroll number", "EMPLOYEEID", "Government ID"),
    _c("voter_id", "Voter registration number", "VOTERID", "Government ID"),
    _c("bar_number", "Bar number", "BARNO", "Government ID"),
    _c("notary_id", "Notary commission number", "NOTARY", "Government ID"),
    _c("professional_license", "Professional license", "LICENSE", "Government ID"),
    _c("tribal_id", "Tribal enrollment number", "TRIBALID", "Government ID"),

    # ---------------------------------------------------------------- health
    _c("mrn", "Medical record number", "MRN", "Health"),
    _c("health_plan", "Health plan / member number", "HEALTHPLAN", "Health"),
    _c("diagnosis_code", "Diagnosis / procedure code", "DXCODE", "Health"),
    _c("prescription", "Prescription number", "RX", "Health"),

    # ------------------------------------------------------------- financial
    _c("bank_account", "Bank account number", "ACCOUNT", "Financial"),
    # Shares the ACCOUNT tag on purpose: the counter keys on the tag, so a
    # masked tail and the full number it belongs to draw from one series and
    # read as the same kind of thing on the delivered page.
    _c("masked_account", "Masked account / card tail", "ACCOUNT", "Financial"),
    _c("routing_number", "Routing number", "ROUTING", "Financial"),
    _c("iban", "IBAN", "IBAN", "Financial"),
    _c("swift", "SWIFT / BIC", "SWIFT", "Financial"),
    _c("credit_card", "Credit / debit card", "CARD", "Financial"),
    _c("payment_handle", "Payment app handle (Venmo, PayPal…)", "PAYHANDLE", "Financial"),
    _c("crypto_wallet", "Crypto wallet address", "WALLET", "Financial"),
    _c("investment_account", "Retirement / brokerage account", "INVACCOUNT", "Financial"),
    _c("loan_number", "Loan / mortgage / escrow number", "LOAN", "Financial"),
    _c("policy_number", "Insurance policy number", "POLICY", "Financial"),
    _c("claim_number", "Insurance claim number", "CLAIM", "Financial"),
    _c("check_number", "Check / invoice / wire number", "CHECK", "Financial"),

    # ---------------------------------------------------------- property
    _c("street_address", "Street address", "ADDRESS", "Property"),
    _c("parcel_number", "APN / parcel number", "PARCEL", "Property"),
    _c("deed_reference", "Deed book & page", "DEEDREF", "Property"),
    _c("legal_description", "Legal description (lot/block)", "LEGALDESC", "Property"),
    _c("vin", "VIN", "VIN", "Property"),
    _c("license_plate", "License plate", "PLATE", "Property"),
    _c("vessel_number", "Boat hull / aircraft tail number", "VESSEL", "Property"),
    _c("safe_deposit", "Safe deposit box", "SAFEBOX", "Property"),
    _c("storage_unit", "Storage unit", "STORAGE", "Property"),

    # ------------------------------------------------------------------ case
    _c("case_number", "Case / docket number", "CASENO", "Case"),
    _c("case_name", "Case name / caption", "CASENAME", "Case"),
    _c("case_designator", "Other case designator", "CASEREF", "Case"),

    # ------------------------------------------------------------------ vital
    _c("dob", "Date of birth", "DOB", "Vital"),
    _c("pob", "Place of birth", "POB", "Vital"),
)

BY_KEY: dict[str, Category] = {c.key: c for c in CATEGORIES}
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(c.group for c in CATEGORIES))


def label_for(key: str) -> str:
    cat = BY_KEY.get(key)
    return cat.label if cat else key


def style_for(key: str) -> str:
    cat = BY_KEY.get(key)
    return cat.style if cat else "placeholder"


def tag_for(key: str) -> str:
    cat = BY_KEY.get(key)
    return cat.tag if cat else key.upper()

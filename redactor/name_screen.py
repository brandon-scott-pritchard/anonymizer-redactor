"""Which of the name screen's suggestions are names: Kev and probablepeople.

The scan proposes a merchant ("Dollar Tree Layton"), a heading ("Promo
Expiration Date") or a town ("Layton UT") as a person as readily as a person,
and "Tick all" sent every one of them through the run. The word lists in
:func:`ner.likely_name` catch the headings made of everyday words, but not a
store or a town.

Two readers sort them here, each on its own:

- Kev's person-name check (name_clusters.kev_is_name) - is this a person's
  name, not a store, brand, product, company, place or heading?
- probablepeople (datamade, MIT) - is it a person, a household or a
  corporation, by how the words are put together?

A suggestion is "other" only when both say it is not a person; if either
calls it one, it is a person. On a real case the two together set 268 of 612
name rows apart, where Kev alone set 233 of 285 main names apart and
probablepeople alone missed a third of the people on payment lines.

It only sorts: a suggestion called "other" is listed apart, unticked, and the
operator can tick it. Nothing is dropped. (This replaced the local language
model's sort on 2026-10-16.)
"""

from __future__ import annotations


def classify(texts: list[str], kev_url: str, progress=None) -> dict[str, str]:
    """{text: "person" | "other"} for each suggestion."""
    import probablepeople

    from . import name_clusters

    is_name = name_clusters.kev_is_name(kev_url)
    out: dict[str, str] = {}
    for index, text in enumerate(texts):
        if progress is not None and index % 25 == 0:
            progress("Kev is sorting the suggestions into names and not names",
                     index / max(len(texts), 1))
        try:
            kev_person = is_name(text) >= name_clusters.KEV_NAME
        except Exception:
            kev_person = True                      # unanswered: not set apart
        try:
            library_person = probablepeople.tag(text)[1] != "Corporation"
        except probablepeople.RepeatedLabelError:
            library_person = True
        out[text] = "person" if kev_person or library_person else "other"
    return out


def describe(labels: dict[str, str]) -> str:
    others = sum(1 for v in labels.values() if v == "other")
    return (f"Kev and probablepeople sorted the suggestions: {others} that both call no "
            "person (stores, places, headings) are listed apart." if labels else "")


def keeps(text: str, category: str, label: str | None) -> bool | None:
    """The sort, with the two things it may not overrule: an employer the
    scan found is the household's own, and a common first name before a
    surname is a person. None when the suggestion was not sorted."""
    from . import ner

    if category == "employer":
        return True
    if category in ("person", "minor") and ner.heading_ended(text):
        return False                   # "Tran Date": a table read across, whoever says so
    if label is None:
        return None
    if label != "other":
        return True
    words = [ner._plain(w) for w in text.split()]
    _common, given = ner._lists()
    return len(words) >= 2 and words[0] in given and words[-1] not in ner._RAILS_AND_BRANDS

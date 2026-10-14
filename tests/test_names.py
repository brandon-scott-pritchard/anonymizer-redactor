

def test_a_surname_both_spouses_share_is_theirs_not_a_third_person():
    """Ticked on its own beside both spouses, "Smith" was told it "could be
    either - gets its own pseudonym" and became a third person."""
    from redactor import engine, review
    from redactor.engine import Settings
    store, overlaps = review.build_store([("Jane Elizabeth Smith", "person"),
                                          ("John Michael Smith", "person"),
                                          ("Smith", "person")])
    assert len(overlaps) == 1 and "share" in overlaps[0].note
    assert {e.canonical for e in store.entities.values()} == {
        "Jane Elizabeth Smith", "John Michael Smith"}
    text = "Ms. Smith moved out. The Smiths sold it. Smith signed."
    settings = Settings(use_ner=False)
    hits = engine.scan_text(text, store, settings, engine.PersonMatcher(store, settings),
                            register=True)
    surnames = {e.replacement.split()[-1] for e in store.entities.values()}
    assert len(surnames) == 1
    shared = surnames.pop()
    out = text
    for hit in sorted(hits, key=lambda h: -h.start):
        out = out[:hit.start] + hit.replacement + out[hit.end:]
    assert out == f"Ms. {shared} moved out. The {shared}s sold it. {shared} signed."

from stackd.data.names import Candidate, NameMatcher, normalize_name


def test_normalize_punctuation_suffixes_and_last_first():
    assert normalize_name("D.J. Moore") == "dj moore"
    assert normalize_name("Marvin Harrison Jr.") == "marvin harrison"
    assert normalize_name("Michael Pittman Jr") == "michael pittman"
    assert normalize_name("Amon-Ra St. Brown") == "amon ra st brown"
    assert normalize_name("Ja'Marr Chase") == "jamarr chase"
    assert normalize_name("Cunningham, Cade") == "cade cunningham"
    assert normalize_name("Harrison Jr., Marvin") == "marvin harrison"
    assert normalize_name("Kenneth Walker III") == "kenneth walker"
    assert normalize_name("Clément Dupont") == "clement dupont"


CANDS = [
    Candidate("A", "DJ Moore", "BUF", "WR"),
    Candidate("B", "Josh Allen", "BUF", "QB"),
    Candidate("C", "Josh Allen", "JAX", "LB"),
    Candidate("D", "Kenneth Walker", "SEA", "RB"),
    Candidate("E", "Christian McCaffrey", "SF", "RB"),
]


def test_exact_match_restricted_to_game_teams():
    m = NameMatcher(CANDS)
    r = m.match("Josh Allen", teams={"BUF", "KC"}, market="pass_yds")
    assert (r.gsis_id, r.method) == ("B", "exact")


def test_same_name_other_team_is_ambiguous_without_team_filter():
    m = NameMatcher(CANDS)
    assert m.match("Josh Allen").method == "ambiguous"


def test_fuzzy_match():
    m = NameMatcher(CANDS)
    r = m.match("Christian McCaffery", teams={"SF", "LA"})
    assert (r.gsis_id, r.method) == ("E", "fuzzy")


def test_low_score_is_unmatched_not_guessed():
    m = NameMatcher(CANDS)
    assert m.match("Deebo Samuel", teams={"SF", "LA"}).method == "unmatched"


def test_override_wins():
    m = NameMatcher(CANDS, overrides={("dj moore", "BUF"): "ZZZ"})
    r = m.match("D.J. Moore", teams={"BUF"}, team_hint="BUF")
    assert (r.gsis_id, r.method) == ("ZZZ", "override")

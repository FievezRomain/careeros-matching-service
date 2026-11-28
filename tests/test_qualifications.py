from uuid import UUID

import pytest

from careeros_matching.domain.scoring import (
    Fact,
    QualificationUnit,
    Requirement,
    assess_qualification,
    explicit_threshold,
)


@pytest.mark.parametrize(
    ("key", "statement", "expected"),
    [
        ("Python", "Python: at least 5 years of experience", ("experience_months", 60)),
        ("Python", "Minimum de 3 ans d’expérience en Python", ("experience_months", 36)),
        ("English", "English B2 required", ("cefr", "B2")),
        ("SQL", "SQL level 4/5 required", ("level", 4)),
        ("SQL", "SQL: 18 months of experience", ("experience_months", 18)),
        ("Python", "Python: 2 to 5 years", None),
        ("Python", "Python: up to 5 years", None),
        ("Python", "5 years in Java, Python optional", None),
        ("English", "English B2 or French C1", None),
        ("C", "C++ level 4/5", None),
        ("Licence", "Licence B2 required", None),
    ],
)
def test_explicit_minimum_is_unambiguous(
    key: str, statement: str, expected: tuple[QualificationUnit, int | str] | None
) -> None:
    requirement = Requirement(
        requirement_id=UUID(int=1), key=key, minimum_level=1, mandatory=True, statement=statement
    )
    assert explicit_threshold(requirement) == expected


def test_level_does_not_establish_duration_or_cefr() -> None:
    fact = Fact(fact_id=UUID(int=2), key="English", level=5)
    assert assess_qualification(("experience_months", 24), fact).status == "unknown"
    assert assess_qualification(("cefr", "B2"), fact).status == "unknown"
    assert assess_qualification(("level", 4), fact).status == "met"

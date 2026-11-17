import pytest

def test_family_is_open_gap():
    from src.patenttrack import family
    with pytest.raises(NotImplementedError):
        family.process({"id": "x"})

def test_legal_status_is_open_gap():
    from src.patenttrack import legal_status
    with pytest.raises(NotImplementedError):
        legal_status.process({"id": "x"})

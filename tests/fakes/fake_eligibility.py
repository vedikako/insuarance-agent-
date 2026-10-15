from tests.fakes.script import Script


def scripted_eligibility(results: list) -> Script:
    return Script(results)

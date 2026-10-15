from tests.fakes.script import Script


def scripted_document(results: list) -> Script:
    return Script(results)

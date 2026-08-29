import pytest

from tests.helpers import read_html_file


@pytest.fixture(scope="package")
def patch_notes_html_data():
    return read_html_file("patch-notes.html")

"""Every script and module must at least compile: some entry points (eval/*.py, scripts/*.py) are not imported by any
other test, and a syntax error in one went unnoticed until a paid run was started."""
import glob
import os
import py_compile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = sorted(glob.glob(os.path.join(ROOT, "*", "*.py")))


@pytest.mark.parametrize("path", FILES, ids=lambda p: os.path.relpath(p, ROOT))
def test_file_compiles(path, tmp_path):
    py_compile.compile(path, cfile=str(tmp_path / "x.pyc"), doraise=True)


def test_there_are_files_to_check():
    assert any("eval" in f for f in FILES) and len(FILES) > 30

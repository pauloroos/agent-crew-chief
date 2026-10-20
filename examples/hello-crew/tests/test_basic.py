import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from hello_crew import greet


def test_greet():
    assert greet("crew") == "Hello, crew!"

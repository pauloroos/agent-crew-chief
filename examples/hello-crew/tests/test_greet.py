from hello_crew.greet import greet


def test_greet():
    assert greet("crew") == "Hello, crew!"

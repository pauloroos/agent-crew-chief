from hello_crew.cli import main


def test_main_with_name(capsys):
    assert main(["crew"]) == 0
    assert capsys.readouterr().out == "Hello, crew!\n"


def test_main_default(capsys):
    assert main([]) == 0
    assert capsys.readouterr().out == "Hello, world!\n"
